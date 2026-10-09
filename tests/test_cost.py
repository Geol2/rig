"""Prices, cost estimates and the shift totals line."""

import asyncio
import json

import pytest

from rig.cost import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR, Meter, Price, cost, price, price_warnings, summary, usd
from rig.hand import EchoWorker, HandResult
from rig.runner import _totals, run_shift
from rig.spec import Rig


def test_price_known_and_dated():
    assert price("claude-sonnet-4-5") == Price(3, 15)
    assert price("claude-sonnet-4-5-20250929") == Price(3, 15)
    assert price("claude-opus-4-20250514") == Price(15, 75)


def test_current_models_priced():
    # The default model must have a price, or every default run shows "n/a".
    assert price("claude-opus-5-5") == Price(4, 20, cache_read=0.20)
    for model in ["claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-5-5"]:
        assert price(model) is not None


def test_model_specific_cache_read_price():
    m = 1_000_000
    # Opus 5.5 reads cache at $0.20/M (0.05x input), not the default 0.1x.
    assert cost("claude-opus-5-5", 0, 0, cache_read_tokens=m) == pytest.approx(0.20)
    assert cost("claude-opus-5-5", 0, 0, cache_write_tokens=m) == pytest.approx(4 * CACHE_WRITE_FACTOR)
    assert cost("claude-fable-5-1", 0, 0, cache_read_tokens=m) == pytest.approx(0.25)


@pytest.mark.parametrize("model", ["claude-opus-9", "gpt-4", "", "claude-sonnet-4-5-2025"])
def test_price_unknown(model):
    assert price(model) is None
    assert cost(model, 1000, 1000) is None


def test_price_warnings_all_priced():
    assert price_warnings({"a": "claude-opus-5-5", "b": "claude-sonnet-4-5-20250929"}) == []
    assert price_warnings({"a": "claude-opus-5-5"}, limit=5) == []


def test_price_warnings_without_limit():
    [line] = price_warnings({"a": "claude-opus-5-5", "b": "claude-opus-9"})
    assert line == ("⚠ no price for model 'claude-opus-9' (hands: b): its cost shows as n/a; "
                    "add it to PRICES in rig/cost.py")


def test_price_warnings_with_limit_names_limit_and_hands():
    [line] = price_warnings({"a": "claude-opus-9", "b": "claude-opus-5-5", "c": "claude-opus-9"}, limit=5)
    assert line.startswith("⚠ cost limit $5.00 can't count hands a, c: no price for model 'claude-opus-9'")
    assert "not limited" in line


def test_price_warnings_one_line_per_model():
    lines = price_warnings({"a": "gpt-4", "b": "claude-opus-9", "c": "gpt-4"})
    assert len(lines) == 2
    assert "'gpt-4' (hands: a, c)" in lines[0] and "'claude-opus-9' (hands: b)" in lines[1]


def _shift_events(tmp_path, rig, meter=None):
    events = []
    asyncio.run(run_shift(rig, "do it", EchoWorker(), root=tmp_path, on_event=events.append, meter=meter))
    return events


def test_shift_warns_about_unpriced_model(tmp_path):
    rig = Rig.model_validate({"name": "t", "hands": {"a": {"role": "A", "model": "claude-opus-9"}}})
    events = _shift_events(tmp_path, rig)
    assert events[0].startswith("shift ")  # still first: `rig serve` reads the shift id from it
    assert events[1].startswith("⚠ no price for model 'claude-opus-9' (hands: a)")


def test_shift_warning_uses_effective_limit(tmp_path):
    rig = Rig.model_validate({"name": "t", "max_cost_usd": 3,
                              "hands": {"a": {"role": "A", "model": "claude-opus-9"}}})
    assert _shift_events(tmp_path, rig)[1].startswith("⚠ cost limit $3.00 can't count hands a")
    # --max-cost (or serve's limit) overrides the rig's.
    assert _shift_events(tmp_path, rig, Meter(limit=1))[1].startswith("⚠ cost limit $1.00 can't count hands a")


def test_shift_without_unpriced_models_has_no_warning(tmp_path):
    rig = Rig.model_validate({"name": "t", "hands": {"a": {"role": "A"}}})
    assert not any(e.startswith("⚠") for e in _shift_events(tmp_path, rig))


def test_foreman_mode_warns_only_for_foreman_and_crew(tmp_path):
    rig = Rig.model_validate({"name": "t", "foreman": {"role": "F", "model": "gpt-4", "crew": ["a"]},
                              "hands": {"a": {"role": "A", "model": "claude-opus-9"},
                                        "b": {"role": "B", "model": "gpt-5"}}})
    assert rig.models() == {"foreman": "gpt-4", "a": "claude-opus-9"}
    warnings = [e for e in _shift_events(tmp_path, rig) if e.startswith("⚠")]
    assert len(warnings) == 2
    assert "(hands: foreman)" in warnings[0] and "(hands: a)" in warnings[1]
    assert not any("gpt-5" in w for w in warnings)


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
    t = _totals([result("claude-sonnet-4-5", 100, 100), result("claude-opus-9", 10, 10)])
    assert t["cost_usd"] is None
    assert t["input_tokens"] == 110


def test_totals_unknown_model_without_tokens_costs_nothing():
    assert _totals([result("claude-opus-9")])["cost_usd"] == 0
    assert _totals([result("claude-opus-9"), result("claude-haiku-4-5", 0, 1_000_000)])["cost_usd"] == pytest.approx(5)


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
    assert data["hands"]["a"]["cost_usd"] == 0  # default model is priced; the dry run used no tokens
    assert data["totals"] == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                              "cache_write_tokens": 0, "cost_usd": 0}
    assert events[-2] == "tokens: 0 in · 0 out · est. $0.00"
    assert events[-1].startswith("logs → ")
