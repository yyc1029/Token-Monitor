"""Live rate-limit fetchers.

The local caches both CLIs write are only refreshed when the CLI feels like it
(Claude Code: rarely; Codex: at the end of each turn), so the dashboard would
otherwise sit on stale percentages for hours.  These fetchers ask the same
endpoints the CLIs themselves use, with the same tokens already on disk.

Tokens are read fresh from disk on every call (the CLIs rotate them) and are
sent only to their issuer: api.anthropic.com for Claude, chatgpt.com for Codex.
Nothing is written back to the credential files.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
CLAUDE_CREDENTIALS = os.path.join(HOME, ".claude", ".credentials.json")
CODEX_AUTH = os.path.join(HOME, ".codex", "auth.json")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
# chatgpt.com sits behind Cloudflare, which intermittently answers plain HTTP
# clients with a 403 "challenge" page. That is a bot-score thing, not the UA,
# so we keep a plain UA, poll Codex slowly and back off on 403 - the rollout
# cache is fresh whenever Codex is actually being used anyway.
USER_AGENT = "TokenMonitor/1.0"


class LiveError(Exception):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


def _get_json(url, headers, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 401/403: token expired or revoked -> wait for the CLI to refresh it.
        # 429: we are polling faster than the endpoint likes -> honour Retry-After,
        # else back off hard; the last good value stays on screen meanwhile.
        retry = 600 if e.code in (401, 403) else 600 if e.code == 429 else 120
        ra = e.headers.get("Retry-After") if e.headers else None
        if ra and ra.isdigit():
            retry = max(retry, int(ra))
        raise LiveError(f"HTTP {e.code}" + (" (rate limited)" if e.code == 429 else ""), retry) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LiveError(f"{type(e).__name__}: {e}", 120) from None


def _parse_iso(s):
    if not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_claude():
    """Return limits shaped like ClaudeLimits.read(), or raise LiveError."""
    try:
        with open(CLAUDE_CREDENTIALS, "r", encoding="utf-8") as fh:
            cred = json.load(fh).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        raise LiveError("no Claude credentials on disk", 600) from None
    token = cred.get("accessToken")
    if not token:
        raise LiveError("Claude credentials have no access token", 600)
    exp = cred.get("expiresAt")
    if exp and exp / 1000 < time.time():
        raise LiveError("Claude access token expired; run Claude Code to refresh", 300)

    body = _get_json(CLAUDE_USAGE_URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    now = time.time()

    def window(key, minutes):
        w = body.get(key)
        if not w:
            return None
        return {"used_percent": w.get("utilization"), "resets_at": _parse_iso(w.get("resets_at")),
                "window_minutes": minutes, "locked_reason": w.get("locked_reason")}

    extra = body.get("extra_usage") or {}
    return {
        "fetched_at": now,
        "five_hour": window("five_hour", 300),
        "seven_day": window("seven_day", 10080),
        "model_windows": {k: {"used_percent": v.get("utilization"), "resets_at": _parse_iso(v.get("resets_at"))}
                          for k in ("seven_day_opus", "seven_day_sonnet")
                          if (v := body.get(k)) and v.get("utilization") is not None},
        "limits": [{"kind": l.get("kind"), "group": l.get("group"), "percent": l.get("percent"),
                    "severity": l.get("severity"), "resets_at": _parse_iso(l.get("resets_at")),
                    "is_active": l.get("is_active")} for l in body.get("limits") or []],
        "extra_usage": {
            "enabled": extra.get("is_enabled"),
            "used_credits": extra.get("used_credits"),
            "monthly_limit": extra.get("monthly_limit"),
            "utilization": extra.get("utilization"),
            "currency": extra.get("currency"),
            "decimal_places": extra.get("decimal_places"),
            "spend_limit_reached": extra.get("spend_limit_reached"),
        },
        "rate_limit_tier": cred.get("rateLimitTier"),
    }


def fetch_codex():
    """Return limits shaped like CodexUsage.limits, or raise LiveError."""
    try:
        with open(CODEX_AUTH, "r", encoding="utf-8") as fh:
            auth = json.load(fh)
    except (OSError, ValueError):
        raise LiveError("no Codex auth on disk", 600) from None
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise LiveError("Codex auth has no access token (API-key mode?)", 600)
    headers = {"Authorization": f"Bearer {token}"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]

    try:
        body = _get_json(CODEX_USAGE_URL, headers)
    except LiveError as exc:
        if exc.args[0].startswith("HTTP 403"):
            raise LiveError("HTTP 403 (Cloudflare challenge; using CLI cache)", 900) from None
        raise
    rl = body.get("rate_limit") or {}

    def window(w):
        if not w:
            return None
        secs = w.get("limit_window_seconds")
        return {"used_percent": w.get("used_percent"), "window_minutes": secs // 60 if secs else None,
                "resets_at": w.get("reset_at")}

    credits = body.get("credits") or {}
    return {
        "fetched_at": time.time(),
        "five_hour": window(rl.get("primary_window")),
        "seven_day": window(rl.get("secondary_window")),
        "plan": body.get("plan_type"),
        "limit_id": "codex",
        "rate_limit_reached": body.get("rate_limit_reached_type"),
        "limit_reached": rl.get("limit_reached"),
        "credits": {"has_credits": credits.get("has_credits"), "unlimited": credits.get("unlimited"),
                    "balance": credits.get("balance")},
    }


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


def merge_limits(cached, live):
    """Prefer whichever of the two was fetched more recently; tag the winner."""
    if live and (not cached or (live.get("fetched_at") or 0) >= (cached.get("fetched_at") or 0)):
        out = dict(cached or {})
        out.update({k: v for k, v in live.items() if v is not None or k not in out})
        out["source"] = "live"
        return out
    if cached:
        out = dict(cached)
        out["source"] = "cache"
        return out
    return None
