"""Estimated USD cost of API usage, from list prices (Claude API, first-party)."""

from __future__ import annotations

# USD per million tokens: (input, output, 5-minute cache write, cache read).
# Hands cache with the default 5-minute TTL, so that's the write rate used.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 12.50, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 12.50, 0.25),
    "claude-fable-5": (10.00, 50.00, 12.50, 1.00),
    "claude-mythos-5": (10.00, 50.00, 12.50, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}

# Haiku 5.5 bills prompts over 100K tokens at a higher rate card.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.625, 0.05))}


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float | None:
    """Cost of one request in USD, or None if the model's price isn't known."""
    prices = PRICES.get(model)
    if prices is None:
        return None
    if model in LONG_PROMPT:
        threshold, long_prices = LONG_PROMPT[model]
        if input_tokens + cache_read + cache_write > threshold:
            prices = long_prices
    p_in, p_out, p_write, p_read = prices
    return (input_tokens * p_in + output_tokens * p_out + cache_write * p_write + cache_read * p_read) / 1_000_000


def add(total: float | None, amount: float | None) -> float | None:
    """Sum costs; one unknown makes the total unknown."""
    return None if total is None or amount is None else total + amount


def usd(amount: float | None) -> str:
    if amount is None:
        return "n/a"
    return f"${amount:,.2f}" if amount >= 1 else f"${amount:.4f}"
