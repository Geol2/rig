"""Token prices and the shift cost summary."""

from __future__ import annotations

import re

# List prices in USD per million tokens: model id -> (input, output). Add new models here;
# models not listed are shown as "n/a".
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-5": (5, 25),
    "claude-opus-4-1": (15, 75),
    "claude-opus-4": (15, 75),
    "claude-sonnet-4-5": (3, 15),
    "claude-sonnet-4": (3, 15),
    "claude-haiku-4-5": (1, 5),
    "claude-3-5-haiku": (0.8, 4),
}

# Cache pricing relative to the input price (writes are 5-minute ephemeral, as hand.py uses).
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25

_DATED = re.compile(r"-\d{8}$")


def price(model: str) -> tuple[float, float] | None:
    """Prices for a model id, also accepting a dated id like `claude-sonnet-4-5-20250929`."""
    return PRICES.get(model) or PRICES.get(_DATED.sub("", model))


def cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """Estimated USD; input_tokens is the uncached part only. None if the model's price is unknown."""
    p = price(model)
    if p is None:
        return None
    inp, out = p
    cached = cache_read_tokens * CACHE_READ_FACTOR + cache_write_tokens * CACHE_WRITE_FACTOR
    return ((input_tokens + cached) * inp + output_tokens * out) / 1_000_000


def usd(x: float | None) -> str:
    if x is None:
        return "n/a"
    # Tiny runs would otherwise all show as $0.00.
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def summary(totals: dict) -> str:
    """One line from shift.json `totals`: token counts and the estimated cost."""
    parts = [f"{totals.get('input_tokens', 0):,} in", f"{totals.get('output_tokens', 0):,} out"]
    if totals.get("cache_read_tokens"):
        parts.append(f"{totals['cache_read_tokens']:,} cache read")
    if totals.get("cache_write_tokens"):
        parts.append(f"{totals['cache_write_tokens']:,} cache write")
    return f"tokens: {' · '.join(parts)} · est. {usd(totals.get('cost_usd'))}"
