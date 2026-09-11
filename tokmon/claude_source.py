"""Claude Code data: real rate-limit utilization + per-request token usage."""

from __future__ import annotations

import json
import os
from datetime import datetime

from .pricing import claude_cost
from .tailer import JsonlTailer

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ClaudeLimits:
    """Reads ~/.claude.json -> cachedUsageUtilization when the file changes.

    Claude Code refreshes this cache itself while it is running; we only ever
    read it, so no credentials leave the machine.
    """

    def __init__(self, path=CLAUDE_JSON):
        self.path = path
        self._mtime = None
        self._data = None

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return self._data
        if st.st_mtime == self._mtime and self._data is not None:
            return self._data
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return self._data
        self._mtime = st.st_mtime
        self._data = self._shape(raw)
        return self._data

    @staticmethod
    def _shape(raw):
        cache = raw.get("cachedUsageUtilization") or {}
        util = cache.get("utilization") or {}
        acct = raw.get("oauthAccount") or {}

        def window(key, minutes):
            w = util.get(key)
            if not w:
                return None
            return {
                "used_percent": w.get("utilization"),
                "resets_at": _parse_ts(w.get("resets_at")),
                "window_minutes": minutes,
                "locked_reason": w.get("locked_reason"),
            }

        limits = []
        for l in util.get("limits") or []:
            limits.append({
                "kind": l.get("kind"),
                "group": l.get("group"),
                "percent": l.get("percent"),
                "severity": l.get("severity"),
                "resets_at": _parse_ts(l.get("resets_at")),
                "is_active": l.get("is_active"),
            })
        # Per-model weekly buckets exist on some plans; surface them if present.
        model_windows = {}
        for key in ("seven_day_opus", "seven_day_sonnet"):
            w = util.get(key)
            if w and w.get("utilization") is not None:
                model_windows[key] = {
                    "used_percent": w.get("utilization"),
                    "resets_at": _parse_ts(w.get("resets_at")),
                }
        extra = util.get("extra_usage") or {}
        fetched = cache.get("fetchedAtMs")
        return {
            "fetched_at": fetched / 1000 if fetched else None,
            "five_hour": window("five_hour", 300),
            "seven_day": window("seven_day", 10080),
            "model_windows": model_windows,
            "limits": limits,
            "extra_usage": {
                "enabled": extra.get("is_enabled"),
                "used_credits": extra.get("used_credits"),
                "monthly_limit": extra.get("monthly_limit"),
                "utilization": extra.get("utilization"),
            },
            "plan": acct.get("organizationType"),
            "display_name": acct.get("displayName"),
            "rate_limit_tier": acct.get("organizationRateLimitTier"),
        }


class ClaudeUsage:
    """Tails transcript JSONL files and emits one event per API response."""

    SOURCE = "claude"

    def __init__(self, projects_dir=PROJECTS_DIR, max_age_days=8):
        self.tailer = JsonlTailer([projects_dir], max_age_days=max_age_days)
        self._seen: set[str] = set()
        self.projects_dir = projects_dir

    def _project_of(self, path, obj):
        cwd = obj.get("cwd")
        if cwd:
            return os.path.basename(cwd.rstrip("\\/")) or cwd
        rel = os.path.relpath(path, self.projects_dir)
        return rel.split(os.sep)[0]

    def poll(self):
        for path, mtime, obj in self.tailer.poll():
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            key = msg.get("id") or obj.get("requestId") or obj.get("uuid")
            if not key or key in self._seen:
                continue
            self._seen.add(key)
            ts = _parse_ts(obj.get("timestamp")) or mtime
            model = msg.get("model") or ""
            if model == "<synthetic>":
                continue
            inp = usage.get("input_tokens", 0) or 0
            cr = usage.get("cache_read_input_tokens", 0) or 0
            cc = usage.get("cache_creation_input_tokens", 0) or 0
            cdet = usage.get("cache_creation") or {}
            cw1h = cdet.get("ephemeral_1h_input_tokens", 0) or 0
            cw5m = cdet.get("ephemeral_5m_input_tokens", cc - cw1h) or 0
            out = usage.get("output_tokens", 0) or 0
            yield {
                "ts": ts,
                "source": self.SOURCE,
                "model": model,
                "input": inp,
                "cache_read": cr,
                "cache_write": cc,
                "output": out,
                "context": inp + cr + cc,
                "context_window": None,
                "cost": claude_cost(model, inp, cr, cw5m, cw1h, out),
                "session": obj.get("sessionId") or "",
                "project": self._project_of(path, obj),
                "sidechain": bool(obj.get("isSidechain")),
                "path": path,
            }

    def prune_seen(self):
        """Keep the dedupe set bounded on very long-running instances."""
        if len(self._seen) > 200_000:
            self._seen.clear()
