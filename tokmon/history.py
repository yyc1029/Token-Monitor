"""Persistent per-day usage totals for the activity heatmap.

The collector only keeps 8 days of raw events in memory, so a year-long view
needs its own store. Days are keyed by local date ("YYYY-MM-DD") and always
written with *replace* semantics (a whole day's totals at once), so the full
backfill scan and the live collector can both write without double counting:
whichever ran last simply has the same complete numbers for that day.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

HISTORY_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenMonitor", "daily-history.json")
KEEP_DAYS = 400
SAVE_INTERVAL = 30.0
SOURCES = ("claude", "codex")
FIELDS = ("input", "cache_read", "cache_write", "output", "total", "cost", "requests")


def day_key(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_start(key):
    return datetime.strptime(key, "%Y-%m-%d").timestamp()


class DailyHistory:
    def __init__(self, path=HISTORY_FILE):
        self.path = path
        self.lock = threading.Lock()
        self.days = {s: {} for s in SOURCES}
        self.scanned_at = {s: None for s in SOURCES}   # last completed full backfill
        self.saved_at = 0.0
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        for s in SOURCES:
            days = raw.get("days", {}).get(s)
            if isinstance(days, dict):
                self.days[s] = {k: v for k, v in days.items() if isinstance(v, dict)}
            self.scanned_at[s] = (raw.get("scanned_at") or {}).get(s)
        self.saved_at = float(raw.get("saved_at") or 0.0)

    def save(self, force=False):
        now = time.time()
        with self.lock:
            if not self._dirty and not force:
                return
            if not force and now - self._last_save < SAVE_INTERVAL:
                return
            self._prune()
            data = {"saved_at": now, "scanned_at": dict(self.scanned_at),
                    "days": {s: dict(self.days[s]) for s in SOURCES}}
            self._dirty = False
            self._last_save = now
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.saved_at = now
        except OSError:
            pass

    def _prune(self):
        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        for s in SOURCES:
            for k in [k for k in self.days[s] if k < cutoff]:
                del self.days[s][k]

    # -- writes --------------------------------------------------------------
    @staticmethod
    def _shape(agg):
        out = {f: agg.get(f, 0) for f in FIELDS}
        out["cost"] = round(float(out["cost"]), 6)
        return out

    def replace_days(self, source, mapping, clear_keys=()):
        """Overwrite whole days. ``clear_keys`` are days known to be empty."""
        with self.lock:
            days = self.days[source]
            for k in clear_keys:
                if k not in mapping and k in days:
                    del days[k]
                    self._dirty = True
            for k, agg in mapping.items():
                shaped = self._shape(agg)
                if days.get(k) != shaped:
                    days[k] = shaped
                    self._dirty = True

    def mark_scanned(self, source, ts):
        with self.lock:
            self.scanned_at[source] = ts
            self._dirty = True

    def needs_backfill(self, source, retention_seconds):
        """A full rescan is due when never done, or when the store slept longer
        than the live window (days in that gap were never captured)."""
        if not self.scanned_at[source]:
            return True
        return self.saved_at < time.time() - (retention_seconds - 86400)

    # -- reads ---------------------------------------------------------------
    def export(self):
        with self.lock:
            return {
                "days": {s: dict(self.days[s]) for s in SOURCES},
                "scanned_at": dict(self.scanned_at),
            }
