"""Codex CLI / app data: real rate limits + per-response token usage."""

from __future__ import annotations

import os
from datetime import datetime

from .pricing import codex_cost
from .tailer import JsonlTailer

HOME = os.path.expanduser("~")
CODEX_DIR = os.path.join(HOME, ".codex")
SESSIONS_DIR = os.path.join(CODEX_DIR, "sessions")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CodexUsage:
    """Tails rollout-*.jsonl files.

    Two record kinds matter:
      * token_usage_record    -> one per API response (response_id -> dedupe)
      * event_msg/token_count -> carries rate_limits (real used_percent, resets_at)
    The most recent rate_limits seen across every session wins.
    """

    SOURCE = "codex"

    def __init__(self, sessions_dir=SESSIONS_DIR, max_age_days=8, price_table=None):
        self.tailer = JsonlTailer([sessions_dir], max_age_days=max_age_days)
        self._seen: set[str] = set()
        self._model_by_thread: dict[str, str] = {}
        self._ctx_window_by_thread: dict[str, int] = {}
        self._cwd_by_thread: dict[str, str] = {}
        self.price_table = price_table
        self.limits = None
        self.limits_ts = 0.0

    @staticmethod
    def _shape_limits(rl, ts):
        def window(w):
            if not w:
                return None
            return {
                "used_percent": w.get("used_percent"),
                "window_minutes": w.get("window_minutes"),
                "resets_at": w.get("resets_at"),
            }
        credits = rl.get("credits") or {}
        return {
            "fetched_at": ts,
            "five_hour": window(rl.get("primary")),
            "seven_day": window(rl.get("secondary")),
            "plan": rl.get("plan_type"),
            "limit_id": rl.get("limit_id"),
            "rate_limit_reached": rl.get("rate_limit_reached_type"),
            "credits": {
                "has_credits": credits.get("has_credits"),
                "unlimited": credits.get("unlimited"),
                "balance": credits.get("balance"),
            },
        }

    @staticmethod
    def _thread_from_path(path):
        # rollout-2026-09-11T16-30-37-<uuid>.jsonl -> <uuid>
        name = os.path.basename(path)
        if name.startswith("rollout-") and name.endswith(".jsonl"):
            return name[len("rollout-") + 20:-len(".jsonl")]
        return name

    def poll(self):
        for path, mtime, obj in self.tailer.poll():
            t = obj.get("type")
            payload = obj.get("payload") or {}
            ts = _parse_ts(obj.get("timestamp")) or mtime
            thread = self._thread_from_path(path)

            if t == "session_meta":
                cwd = payload.get("cwd")
                if cwd:
                    self._cwd_by_thread[thread] = os.path.basename(cwd.rstrip("\\/")) or cwd
                continue

            if t == "turn_context":
                if payload.get("model"):
                    self._model_by_thread[thread] = payload["model"]
                continue

            if t == "event_msg" and payload.get("type") == "token_count":
                rl = payload.get("rate_limits")
                if rl and ts >= self.limits_ts:
                    self.limits = self._shape_limits(rl, ts)
                    self.limits_ts = ts
                info = payload.get("info") or {}
                cw = info.get("model_context_window")
                if cw:
                    self._ctx_window_by_thread[thread] = cw
                continue

            if t != "token_usage_record":
                continue
            rid = payload.get("response_id") or payload.get("turn_id")
            if not rid or rid in self._seen:
                continue
            self._seen.add(rid)
            u = payload.get("usage") or {}
            thread = payload.get("thread_id") or thread
            model = self._model_by_thread.get(thread, "")
            total_in = u.get("input_tokens", 0) or 0
            cached = u.get("cached_input_tokens", 0) or 0
            cw = u.get("cache_write_input_tokens", 0) or 0
            out = u.get("output_tokens", 0) or 0
            uncached = max(total_in - cached, 0)
            yield {
                "ts": ts,
                "source": self.SOURCE,
                "model": model,
                "input": uncached,
                "cache_read": cached,
                "cache_write": cw,
                "output": out,
                "reasoning": u.get("reasoning_output_tokens", 0) or 0,
                "context": total_in,
                "context_window": self._ctx_window_by_thread.get(thread),
                "cost": codex_cost(model, uncached, cached, cw, out, self.price_table),
                "session": thread,
                "project": self._cwd_by_thread.get(thread, ""),
                "sidechain": False,
                "path": path,
            }

    def prune_seen(self):
        if len(self._seen) > 200_000:
            self._seen.clear()
