import unittest

from tokmon.codex_limits import normalize_windows
from tokmon.codex_source import CodexUsage


def window(minutes, percent):
    return {"window_minutes": minutes, "used_percent": percent, "resets_at": 123}


class CodexLimitTests(unittest.TestCase):
    def test_single_weekly_primary_is_displayed_as_weekly(self):
        limits = normalize_windows({"five_hour": window(10080, 16), "seven_day": None})
        self.assertIsNone(limits["five_hour"])
        self.assertEqual(16, limits["seven_day"]["used_percent"])

    def test_standard_short_and_weekly_windows_remain_in_place(self):
        limits = normalize_windows({"five_hour": window(300, 4), "seven_day": window(10080, 18)})
        self.assertEqual(4, limits["five_hour"]["used_percent"])
        self.assertEqual(18, limits["seven_day"]["used_percent"])

    def test_canonical_bucket_replaces_newer_auxiliary_bucket(self):
        usage = CodexUsage(sessions_dir="missing")
        usage.limits = {"limit_id": "codex_bengalfox"}
        usage.limits_ts = 200
        self.assertTrue(usage._accept_limits({"limit_id": "codex"}, 100))

    def test_auxiliary_bucket_cannot_replace_canonical_bucket(self):
        usage = CodexUsage(sessions_dir="missing")
        usage.limits = {"limit_id": "codex"}
        usage.limits_ts = 100
        self.assertFalse(usage._accept_limits({"limit_id": "codex_bengalfox"}, 200))


if __name__ == "__main__":
    unittest.main()
