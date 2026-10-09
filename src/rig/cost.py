"""Token prices and the shift cost summary."""

from __future__ import annotations

import re
from typing import NamedTuple


class Price(NamedTuple):
    """USD per million tokens. cache_read is None where it is the usual 0.1x input."""

    input: float
    output: float
    cache_read: float | None = None


# Anthropic API list prices. Add new models here; models not listed are shown as "n/a".
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10, 50, cache_read=0.25),
    "claude-fable-5": Price(10, 50),
    "claude-opus-5-5": Price(4, 20, cache_read=0.20),
    "claude-opus-5": Price(5, 25),
    "claude-opus-4-8": Price(5, 25),
    "claude-opus-4-7": Price(5, 25),
    "claude-opus-4-6": Price(5, 25),
    "claude-opus-4-5": Price(5, 25),
    "claude-opus-4-1": Price(15, 75),
    "claude-opus-4": Price(15, 75),
    "claude-sonnet-5-5": Price(2, 10, cache_read=0.20),
    "claude-sonnet-5": Price(2, 10),
    "claude-sonnet-4-6": Price(3, 15),
    "claude-sonnet-4-5": Price(3, 15),
    "claude-sonnet-4": Price(3, 15),
    # Prompts up to 100K tokens; longer prompts cost $0.50 / $2.50 (not modelled).
    "claude-haiku-5-5": Price(0.10, 0.50),
    "claude-haiku-4-5": Price(1, 5),
    "claude-3-5-haiku": Price(0.8, 4),
}

# Default cache pricing relative to the input price (writes are 5-minute ephemeral, as hand.py uses).
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25

_DATED = re.compile(r"-\d{8}$")


def price(model: str) -> Price | None:
    """Prices for a model id, also accepting a dated id like `claude-sonnet-4-5-20250929`."""
    return PRICES.get(model) or PRICES.get(_DATED.sub("", model))


def price_warnings(models: dict[str, str], limit: float | None = None) -> list[str]:
    """One line per hand (name → model) whose model has no known price; louder when a cost limit is set."""
    lines = []
    for name, model in models.items():
        if price(model) is not None:
            continue
        if limit is None:
            lines.append(f"⚠ {name}: model {model!r} has no known price; its cost shows as n/a")
        else:
            lines.append(f"⚠ WARNING {name}: model {model!r} has no known price, so the cost limit {usd(limit)} "
                         "can't count it; this shift may cost more than the limit")
    return lines


def cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """Estimated USD; input_tokens is the uncached part only. None if the model's price is unknown."""
    p = price(model)
    if p is None:
        return None
    read = p.cache_read if p.cache_read is not None else p.input * CACHE_READ_FACTOR
    return (
        input_tokens * p.input
        + cache_write_tokens * p.input * CACHE_WRITE_FACTOR
        + cache_read_tokens * read
        + output_tokens * p.output
    ) / 1_000_000


class Meter:
    """A shift's running cost, with an optional limit and a stop request (the Stop button in `rig serve`).

    Hands check it before every API call, so a shift can end a little over its limit:
    requests already in flight (one per running hand) still complete and are counted.
    """

    def __init__(self, limit: float | None = None):
        self.limit = limit
        self.spent = 0.0
        self.unpriced = False  # some request ran on a model with no known price
        self.stop_reason: str | None = None  # "budget" | "stopped"

    def add(self, amount: float | None) -> None:
        if amount is None:
            self.unpriced = True
            return
        self.spent += amount
        if self.limit is not None and self.spent >= self.limit and not self.stop_reason:
            self.stop_reason = "budget"

    def stop(self) -> None:
        self.stop_reason = self.stop_reason or "stopped"

    def message(self) -> str:
        if self.stop_reason == "budget":
            return f"cost limit {usd(self.limit)} reached ({usd(self.spent)} spent)"
        return "stopped by user"


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
