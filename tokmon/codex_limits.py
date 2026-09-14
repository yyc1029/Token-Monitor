"""Normalize Codex subscription rate-limit buckets.

Codex can emit more than one named bucket for a response.  The canonical
``codex`` bucket is the account allowance; feature/model buckets must not
replace it just because they were written a few milliseconds later.
"""

from __future__ import annotations


def bucket_priority(limit_id):
    """Return the display priority of a Codex rate-limit bucket."""
    if limit_id == "codex":
        return 2
    return 1


def normalize_windows(limits):
    """Place windows by their actual duration, not primary/secondary order."""
    if not limits:
        return limits
    out = dict(limits)
    candidates = [out.get("five_hour"), out.get("seven_day")]
    out["five_hour"] = None
    out["seven_day"] = None

    unclassified = []
    for window in candidates:
        if not window:
            continue
        minutes = window.get("window_minutes")
        if minutes is not None and minutes <= 24 * 60:
            out["five_hour"] = window
        elif minutes is not None and minutes > 24 * 60:
            out["seven_day"] = window
        else:
            unclassified.append(window)

    # Backward compatibility for old cache records that did not include a
    # duration.  Fill the conventional short window first, then the weekly one.
    for window in unclassified:
        key = "five_hour" if out["five_hour"] is None else "seven_day"
        if out[key] is None:
            out[key] = window
    return out
