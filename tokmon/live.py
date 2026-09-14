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

from .codex_app_server import AppServerError, read_rate_limits
from .codex_limits import normalize_windows

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


# Why a fetch failed decides how we retry.  Only "throttled" earns the long
# exponential backoff; the endpoint told us to go away and asking again sooner
# just extends the block.  A dropped network (sleep, Wi-Fi, VPN) is "transient"
# and must recover within minutes, not hours.  "auth" waits for the CLI to
# refresh its token and retries at a fixed pace.
THROTTLED, AUTH, TRANSIENT = "throttled", "auth", "transient"
MAX_THROTTLE_WAIT = 3600
MAX_TRANSIENT_WAIT = 600


class LiveError(Exception):
    def __init__(self, message, retry_after, kind=TRANSIENT):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind


def backoff_seconds(kind, retry_after, interval, fails):
    """Seconds to wait after the `fails`-th consecutive failure of one kind."""
    fails = max(1, int(fails))
    if kind == THROTTLED:
        return min(MAX_THROTTLE_WAIT, max(retry_after, interval) * 2 ** (fails - 1))
    if kind == AUTH:
        return max(60, retry_after)
    return min(MAX_TRANSIENT_WAIT, max(60, retry_after) * 2 ** (fails - 1))


def _get_json(url, headers, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        ra = int(ra) if ra and ra.isdigit() else 0
        if e.code == 429:
            raise LiveError("HTTP 429 (rate limited)", max(600, ra), THROTTLED) from None
        if e.code in (401, 403):
            raise LiveError(f"HTTP {e.code}", max(600, ra), AUTH) from None
        raise LiveError(f"HTTP {e.code}", max(120, ra), TRANSIENT) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # includes socket.timeout, DNS failure, connection refused - the network
        # is missing, not the endpoint's goodwill
        raise LiveError(f"{type(e).__name__}: {e}", 60, TRANSIENT) from None


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
        raise LiveError("no Claude credentials on disk", 600, AUTH) from None
    token = cred.get("accessToken")
    if not token:
        raise LiveError("Claude credentials have no access token", 600, AUTH)
    exp = cred.get("expiresAt")
    if exp and exp / 1000 < time.time():
        raise LiveError("Claude access token expired; run Claude Code to refresh", 300, AUTH)

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


def _shape_codex_app_server(body):
    """Translate the native camelCase app-server response to dashboard shape."""
    buckets = body.get("rateLimitsByLimitId") or {}
    rl = buckets.get("codex") or body.get("rateLimits") or {}

    def window(value):
        if not value:
            return None
        return {
            "used_percent": value.get("usedPercent"),
            "window_minutes": value.get("windowDurationMins"),
            "resets_at": value.get("resetsAt"),
        }

    credits = rl.get("credits") or {}
    return normalize_windows({
        "fetched_at": time.time(),
        "five_hour": window(rl.get("primary")),
        "seven_day": window(rl.get("secondary")),
        "plan": rl.get("planType"),
        "limit_id": rl.get("limitId") or "codex",
        "rate_limit_reached": rl.get("rateLimitReachedType"),
        "limit_reached": bool(rl.get("rateLimitReachedType")),
        "credits": {
            "has_credits": credits.get("hasCredits"),
            "unlimited": credits.get("unlimited"),
            "balance": credits.get("balance"),
        },
        "source_detail": "codex_app_server",
    })


def fetch_codex():
    """Return limits shaped like CodexUsage.limits, or raise LiveError."""
    try:
        return _shape_codex_app_server(read_rate_limits())
    except AppServerError as exc:
        # A brief local app-server problem must not become a one-hour web
        # endpoint backoff.  Retry it as a transient failure instead.
        if "executable not found" not in str(exc):
            raise LiveError(f"Codex app-server: {exc}", 60, TRANSIENT) from None

    # Compatibility fallback for machines with an older Codex installation.
    try:
        with open(CODEX_AUTH, "r", encoding="utf-8") as fh:
            auth = json.load(fh)
    except (OSError, ValueError):
        raise LiveError("no Codex auth on disk", 600, AUTH) from None
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise LiveError("Codex auth has no access token (API-key mode?)", 600, AUTH)
    headers = {"Authorization": f"Bearer {token}"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]

    try:
        body = _get_json(CODEX_USAGE_URL, headers)
    except LiveError as exc:
        if exc.args[0].startswith("HTTP 403"):
            # Cloudflare's bot check, not a bad token: a throttle in all but name.
            raise LiveError("HTTP 403 (Cloudflare challenge; using CLI cache)", 900, THROTTLED) from None
        raise
    rl = body.get("rate_limit") or {}

    def window(w):
        if not w:
            return None
        secs = w.get("limit_window_seconds")
        return {"used_percent": w.get("used_percent"), "window_minutes": secs // 60 if secs else None,
                "resets_at": w.get("reset_at")}

    credits = body.get("credits") or {}
    return normalize_windows({
        "fetched_at": time.time(),
        "five_hour": window(rl.get("primary_window")),
        "seven_day": window(rl.get("secondary_window")),
        "plan": body.get("plan_type"),
        "limit_id": "codex",
        "rate_limit_reached": body.get("rate_limit_reached_type"),
        "limit_reached": rl.get("limit_reached"),
        "credits": {"has_credits": credits.get("has_credits"), "unlimited": credits.get("unlimited"),
                    "balance": credits.get("balance")},
    })


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


def window_expired(result, now):
    """True when a fetched result has a window whose reset time has passed since
    the fetch - the moment the percentages are guaranteed to be wrong."""
    if not result or not result.get("fetched_at"):
        return False
    for key in ("five_hour", "seven_day"):
        w = result.get(key)
        ra = w.get("resets_at") if w else None
        if ra and result["fetched_at"] < ra < now:
            return True
    return False


def restore_live_state(saved, now):
    """Decide what a restart inherits from the persisted live cache.

    The last good result is always kept - it beats the CLI's older cache.  A
    pending backoff is kept only when the endpoint itself asked us to wait
    (throttled) and the wait has not run out; anything else (network gone,
    token expired, an old-format file) is retried straight away so a restart
    never sits on stale data for an hour with no explanation.
    """
    results = {k: None for k in FETCHERS}
    state = {k: {"next": 0.0, "fails": 0, "kind": None, "error": None, "attempted_at": None} for k in FETCHERS}
    if not isinstance(saved, dict):
        return results, state
    for k in FETCHERS:
        if isinstance(saved.get(k), dict) and saved[k].get("fetched_at"):
            results[k] = saved[k]
        st = (saved.get("_state") or {}).get(k)
        if not isinstance(st, dict):
            continue
        nxt = float(st.get("next") or 0.0)
        if st.get("kind") == THROTTLED and nxt > now:
            state[k] = {"next": min(nxt, now + MAX_THROTTLE_WAIT), "fails": int(st.get("fails") or 1),
                        "kind": THROTTLED, "error": st.get("error"), "attempted_at": st.get("attempted_at")}
    return results, state


def merge_limits(cached, live):
    """Prefer whichever of the two was fetched more recently; tag the winner.

    A fresher record that lacks a window (a CLI cache entry with nulls, say)
    never hides an older record that has it - missing windows are filled from
    the other side.
    """
    if not cached and not live:
        return None
    live_newer = bool(live) and (not cached or (live.get("fetched_at") or 0) >= (cached.get("fetched_at") or 0))
    winner, loser = (live, cached) if live_newer else (cached, live)
    out = dict(loser or {})
    out.update({k: v for k, v in winner.items() if v is not None or k not in out})
    for key in ("five_hour", "seven_day"):
        if not out.get(key) and loser and loser.get(key):
            out[key] = loser[key]
    out["source"] = "live" if live_newer else "cache"
    return out
