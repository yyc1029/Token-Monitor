"""Live-quota retry policy: what makes the dashboard lag and what must not."""

import os
import tempfile
import unittest
from unittest.mock import patch

from tokmon import collector as collector_mod
from tokmon.collector import STALE_SECONDS, Collector
from tokmon.history import DailyHistory
from tokmon.live import (AUTH, MAX_THROTTLE_WAIT, MAX_TRANSIENT_WAIT, THROTTLED, TRANSIENT, LiveError,
                         backoff_seconds, restore_live_state, window_expired)

NOW = 1_000_000.0


def result(fetched_at, five_hour_reset=None):
    return {"fetched_at": fetched_at, "seven_day": {"used_percent": 20, "resets_at": fetched_at + 86400},
            "five_hour": {"used_percent": 50, "resets_at": five_hour_reset} if five_hour_reset else None}


class BackoffTests(unittest.TestCase):
    def test_transient_failures_recover_within_minutes(self):
        waits = [backoff_seconds(TRANSIENT, 60, 180, n) for n in range(1, 8)]
        self.assertEqual(60, waits[0])
        self.assertEqual(MAX_TRANSIENT_WAIT, max(waits))
        self.assertLess(MAX_TRANSIENT_WAIT, MAX_THROTTLE_WAIT)

    def test_throttled_failures_back_off_exponentially_to_one_hour(self):
        self.assertEqual(600, backoff_seconds(THROTTLED, 600, 180, 1))
        self.assertEqual(1200, backoff_seconds(THROTTLED, 600, 180, 2))
        self.assertEqual(MAX_THROTTLE_WAIT, backoff_seconds(THROTTLED, 600, 180, 9))

    def test_auth_failures_retry_at_a_fixed_pace(self):
        self.assertEqual(300, backoff_seconds(AUTH, 300, 180, 1))
        self.assertEqual(300, backoff_seconds(AUTH, 300, 180, 6))


class RestoreStateTests(unittest.TestCase):
    def test_restart_keeps_last_result_but_drops_transient_backoff(self):
        saved = {"claude": result(NOW - 3600), "codex": None,
                 "_state": {"claude": {"next": NOW + 3000, "kind": TRANSIENT, "fails": 6, "error": "URLError"}}}
        results, state = restore_live_state(saved, NOW)
        self.assertEqual(NOW - 3600, results["claude"]["fetched_at"])
        self.assertEqual(0, state["claude"]["next"])
        self.assertEqual(0, state["claude"]["fails"])
        self.assertIsNone(state["claude"]["error"])

    def test_restart_keeps_throttle_backoff_and_its_reason(self):
        saved = {"_state": {"codex": {"next": NOW + 800, "kind": THROTTLED, "fails": 2, "error": "HTTP 429"}}}
        _, state = restore_live_state(saved, NOW)
        self.assertEqual(NOW + 800, state["codex"]["next"])
        self.assertEqual(THROTTLED, state["codex"]["kind"])
        self.assertEqual("HTTP 429", state["codex"]["error"])

    def test_expired_throttle_and_legacy_next_field_are_ignored(self):
        saved = {"_next": {"claude": NOW + 3000, "codex": NOW + 3000},
                 "_state": {"codex": {"next": NOW - 5, "kind": THROTTLED, "fails": 3}}}
        _, state = restore_live_state(saved, NOW)
        self.assertEqual(0, state["claude"]["next"])
        self.assertEqual(0, state["codex"]["next"])


class WindowExpiryTests(unittest.TestCase):
    def test_reset_since_fetch_means_expired(self):
        self.assertTrue(window_expired(result(NOW - 7200, five_hour_reset=NOW - 60), NOW))

    def test_reset_before_fetch_or_in_future_is_fine(self):
        self.assertFalse(window_expired(result(NOW - 60, five_hour_reset=NOW - 120), NOW))
        self.assertFalse(window_expired(result(NOW - 60, five_hour_reset=NOW + 120), NOW))
        self.assertFalse(window_expired(None, NOW))


class CollectorLiveLoopTests(unittest.TestCase):
    """Drive the collector's scheduling logic without any network or real files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cache = os.path.join(self.tmp.name, "live-cache.json")
        history = os.path.join(self.tmp.name, "daily-history.json")
        self.patches = [
            patch.object(collector_mod, "LIVE_CACHE_FILE", cache),
            patch.object(collector_mod, "DailyHistory", lambda: DailyHistory(path=history)),
        ]
        for p in self.patches:
            p.start()
        self.c = Collector({"live_usage": {"claude": True, "codex": True}})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fail(self, src, kind, retry_after=60, now=NOW):
        st = self.c._live_state[src]
        st.update({"fails": st["fails"] + 1, "kind": kind, "error": "x",
                   "next": now + backoff_seconds(kind, retry_after, self.c.live_interval[src], st["fails"] + 1)})

    def test_wake_from_sleep_retries_transient_failures_immediately(self):
        for _ in range(6):
            self.fail("claude", TRANSIENT)
            self.fail("codex", TRANSIENT)
        self.assertGreater(self.c._live_state["codex"]["next"], NOW + 300)
        self.c._on_wake(NOW + 5000, 5000)
        for src in ("claude", "codex"):
            self.assertTrue(self.c._live_due(src, NOW + 5000))
            self.assertEqual(0, self.c._live_state[src]["fails"])

    def test_wake_keeps_a_short_grace_for_a_real_throttle(self):
        self.fail("claude", THROTTLED, retry_after=600)
        self.c._on_wake(NOW + 100, 100)
        self.assertFalse(self.c._live_due("claude", NOW + 100))
        self.assertTrue(self.c._live_due("claude", NOW + 161))
        self.assertEqual(THROTTLED, self.c._live_state["claude"]["kind"])

    def test_window_reset_triggers_an_early_fetch_unless_failing(self):
        self.c.live["claude"] = result(NOW - 3600, five_hour_reset=NOW - 30)
        self.c._live_state["claude"]["next"] = NOW + 100
        self.assertTrue(self.c._live_due("claude", NOW))
        self.fail("claude", TRANSIENT)
        self.assertFalse(self.c._live_due("claude", NOW))

    def test_snapshot_flags_old_limits_even_without_an_error(self):
        self.c.live["claude"] = result(NOW - STALE_SECONDS - 1, five_hour_reset=NOW - 120)
        self.c.claude_limits.read = lambda: None
        with patch("tokmon.collector.time.time", return_value=NOW):
            snap = self.c.snapshot()
        lim = snap["claude"]["limits"]
        self.assertTrue(lim["stale"])
        self.assertTrue(lim["five_hour"]["expired"])
        self.assertEqual(0, lim["five_hour"]["used_percent"])
        self.assertIsNone(snap["live"]["claude"]["error"])
        self.assertIn("next_attempt", snap["live"]["claude"])

    def test_live_error_kinds_come_from_http_status(self):
        import io
        import urllib.error
        from tokmon import live

        def raising(code):
            def opener(req, timeout):
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
            return opener

        for code, kind in ((429, THROTTLED), (401, AUTH), (500, TRANSIENT)):
            with patch.object(live.urllib.request, "urlopen", raising(code)):
                with self.assertRaises(LiveError) as ctx:
                    live._get_json("https://example.invalid/", {})
            self.assertEqual(kind, ctx.exception.kind, code)
        with patch.object(live.urllib.request, "urlopen", side_effect=OSError("network down")):
            with self.assertRaises(LiveError) as ctx:
                live._get_json("https://example.invalid/", {})
        self.assertEqual(TRANSIENT, ctx.exception.kind)


if __name__ == "__main__":
    unittest.main()
