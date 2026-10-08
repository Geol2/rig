import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from rig.graph import CycleError, layers
from rig.hand import EchoWorker
from rig.runner import run_shift
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
    assert {p.name for p in shift.iterdir()} == {"a.md", "b.md", "c.md", "shift.json"}
