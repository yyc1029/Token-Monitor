"""Background collector: tails both sources and builds dashboard snapshots."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from .claude_source import ClaudeLimits, ClaudeUsage
from .codex_limits import normalize_windows
from .codex_source import CodexUsage
from .history import DailyHistory, day_key, day_start
from .live import (FETCHERS, THROTTLED, LiveError, backoff_seconds, merge_limits, restore_live_state,
                   window_expired)

log = logging.getLogger("tokmon.collector")

LIVE_CACHE_FILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TokenMonitor", "live-cache.json")
RETENTION_SECONDS = 8 * 86400
# A turn can run for hours without writing a completed assistant response.
# Keep recent sessions visible long enough to survive those quiet stretches;
# the UI still shows each session's exact last-activity age.
ACTIVE_SESSION_SECONDS = 8 * 60 * 60
BURN_WINDOW_SECONDS = 15 * 60
SERIES_HOURS = 6
SERIES_BUCKET = 5 * 60
# Limits older than this are flagged on every surface, whatever the reason.
STALE_SECONDS = 30 * 60
# The live loop sleeps 5 s; a longer gap between two ticks means the machine
# was asleep (Modern Standby included) and every pending backoff is void.
WAKE_GAP_SECONDS = 30

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8787,
    "poll_interval": 2.0,
    "codex_prices": {},
    "claude_prices": {},
    "discord": {"enabled": False},
    # Ask the CLIs' own usage endpoints directly (tokens already on disk, sent only
    # to their issuer). Without this the percentages only move when the CLI feels
    # like refreshing its cache.
    # Claude's web endpoint is conservative; Codex uses its local app-server
    # and can refresh safely about once per minute.
    "live_usage": {"claude": True, "codex": True, "claude_interval": 180, "codex_interval": 60},
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


def _pct(window):
    return None if not window else window.get("used_percent")


def _fmt_wait(seconds):
    return f"{int(seconds // 60)} 分" if seconds >= 60 else f"{int(seconds)} 秒"


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

        # Per-day totals for the year heatmap; outlives the 8-day event window.
        self.history = DailyHistory()
        self.history_error = None
        self._history_next = 0.0
        self._backfill_thread = threading.Thread(target=self._backfill, name="history-backfill", daemon=True)

        live_cfg = dict(DEFAULT_CONFIG["live_usage"])
        live_cfg.update(config.get("live_usage") or {})
        self.live_enabled = {k: bool(live_cfg.get(k)) for k in FETCHERS}
        base = max(60.0, float(live_cfg.get("interval", 180)))
        self.live_interval = {k: max(60.0, float(live_cfg.get(f"{k}_interval", base))) for k in FETCHERS}
        # Last good live result per source, persisted so a restart during a
        # rate-limit backoff does not regress to the CLI's older cache.
        # _live_state[src]: next (earliest next attempt), fails (consecutive
        # failures), kind (why the last attempt failed), error, attempted_at.
        self.live, self._live_state = self._load_live_cache()
        self._live_thread = threading.Thread(target=self._live_loop, name="live-usage", daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.poll_once()
        self._thread.start()
        if any(self.live_enabled.values()):
            self._live_thread.start()
        self._backfill_thread.start()

    def stop(self):
        self._stop.set()
        self.history.save(force=True)

    # -- daily history -----------------------------------------------------
    def _backfill(self):
        """One-off full scan of every transcript on disk (no age limit) so the
        heatmap has data from before this monitor was first started. Runs in
        the background; the live collector keeps the recent days current."""
        makers = {
            "claude": lambda: ClaudeUsage(max_age_days=None),
            "codex": lambda: CodexUsage(max_age_days=None, price_table=self.codex_usage.price_table),
        }
        for src, make in makers.items():
            if self._stop.is_set() or not self.history.needs_backfill(src, RETENTION_SECONDS):
                continue
            try:
                days = defaultdict(_empty_agg)
                for ev in make().poll():
                    _add(days[day_key(ev["ts"])], ev)
                self.history.replace_days(src, days)
                self.history.mark_scanned(src, time.time())
                self.history.save(force=True)
            except Exception as exc:
                self.history_error = f"{src}: {type(exc).__name__}: {exc}"

    def _update_history(self, now):
        """Rewrite every day the in-memory window covers completely."""
        cutoff = now - RETENTION_SECONDS
        with self.lock:
            snapshot = {k: list(v) for k, v in self.events.items()}
        # Days whose local midnight lies inside the window are complete in memory.
        covered = set()
        day = datetime.fromtimestamp(now).date()
        while day_start(day.isoformat()) >= cutoff:
            covered.add(day.isoformat())
            day -= timedelta(days=1)
        for src, events in snapshot.items():
            days = defaultdict(_empty_agg)
            for ev in events:
                k = day_key(ev["ts"])
                if k in covered:
                    _add(days[k], ev)
            self.history.replace_days(src, days, clear_keys=covered)
        self.history.save()

    def _load_live_cache(self):
        """Last good result per source + whatever backoff a restart may inherit."""
        saved = None
        try:
            with open(LIVE_CACHE_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            pass
        results, state = restore_live_state(saved, time.time())
        for k, st in state.items():
            if st["kind"]:
                log.info("live %s: inheriting %s backoff until %s (%s)", k, st["kind"],
                         time.strftime("%H:%M:%S", time.localtime(st["next"])), st["error"])
        return results, state

    def _save_live_cache(self):
        try:
            os.makedirs(os.path.dirname(LIVE_CACHE_FILE), exist_ok=True)
            with self.lock:
                data = dict(self.live)
                data["_state"] = {k: dict(v) for k, v in self._live_state.items()}
            tmp = LIVE_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, LIVE_CACHE_FILE)
        except OSError as exc:
            log.warning("live cache not saved: %s", exc)

    def _on_wake(self, now, gap):
        """The clock jumped: the machine slept. Failures accumulated while the
        network was down say nothing about the endpoints, so forget them.  A
        throttle that was in force keeps a short grace period only."""
        log.info("clock jumped %.0f s (sleep/resume); resetting live backoff", gap)
        with self.lock:
            for src, st in self._live_state.items():
                st["fails"] = 0
                if st["kind"] == THROTTLED:
                    st["next"] = min(st["next"], now + 60)
                else:
                    st["next"] = min(st["next"], now)
                    st["kind"] = None

    def _live_due(self, src, now):
        st = self._live_state[src]
        if now >= st["next"]:
            return True
        # A window reset since the last fetch: the number on screen is wrong by
        # definition, so refresh early unless the endpoint is pushing back.
        return st["fails"] == 0 and window_expired(self.live.get(src), now)

    def _live_loop(self):
        last_tick = time.time()
        while not self._stop.is_set():
            now = time.time()
            if now - last_tick > WAKE_GAP_SECONDS:
                self._on_wake(now, now - last_tick)
            last_tick = now
            for src, fetch in FETCHERS.items():
                if not self.live_enabled[src] or not self._live_due(src, now):
                    continue
                st = self._live_state[src]
                st["attempted_at"] = now
                try:
                    result = fetch()
                    with self.lock:
                        self.live[src] = result
                        st.update({"fails": 0, "kind": None, "error": None, "next": now + self.live_interval[src]})
                    log.info("live %s: ok 5h=%s 7d=%s", src, _pct(result.get("five_hour")), _pct(result.get("seven_day")))
                except LiveError as exc:
                    wait = backoff_seconds(exc.kind, exc.retry_after, self.live_interval[src], st["fails"] + 1)
                    with self.lock:
                        st.update({"fails": st["fails"] + 1, "kind": exc.kind, "next": now + wait,
                                   "error": f"{exc} · 下次 {_fmt_wait(wait)}後"})
                    log.warning("live %s: %s failure #%d: %s; next try in %.0f s", src, exc.kind, st["fails"], exc, wait)
                except Exception as exc:  # never let one bad response kill the loop
                    with self.lock:
                        st.update({"fails": st["fails"] + 1, "kind": "error", "next": now + 300,
                                   "error": f"{type(exc).__name__}: {exc}"})
                    log.exception("live %s: unexpected error", src)
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
        now = time.time()
        if new_claude or new_codex or now >= self._history_next:
            self._history_next = now + 60
            self._update_history(now)

    # -- snapshot ----------------------------------------------------------
    def snapshot(self):
        now = time.time()
        with self.lock:
            claude_events = list(self.events["claude"])
            codex_events = list(self.events["codex"])
            live = dict(self.live)
            live_state = {k: dict(v) for k, v in self._live_state.items()}
        claude_limits = merge_limits(self.claude_limits.read(), live["claude"])
        codex_limits = normalize_windows(merge_limits(self.codex_usage.limits, live["codex"]))
        return {
            "now": now,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "live": {k: {"enabled": self.live_enabled[k], "error": live_state[k]["error"],
                         "error_kind": live_state[k]["kind"], "fails": live_state[k]["fails"],
                         "interval": self.live_interval[k],
                         "last_attempt": live_state[k]["attempted_at"],
                         "next_attempt": live_state[k]["next"] if self.live_enabled[k] else None}
                     for k in FETCHERS},
            "claude": self._source_snapshot(
                claude_events, claude_limits, now, self.claude_usage.runtime_sessions()
            ),
            "codex": self._source_snapshot(codex_events, codex_limits, now),
        }

    @staticmethod
    def _expire_windows(limits, now):
        """A window whose resets_at has passed is 0 % until the next fetch says
        otherwise; it is tagged `expired` so the UI can say "waiting" instead of
        presenting 0 % as a measurement.  `age`/`stale` let every consumer (web,
        pet, Discord) flag old data by one rule."""
        if not limits:
            return limits
        out = dict(limits)
        for key in ("five_hour", "seven_day"):
            w = out.get(key)
            if w and w.get("resets_at") and w["resets_at"] < now - 60 and (w.get("used_percent") or 0) > 0:
                out[key] = {**w, "used_percent": 0, "expired": True}
        age = (now - out["fetched_at"]) if out.get("fetched_at") else None
        out["age"] = age
        out["stale"] = age is None or age > STALE_SECONDS
        return out

    def _source_snapshot(self, events, limits, now, runtime_sessions=None):
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

        # Claude's --no-session-persistence mode never writes token events to
        # projects/*.jsonl while it runs.  Merge its separate process registry
        # so the session remains visible without inventing token counts.
        for runtime in runtime_sessions or []:
            session_id = runtime["session"]
            existing = sessions.get(session_id)
            if existing:
                existing.update({k: v for k, v in runtime.items() if k in ("runtime", "status")})
            else:
                sessions[session_id] = dict(runtime)

        session_list = sorted(sessions.values(), key=lambda s: s["last_ts"], reverse=True)
        for s in session_list:
            if "agg" in s:
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
