import os
import tempfile
import unittest
from unittest.mock import patch

from tokmon.discord_bot import DiscordService, format_tokens, format_usage, valid_dashboard_url


def snapshot(percent=10, reset_at=2000):
    source = {
        "limits": {
            "five_hour": {"used_percent": percent, "resets_at": reset_at},
            "seven_day": {"used_percent": 20, "resets_at": 9000},
        },
        "windows": {
            "today": {"total": 1234, "requests": 3},
            "5h": {"total": 500, "requests": 2},
            "7d": {"total": 5000, "requests": 9},
        },
    }
    return {"now": 1000, "codex": source, "claude": source, "live": {}}


def alert_snapshot(percent=10, reset_at=2000):
    data = snapshot(percent, reset_at)
    data["claude"] = {"limits": {}, "windows": {}}
    return data


class FakeCollector:
    def snapshot(self):
        return snapshot()


class DiscordFormattingTests(unittest.TestCase):
    def test_dashboard_url_requires_http(self):
        self.assertEqual("https://example.test/", valid_dashboard_url("https://example.test/"))
        self.assertIsNone(valid_dashboard_url("javascript:alert(1)"))

    def test_usage_can_select_one_source(self):
        text = format_usage(snapshot(), "codex")
        self.assertIn("Codex", text)
        self.assertNotIn("Claude", text)
        self.assertIn("1,234 tokens", text)

    def test_token_windows_are_included(self):
        text = format_tokens(snapshot(), "all")
        self.assertIn("近 5 小時：500 tokens", text)
        self.assertIn("近 7 天：5,000 tokens", text)

    def test_empty_allow_list_fails_closed(self):
        service = DiscordService(FakeCollector(), {})
        interaction = type("Interaction", (), {"user": type("User", (), {"id": 123})()})()
        self.assertFalse(service._authorized(interaction))

    def test_explicit_allow_all_opt_in(self):
        service = DiscordService(FakeCollector(), {"allow_all_users": True})
        interaction = type("Interaction", (), {"user": type("User", (), {"id": 123})()})()
        self.assertTrue(service._authorized(interaction))


class DiscordAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_patch = patch("tokmon.discord_bot.STATE_FILE", os.path.join(self.temp.name, "state.json"))
        self.state_patch.start()
        self.service = DiscordService(FakeCollector(), {"thresholds": [50, 80, 95]})

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    def test_first_snapshot_only_establishes_baseline(self):
        self.assertEqual([], self.service._detect_alerts(alert_snapshot(81)))

    def test_threshold_is_sent_once_when_crossed(self):
        self.service._detect_alerts(alert_snapshot(49))
        messages = self.service._detect_alerts(alert_snapshot(52))
        self.assertEqual(1, len(messages))
        self.assertIn("50%", messages[0])
        self.assertEqual([], self.service._detect_alerts(alert_snapshot(53)))

    def test_new_reset_window_with_lower_usage_sends_reset(self):
        self.service._detect_alerts(alert_snapshot(80, 2000))
        messages = self.service._detect_alerts(alert_snapshot(2, 4000))
        self.assertEqual(1, len(messages))
        self.assertIn("已重置", messages[0])

    def test_reset_is_sent_after_expired_window_was_already_zeroed(self):
        before = alert_snapshot(0, 900)
        before["now"] = 1000
        self.service._detect_alerts(before)
        after = alert_snapshot(0, 4000)
        after["now"] = 1010
        messages = self.service._detect_alerts(after)
        self.assertEqual(1, len(messages))
        self.assertIn("已重置", messages[0])


if __name__ == "__main__":
    unittest.main()
