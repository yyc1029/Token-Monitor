"""Background collector: tails both sources and builds dashboard snapshots."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

from .claude_source import ClaudeLimits, ClaudeUsage
from .codex_source import CodexUsage
from .live import FETCHERS, LiveError, merge_limits

LIVE_CACHE_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenMonitor", "live-cache.json")
RETENTION_SECONDS = 8 * 86400
ACTIVE_SESSION_SECONDS = 30 * 60
BURN_WINDOW_SECONDS = 15 * 60
SERIES_HOURS = 6
SERIES_BUCKET = 5 * 60

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8787,
    "poll_interval": 2.0,
    "codex_prices": {},
    "claude_prices": {},
    # Ask the CLIs' own usage endpoints directly (tokens already on disk, sent only
    # to their issuer). Without this the percentages only move when the CLI feels
    # like refreshing its cache.
    # Both endpoints throttle eager pollers (Anthropic: 429 at ~1/min; chatgpt.com:
    # Cloudflare challenges). 3 min for Claude is fresh enough for a 5-hour window;
    # Codex writes fresh limits itself after every turn, so 5 min is plenty.
    "live_usage": {"claude": True, "codex": True, "claude_interval": 180, "codex_interval": 300},
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (OSError, ValueError):
            pass
    return cfg


def _empty_agg():
    return {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
            "total": 0, "cost": 0.0, "requests": 0}


def _add(agg, ev):
    agg["input"] += ev["input"]
    agg["cache_read"] += ev["cache_read"]
    agg["cache_write"] += ev["cache_write"]
    agg["output"] += ev["output"]
    agg["total"] += ev["input"] + ev["cache_read"] + ev["cache_write"] + ev["output"]
    agg["cost"] += ev["cost"]
    agg["requests"] += 1
    return agg


def _local_midnight(now):
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Collector:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.events = {"claude": [], "codex": []}
        self.claude_limits = ClaudeLimits()
        codex_prices = {k: tuple(v) for k, v in (config.get("codex_prices") or {}).items()}
        self.claude_usage = ClaudeUsage()
        self.codex_usage = CodexUsage(price_table=codex_prices)
        if config.get("claude_prices"):
            from . import pricing
            pricing.CLAUDE_PRICES.update({k: tuple(v) for k, v in config["claude_prices"].items()})
        self.last_poll = 0.0
        self.last_error = None
        self.started_at = time.time()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)

        live_cfg = dict(DEFAULT_CONFIG["live_usage"])
        live_cfg.update(config.get("live_usage") or {})
        self.live_enabled = {k: bool(live_cfg.get(k)) for k in FETCHERS}
        base = max(60.0, float(live_cfg.get("interval", 180)))
        self.live_interval = {k: max(60.0, float(live_cfg.get(f"{k}_interval", base))) for k in FETCHERS}
        # Last good live result per source, persisted so a restart during a
        # rate-limit backoff does not regress to the CLI's older cache.
        self.live, self._live_next = self._load_live_cache()
        self.live_error = {k: None for k in FETCHERS}
        self._live_fail = {k: 0 for k in FETCHERS}      # consecutive failures -> exponential backoff
        self._live_thread = threading.Thread(target=self._live_loop, name="live-usage", daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.poll_once()
        self._thread.start()
        if any(self.live_enabled.values()):
            self._live_thread.start()

    def stop(self):
        self._stop.set()

    def _load_live_cache(self):
        """Last good result per source + the time each endpoint may next be asked."""
        results = {k: None for k in FETCHERS}
        nxt = {k: 0.0 for k in FETCHERS}
        try:
            with open(LIVE_CACHE_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            for k in FETCHERS:
                if isinstance(saved.get(k), dict) and saved[k].get("fetched_at"):
                    results[k] = saved[k]
                nxt[k] = float((saved.get("_next") or {}).get(k) or 0.0)
        except (OSError, ValueError):
            pass
        return results, nxt

    def _save_live_cache(self):
        try:
            os.makedirs(os.path.dirname(LIVE_CACHE_FILE), exist_ok=True)
            with self.lock:
                data = dict(self.live)
            data["_next"] = dict(self._live_next)
            tmp = LIVE_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, LIVE_CACHE_FILE)
        except OSError:
            pass

    def _live_loop(self):
        while not self._stop.is_set():
            now = time.time()
            for src, fetch in FETCHERS.items():
                if not self.live_enabled[src] or now < self._live_next[src]:
                    continue
                try:
                    result = fetch()
                    with self.lock:
                        self.live[src] = result
                        self.live_error[src] = None
                    self._live_fail[src] = 0
                    self._live_next[src] = now + self.live_interval[src]
                except LiveError as exc:
                    # exponential backoff on repeated failures so we stop feeding
                    # the throttle: retry_after x 1, 2, 4 ... capped at one hour
                    self._live_fail[src] += 1
                    wait = min(3600, max(exc.retry_after, self.live_interval[src]) * 2 ** (self._live_fail[src] - 1))
                    with self.lock:
                        self.live_error[src] = f"{exc} · 下次 {int(wait // 60)} 分後"
                    self._live_next[src] = now + wait
                except Exception as exc:  # never let one bad response kill the loop
                    with self.lock:
                        self.live_error[src] = f"{type(exc).__name__}: {exc}"
                    self._live_next[src] = now + 300
                self._save_live_cache()
            self._stop.wait(5)

    def _loop(self):
        interval = float(self.config.get("poll_interval", 2.0))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # keep the thread alive no matter what
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(interval)

    def poll_once(self):
        new_claude = list(self.claude_usage.poll())
        new_codex = list(self.codex_usage.poll())
        cutoff = time.time() - RETENTION_SECONDS
        with self.lock:
            self.events["claude"].extend(new_claude)
            self.events["codex"].extend(new_codex)
            for key in self.events:
                if self.events[key] and self.events[key][0]["ts"] < cutoff:
                    self.events[key] = [e for e in self.events[key] if e["ts"] >= cutoff]
            self.last_poll = time.time()
        self.claude_usage.prune_seen()
        self.codex_usage.prune_seen()

    # -- snapshot ----------------------------------------------------------
    def snapshot(self):
        now = time.time()
        with self.lock:
            claude_events = list(self.events["claude"])
            codex_events = list(self.events["codex"])
            live = dict(self.live)
            live_error = dict(self.live_error)
        claude_limits = merge_limits(self.claude_limits.read(), live["claude"])
        codex_limits = merge_limits(self.codex_usage.limits, live["codex"])
        return {
            "now": now,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "live": {k: {"enabled": self.live_enabled[k], "error": live_error[k],
                         "interval": self.live_interval[k]} for k in FETCHERS},
            "claude": self._source_snapshot(claude_events, claude_limits, now),
            "codex": self._source_snapshot(codex_events, codex_limits, now),
        }

    @staticmethod
    def _expire_windows(limits, now):
        """A window whose resets_at has passed is 0 % until the next fetch says otherwise."""
        if not limits:
            return limits
        out = dict(limits)
        for key in ("five_hour", "seven_day"):
            w = out.get(key)
            if w and w.get("resets_at") and w["resets_at"] < now - 60 and (w.get("used_percent") or 0) > 0:
                out[key] = {**w, "used_percent": 0, "expired": True}
        return out

    def _source_snapshot(self, events, limits, now):
        limits = self._expire_windows(limits, now)
        events.sort(key=lambda e: e["ts"])
        midnight = _local_midnight(now)
        windows = {
            "5h": (now - 5 * 3600, _empty_agg()),
            "today": (midnight, _empty_agg()),
            "7d": (now - 7 * 86400, _empty_agg()),
        }
        burn = _empty_agg()
        models_today = defaultdict(_empty_agg)
        sessions = {}
        series_start = now - SERIES_HOURS * 3600
        series = defaultdict(lambda: [0, 0.0])

        for ev in events:
            ts = ev["ts"]
            for start, agg in windows.values():
                if ts >= start:
                    _add(agg, ev)
            if ts >= now - BURN_WINDOW_SECONDS:
                _add(burn, ev)
            if ts >= midnight:
                _add(models_today[ev["model"] or "unknown"], ev)
            if ts >= series_start:
                bucket = int(ts // SERIES_BUCKET * SERIES_BUCKET)
                series[bucket][0] += ev["input"] + ev["cache_read"] + ev["cache_write"] + ev["output"]
                series[bucket][1] += ev["cost"]
            if ts >= now - ACTIVE_SESSION_SECONDS and ev["session"]:
                s = sessions.get(ev["session"])
                if s is None:
                    s = sessions[ev["session"]] = {
                        "session": ev["session"],
                        "project": ev["project"],
                        "model": ev["model"],
                        "first_ts": ts,
                        "last_ts": ts,
                        "context": 0,
                        "context_window": None,
                        "agg": _empty_agg(),
                    }
                _add(s["agg"], ev)
                s["last_ts"] = ts
                if not ev["sidechain"]:
                    s["context"] = ev["context"]
                    s["model"] = ev["model"] or s["model"]
                if ev.get("context_window"):
                    s["context_window"] = ev["context_window"]

        session_list = sorted(sessions.values(), key=lambda s: s["last_ts"], reverse=True)
        for s in session_list:
            s.update(s.pop("agg"))

        model_list = sorted(
            ({"model": m, **agg} for m, agg in models_today.items()),
            key=lambda x: x["cost"] if x["cost"] else x["total"], reverse=True,
        )
        series_list = [[b, v[0], v[1]] for b, v in sorted(series.items())]
        return {
            "limits": limits,
            "windows": {k: v[1] for k, v in windows.items()},
            "burn": {
                "tokens_per_min": burn["total"] / (BURN_WINDOW_SECONDS / 60),
                "cost_per_hour": burn["cost"] / (BURN_WINDOW_SECONDS / 3600),
                "requests": burn["requests"],
            },
            "models_today": model_list,
            "sessions": session_list,
            "series": series_list,
            "event_count": len(events),
        }
