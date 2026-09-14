import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tokmon import claude_source
from tokmon.claude_source import ClaudeUsage
from tokmon.collector import Collector


class ClaudeRuntimeSessionTests(unittest.TestCase):
    def test_reads_busy_no_persistence_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "123.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "pid": 123,
                    "sessionId": "live-session",
                    "cwd": r"C:\work\project",
                    "name": "benchmark-run",
                    "startedAt": 900000,
                    "updatedAt": 950000,
                    "status": "busy",
                }, fh)
            with patch.object(claude_source, "RUNTIME_SESSIONS_DIR", tmp):
                rows = ClaudeUsage(projects_dir=tmp).runtime_sessions()
        self.assertEqual(1, len(rows))
        self.assertEqual("live-session", rows[0]["session"])
        self.assertTrue(rows[0]["runtime"])
        self.assertEqual("busy", rows[0]["status"])
        self.assertNotIn("pid", rows[0])

    def test_runtime_session_is_merged_without_fake_tokens(self):
        runtime = [{
            "session": "live-session", "project": "run", "model": "",
            "first_ts": 1_799_999_900, "last_ts": 1_799_999_950, "context": 0,
            "context_window": None, "input": 0, "cache_read": 0,
            "cache_write": 0, "output": 0, "total": 0, "cost": 0.0,
            "requests": 0, "runtime": True, "status": "busy",
        }]
        collector = Collector.__new__(Collector)
        snap = collector._source_snapshot([], None, 1_800_000_000, runtime)
        self.assertEqual(1, len(snap["sessions"]))
        self.assertEqual(0, snap["sessions"][0]["total"])
        self.assertTrue(snap["sessions"][0]["runtime"])


if __name__ == "__main__":
    unittest.main()
