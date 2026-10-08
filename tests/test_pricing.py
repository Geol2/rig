import asyncio
import json

import pytest

from rig import pricing
from rig.cli import main
from rig.hand import HandResult
from rig.runner import run_shift, total_usage
from rig.spec import Rig


def test_cost_uses_all_four_rates():
    # Opus 5.5: $4 in, $20 out, $5 cache write, $0.20 cache read per MTok.
    assert pricing.cost("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert pricing.cost("claude-opus-5-5", 1_000, 2_000, cache_read=10_000, cache_write=4_000) == pytest.approx(
        (1_000 * 4 + 2_000 * 20 + 4_000 * 5 + 10_000 * 0.20) / 1e6
    )


def test_unknown_model_is_none():
    assert pricing.cost("some-other-model", 100, 100) is None


def test_haiku_long_prompt_rate():
    short = pricing.cost("claude-haiku-5-5", 100_000, 1_000)
    long = pricing.cost("claude-haiku-5-5", 60_000, 1_000, cache_read=50_000)
    assert short == pytest.approx((100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert long == pytest.approx((60_000 * 0.50 + 1_000 * 2.50 + 50_000 * 0.05) / 1e6)


def test_add_and_format():
    assert pricing.add(0.5, 0.25) == 0.75
    assert pricing.add(0.5, None) is None and pricing.add(None, 0.5) is None
    assert pricing.usd(None) == "n/a"
    assert pricing.usd(0.01234) == "$0.0123"
    assert pricing.usd(12.5) == "$12.50"


def test_total_usage_any_unknown_makes_total_unknown():
    a = HandResult("a", "", "end_turn", 1, input_tokens=10, output_tokens=5, cache_read_tokens=3, cost_usd=0.5)
    b = HandResult("b", "", "end_turn", 1, input_tokens=1, output_tokens=2, cache_write_tokens=7, cost_usd=0.25)
    assert total_usage([a, b]) == {
        "input_tokens": 11, "output_tokens": 7, "cache_read_tokens": 3, "cache_write_tokens": 7, "cost_usd": 0.75,
    }
    b.cost_usd = None
    assert total_usage([a, b])["cost_usd"] is None


class PricedWorker:
    def __init__(self, costs):
        self.costs = costs

    async def run(self, hand, prompt, toolbox, check=None):
        return HandResult(hand.name, "out", "end_turn", 1, input_tokens=100, output_tokens=10, cost_usd=self.costs[hand.name])


def shift_with(tmp_path, costs):
    rig = Rig.model_validate({"name": "t", "hands": {"a": {"role": "A"}, "b": {"role": "B"}}, "lines": ["a -> b"]})
    events = []
    shift = asyncio.run(run_shift(rig, "task", PricedWorker(costs), root=tmp_path, on_event=events.append))
    return json.loads((shift.dir / "shift.json").read_text(encoding="utf-8")), events


def test_shift_summary_printed_and_stored(tmp_path):
    summary, events = shift_with(tmp_path, {"a": 0.25, "b": 1.5})
    assert summary["usage"] == {
        "input_tokens": 200, "output_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 1.75,
    }
    assert summary["hands"]["a"]["cost_usd"] == 0.25
    assert "usage 200 in / 20 out tok · cache 0 read / 0 write · est. $1.75" in events


def test_shift_summary_unknown_price(tmp_path):
    summary, events = shift_with(tmp_path, {"a": 0.25, "b": None})
    assert summary["usage"]["cost_usd"] is None
    assert any(e.startswith("usage ") and e.endswith("est. n/a") for e in events)


def test_logs_shows_cost(tmp_path, monkeypatch, capsys):
    shift_with(tmp_path, {"a": 0.25, "b": 1.5})
    old = tmp_path / ".rig" / "shifts" / "00000000-000000"  # logged before cost tracking
    old.mkdir()
    (old / "shift.json").write_text(json.dumps({"mode": "lines", "ok": True, "task": "old"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["logs"])
    old_line, new_line = capsys.readouterr().out.splitlines()
    assert "$1.75" in new_line
    assert "$" not in old_line and "n/a" not in old_line
