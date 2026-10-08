"""Cost limit, the stop request, and the running cost shown while a shift runs."""

import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from rig.cli import main
from rig.cost import Meter
from rig.hand import ClaudeWorker, HandResult
from rig.runner import run_shift
from rig.serve import App
from rig.spec import Rig
from rig.tools import Toolbox
from test_worker import FakeClient, block, response


def test_meter():
    m = Meter(limit=1.0)
    m.add(0.6)
    assert m.stop_reason is None
    m.add(None)
    assert m.unpriced and m.spent == 0.6
    m.add(0.5)
    assert m.stop_reason == "budget" and m.message() == "cost limit $1.00 reached ($1.10 spent)"
    m.stop()  # an earlier reason wins
    assert m.stop_reason == "budget"
    assert Meter().message() == "stopped by user"


def hand(**extra):
    rig = Rig.model_validate({"name": "t", "hands": {"coder": {"role": "Code.", "tools": ["read_file"], **extra}}})
    return rig.resolve("coder")


def test_worker_stops_at_the_limit(tmp_path):
    # Each fake response costs (10*4 + 5*20 + 100*0.2) / 1e6 = $0.00016 on Opus 5.5.
    tool_turn = lambda i: response("tool_use", block("tool_use", id=f"t{i}", name="read_file", input={"path": "x"}))
    client = FakeClient([tool_turn(i) for i in range(5)])
    meter = Meter(limit=0.0003)
    h = hand()
    res = asyncio.run(ClaudeWorker(client, meter=meter).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert len(client.calls) == 2  # the second response crossed the limit; no third request
    assert res.stop_reason == "budget" and not res.ok
    assert res.output == "[cost limit $0.0003 reached ($0.0003 spent)]"


def test_worker_stops_on_request(tmp_path):
    meter = Meter()
    meter.stop()
    client = FakeClient([])
    h = hand()
    res = asyncio.run(ClaudeWorker(client, meter=meter).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.stop_reason == "stopped" and res.output == "[stopped by user]" and client.calls == []


class CostlyWorker:
    """Each hand costs $1; the meter is set by run_shift."""

    meter: Meter | None = None

    async def run(self, hand, prompt, toolbox, check=None):
        if self.meter.stop_reason:
            return HandResult(hand.name, f"[{self.meter.message()}]", self.meter.stop_reason, 0)
        self.meter.add(1.0)
        return HandResult(hand.name, "done", "end_turn", 1)


def test_shift_stops_before_the_next_stage(tmp_path):
    rig = Rig.model_validate({"name": "t", "max_cost_usd": 1.5, "hands": {"a": {"role": "A"}, "b": {"role": "B"}, "c": {"role": "C"}},
                              "lines": ["a -> b -> c"]})
    events = []
    shift = asyncio.run(run_shift(rig, "go", CostlyWorker(), root=tmp_path, on_event=events.append))
    assert not shift.ok and set(shift.results) == {"a", "b"}
    assert "  ✗ not starting c: cost limit $1.50 reached ($2.00 spent)" in events
    assert "✗ stopped: cost limit $1.50 reached ($2.00 spent)" in events
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["stopped"] == "budget" and summary["max_cost_usd"] == 1.5


def test_explicit_limit_overrides_the_rig(tmp_path):
    rig = Rig.model_validate({"name": "t", "max_cost_usd": 100, "hands": {"a": {"role": "A"}, "b": {"role": "B"}}, "lines": ["a -> b"]})
    shift = asyncio.run(run_shift(rig, "go", CostlyWorker(), root=tmp_path, on_event=lambda _: None, meter=Meter(0.5)))
    assert set(shift.results) == {"a"} and shift.meter.limit == 0.5


def test_spec_and_cli_reject_non_positive_limits(tmp_path, monkeypatch):
    with pytest.raises(ValidationError):
        Rig.model_validate({"name": "t", "max_cost_usd": 0, "hands": {"a": {"role": "A"}}})
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="--max-cost must be more than 0"):
        main(["run", "--dry", "--max-cost", "0", "go"])


class WaitingWorker:
    """Runs until the shift is asked to stop, like a hand in a long tool loop."""

    meter: Meter | None = None

    async def run(self, hand, prompt, toolbox, check=None):
        for _ in range(200):
            if self.meter.stop_reason:
                return HandResult(hand.name, f"[{self.meter.message()}]", self.meter.stop_reason, 1)
            self.meter.add(0.01)
            await asyncio.sleep(0.01)
        return HandResult(hand.name, "done", "end_turn", 1)


def test_serve_stop_and_live_cost(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: t\nmax_cost_usd: 9\nhands:\n  a: {role: A}\n  b: {role: B}\nlines: ['a -> b']\n",
                                       encoding="utf-8")
    monkeypatch.setattr("rig.hand.EchoWorker", WaitingWorker)
    app = App(tmp_path)
    assert app.rigs()[0]["max_cost_usd"] == 9
    app.start("rig.yaml", "go", {}, dry=True, use_worktree=False, max_cost=None)
    time.sleep(0.2)
    state = app.run_state(0)
    assert state["status"] == "running" and state["cost"]["limit"] == "$9.00" and state["cost"]["spent"] != "$0.00"
    app.stop()
    for _ in range(100):
        state = app.run_state(0)
        if state["status"] != "running":
            break
        time.sleep(0.02)
    assert state["status"] == "stopped" and state["cost"]["stopping"] == "stopped"
    assert any("stop requested" in line for line in state["lines"])
    assert any("not starting b" in line for line in state["lines"])
    with pytest.raises(ValueError, match="nothing is running"):
        app.stop()


def test_serve_limit_from_the_page(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    monkeypatch.setattr("rig.hand.EchoWorker", WaitingWorker)
    app = App(tmp_path)
    with pytest.raises(ValueError, match="more than 0"):
        app.start("rig.yaml", "go", {}, dry=True, use_worktree=False, max_cost=0)
    app.start("rig.yaml", "go", {}, dry=True, use_worktree=False, max_cost=0.05)
    for _ in range(200):
        if app.run_state(0)["status"] != "running":
            break
        time.sleep(0.02)
    state = app.run_state(0)
    assert state["status"] == "stopped" and state["cost"]["stopping"] == "budget"
