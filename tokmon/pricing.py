"""Model price table (USD per 1M tokens) and cost maths.

Claude prices are Anthropic first-party API rates.  Cache writes cost 1.25x the
base input rate on the 5-minute TTL and 2x on the 1-hour TTL; cache reads cost
0.1x the base rate (0.025x on the Fable family).

Codex / OpenAI prices are intentionally left at 0 -- fill them in via
config.json if you want dollar figures for Codex.  Token counts and the real
rate-limit percentages do not depend on them.
"""

# model id -> (input, output, cache_read_multiplier)
CLAUDE_PRICES = {
    "claude-fable-5-1":  (10.0, 50.0, 0.025),
    "claude-mythos-5-1": (10.0, 50.0, 0.025),
    "claude-fable-5":    (10.0, 50.0, 0.025),
    "claude-opus-5":     (5.0,  25.0, 0.1),
    "claude-opus-4-8":   (5.0,  25.0, 0.1),
    "claude-opus-4-7":   (5.0,  25.0, 0.1),
    "claude-opus-4-6":   (5.0,  25.0, 0.1),
    "claude-sonnet-5":   (2.0,  10.0, 0.1),
    "claude-sonnet-4-6": (3.0,  15.0, 0.1),
    "claude-sonnet-4-5": (3.0,  15.0, 0.1),
    "claude-haiku-4-5":  (1.0,   5.0, 0.1),
}

# Prefix fallbacks for ids carrying a date suffix (claude-haiku-4-5-20251001).
_CLAUDE_FALLBACK_ORDER = sorted(CLAUDE_PRICES, key=len, reverse=True)

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# Codex: no public rate baked in. Override in config.json -> codex_prices.
CODEX_PRICES: dict[str, tuple[float, float, float]] = {}


def claude_price(model: str):
    """Return (input, output, cache_read_mult) or None for unknown models."""
    if not model:
        return None
    if model in CLAUDE_PRICES:
        return CLAUDE_PRICES[model]
    for known in _CLAUDE_FALLBACK_ORDER:
        if model.startswith(known):
            return CLAUDE_PRICES[known]
    return None


def claude_cost(model, inp, cache_read, cw5m, cw1h, out):
    p = claude_price(model)
    if p is None:
        return 0.0
    pin, pout, read_mult = p
    return (
        inp * pin
        + cache_read * pin * read_mult
        + cw5m * pin * CACHE_WRITE_5M_MULT
        + cw1h * pin * CACHE_WRITE_1H_MULT
        + out * pout
    ) / 1_000_000


def codex_cost(model, uncached_in, cached_in, cache_write, out, table=None):
    table = table if table is not None else CODEX_PRICES
    p = table.get(model)
    if p is None:
        for known in sorted(table, key=len, reverse=True):
            if model and model.startswith(known):
                p = table[known]
                break
    if p is None:
        return 0.0
    pin, pout, read_mult = p
    return (
        uncached_in * pin
        + cached_in * pin * read_mult
        + cache_write * pin * CACHE_WRITE_5M_MULT
        + out * pout
    ) / 1_000_000
