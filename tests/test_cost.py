"""Prices, cost estimates and the shift totals line."""

import asyncio
import json

import pytest

from rig.cost import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR, cost, price, summary, usd
from rig.hand import EchoWorker, HandResult
from rig.runner import _totals, run_shift
from rig.spec import Rig


def test_price_known_and_dated():
    assert price("claude-sonnet-4-5") == (3, 15)
    assert price("claude-sonnet-4-5-20250929") == (3, 15)
    assert price("claude-opus-4-20250514") == (15, 75)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-4", "", "claude-sonnet-4-5-2025"])
def test_price_unknown(model):
    assert price(model) is None
    assert cost(model, 1000, 1000) is None


def test_cost_with_cache():
    # 1M of each at sonnet prices: 3 in, 15 out, cache read/write derived from input.
    m = 1_000_000
    assert cost("claude-sonnet-4-5", m, m) == pytest.approx(18)
    assert cost("claude-sonnet-4-5", 0, 0, cache_read_tokens=m) == pytest.approx(3 * CACHE_READ_FACTOR)
    assert cost("claude-sonnet-4-5", 0, 0, cache_write_tokens=m) == pytest.approx(3 * CACHE_WRITE_FACTOR)
    assert CACHE_READ_FACTOR == 0.1 and CACHE_WRITE_FACTOR == 1.25


@pytest.mark.parametrize("x, text", [(None, "n/a"), (0, "$0.00"), (1.234, "$1.23"), (0.00123, "$0.0012"), (12.5, "$12.50")])
def test_usd(x, text):
    assert usd(x) == text


def result(model, inp=0, out=0, read=0, write=0):
    return HandResult(name="h", output="", stop_reason="end_turn", turns=1, model=model, input_tokens=inp,
                      output_tokens=out, cache_read_tokens=read, cache_write_tokens=write)


def test_totals_known_models():
    t = _totals([result("claude-sonnet-4-5", 1_000_000, 0, 100, 10), result("claude-haiku-4-5", 0, 1_000_000)])
    assert t["input_tokens"] == 1_000_000 and t["output_tokens"] == 1_000_000
    assert t["cache_read_tokens"] == 100 and t["cache_write_tokens"] == 10
    assert t["cost_usd"] == pytest.approx(3 + 5 + (100 * 0.1 + 10 * 1.25) * 3 / 1_000_000)


def test_totals_unknown_model_with_tokens_is_null():
    t = _totals([result("claude-sonnet-4-5", 100, 100), result("claude-opus-5-5", 10, 10)])
    assert t["cost_usd"] is None
    assert t["input_tokens"] == 110


def test_totals_unknown_model_without_tokens_costs_nothing():
    assert _totals([result("claude-opus-5-5")])["cost_usd"] == 0
    assert _totals([result("claude-opus-5-5"), result("claude-haiku-4-5", 0, 1_000_000)])["cost_usd"] == pytest.approx(5)


def test_summary_line():
    t = {"input_tokens": 12345, "output_tokens": 3456, "cache_read_tokens": 100000, "cache_write_tokens": 0, "cost_usd": 1.234}
    assert summary(t) == "tokens: 12,345 in · 3,456 out · 100,000 cache read · est. $1.23"
    assert summary({**t, "cost_usd": None}).endswith("est. n/a")


def test_shift_writes_totals(tmp_path):
    rig = Rig.model_validate({"name": "t", "hands": {"a": {"role": "A"}}})
    events = []
    shift = asyncio.run(run_shift(rig, "do it", EchoWorker(), root=tmp_path, on_event=events.append))
    data = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert data["hands"]["a"]["model"] == rig.resolve("a").model
    assert data["hands"]["a"]["cost_usd"] is None  # default model has no listed price
    assert data["totals"] == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                              "cache_write_tokens": 0, "cost_usd": 0}
    assert events[-2] == "tokens: 0 in · 0 out · est. $0.00"
    assert events[-1].startswith("logs → ")
