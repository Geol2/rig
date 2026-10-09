import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from rig.graph import CycleError, layers
from rig.hand import EchoWorker
from rig.runner import new_shift, run_shift
from rig.spec import Rig, load


def make(**overrides):
    data = {
        "name": "t",
        "hands": {"a": {"role": "A"}, "b": {"role": "B"}, "c": {"role": "C"}},
        "lines": ["a -> b", "a -> c"],
    }
    data.update(overrides)
    return Rig.model_validate(data)


def test_template_is_valid():
    rig = load(Path(__file__).parents[1] / "src" / "rig" / "template.yaml")
    assert layers(list(rig.hands), rig.edges) == [["planner"], ["coder"], ["reviewer"]]


def test_chains_expand_to_edges():
    rig = make(lines=["a -> b -> c"])
    assert rig.edges == [("a", "b"), ("b", "c")]


def test_independent_hands_share_a_layer():
    rig = make()
    assert layers(list(rig.hands), rig.edges) == [["a"], ["b", "c"]]


def test_hand_overrides_defaults():
    rig = make(defaults={"effort": "low"}, hands={"a": {"role": "A", "effort": "max"}, "b": {"role": "B"}, "c": {"role": "C"}})
    assert rig.resolve("a").effort == "max"
    assert rig.resolve("b").effort == "low"


@pytest.mark.parametrize(
    "overrides",
    [
        {"lines": ["a -> x"]},
        {"lines": ["a ->"]},
        {"lines": ["a -> b", "b -> a"]},
        {"hands": {"a": {"role": "A", "tools": ["rm_rf"]}}, "lines": []},
        {"hands": {}, "lines": []},
    ],
)
def test_invalid_rigs_rejected(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_cycle_error():
    with pytest.raises(CycleError):
        layers(["a", "b"], [("a", "b"), ("b", "a")])


def test_dry_shift_hands_off(tmp_path):
    rig = make()
    shift = asyncio.run(run_shift(rig, "do it", EchoWorker(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok
    assert set(shift.results) == {"a", "b", "c"}
    assert '<handoff from="a">' in shift.results["b"].output
    shift = next((tmp_path / ".rig" / "shifts").iterdir())
    assert {p.name for p in shift.iterdir()} == {"a.md", "b.md", "c.md", "shift.json", "progress.log",
                                                 "a.transcript.json", "b.transcript.json", "c.transcript.json"}


def test_every_last_stage_hand_is_final(tmp_path):
    shift = asyncio.run(run_shift(make(), "do it", EchoWorker(), root=tmp_path, on_event=lambda _: None))
    assert [r.name for r in shift.finals] == ["b", "c"]  # YAML order
    assert shift.final.name == "c"
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["final"] == ["b", "c"]


def test_single_last_hand_is_final(tmp_path):
    shift = asyncio.run(run_shift(make(lines=["a -> b -> c"]), "do it", EchoWorker(), root=tmp_path,
                                  on_event=lambda _: None))
    assert [r.name for r in shift.finals] == ["c"]
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["final"] == ["c"]


def test_refusal_is_reported_in_progress(tmp_path):
    from rig.hand import HandResult, refusal_message

    class Refusing:
        async def run(self, hand, prompt, toolbox, check=None):
            return HandResult(hand.name, refusal_message("reasoning_extraction"), "refusal", 1)

    events = []
    shift = asyncio.run(run_shift(make(), "x", Refusing(), root=tmp_path, on_event=events.append))
    assert not shift.ok
    assert any(e.startswith("  ✗ a: [거절됨: reasoning_extraction]") for e in events)


class CrashingWorker(EchoWorker):
    """Echoes, except that the hands in `crash` raise `exc`."""

    def __init__(self, exc, *crash):
        self.exc, self.crash = exc, crash

    async def run(self, hand, prompt, toolbox, check=None):
        if hand.name in self.crash:
            raise self.exc
        return await super().run(hand, prompt, toolbox, check)


def summary(shift):
    return json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))


def test_crashing_hand_does_not_take_down_its_siblings(tmp_path):
    events = []
    shift = asyncio.run(run_shift(make(lines=["a -> c", "b -> c"]), "do it", CrashingWorker(RuntimeError("boom"), "a"),
                                  root=tmp_path, on_event=events.append))
    assert not shift.ok and shift.error is None
    assert shift.results["a"].stop_reason == "error" and shift.results["a"].output == "[error: RuntimeError: boom]"
    assert shift.results["b"].ok and (shift.dir / "b.md").exists() and "c" not in shift.results
    assert "  ✗ a crashed: RuntimeError: boom" in events
    # The crashed hand still gets a (empty) transcript next to its output.
    assert json.loads((shift.dir / "a.transcript.json").read_text(encoding="utf-8")) == []
    assert any("upstream hands did not finish cleanly: ['a']" in e for e in events)
    hands = summary(shift)["hands"]
    assert hands["a"]["stop_reason"] == "error" and hands["b"]["stop_reason"] == "end_turn"
    # The last stage never ran, so there's no final result.
    assert shift.finals == [] and summary(shift)["final"] == []


def test_crashed_shift_still_writes_shift_json(tmp_path, monkeypatch):
    # A crash outside any hand (here: building the second stage's prompt) ends the shift, not the program.
    def broken(task, handoffs, *args, **kwargs):
        if handoffs:
            raise RuntimeError("bad prompt")
        return f"<task>\n{task}\n</task>"

    monkeypatch.setattr("rig.runner.build_prompt", broken)
    events = []
    shift = asyncio.run(run_shift(make(), "do it", EchoWorker(), root=tmp_path, on_event=events.append))
    assert not shift.ok and shift.error == "RuntimeError: bad prompt"
    assert "✗ shift failed: RuntimeError: bad prompt" in events and events[-1] == f"logs → {shift.dir}"
    s = summary(shift)
    assert s["ok"] is False and s["error"] == "RuntimeError: bad prompt" and list(s["hands"]) == ["a"]


def test_interrupted_shift_writes_shift_json_and_propagates(tmp_path):
    rig = make(hands={"a": {"role": "A"}}, lines=[])
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_shift(rig, "do it", CrashingWorker(KeyboardInterrupt(), "a"), root=tmp_path, on_event=lambda _: None))
    shift_dir = next((tmp_path / ".rig" / "shifts").iterdir())
    s = json.loads((shift_dir / "shift.json").read_text(encoding="utf-8"))
    assert s["error"] == "interrupted" and s["ok"] is False


def test_same_second_shifts_get_distinct_ids(tmp_path, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr("rig.runner.datetime", Frozen)
    ids = [new_shift(tmp_path).id for _ in range(3)]
    assert ids == ["20260102-030405", "20260102-030405-2", "20260102-030405-3"]
    # Still ordered by time for `rig logs` / `rig logs last`.
    (tmp_path / ".rig" / "shifts" / "20260102-030406").mkdir()
    names = [p.name for p in sorted((tmp_path / ".rig" / "shifts").iterdir())]
    assert names == [*ids, "20260102-030406"]
