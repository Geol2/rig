import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from rig.graph import CycleError, layers
from rig.hand import EchoWorker
from rig.runner import new_shift, run_shift
from rig.spec import Rig, RigFileError, load, parse_yaml


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


def test_clear_tool_results_on_for_foreman_only():
    rig = make(lines=[], foreman={})
    assert rig.resolve("foreman").clear_tool_results is True
    assert rig.resolve("a").clear_tool_results is False
    assert make(lines=[], foreman={"clear_tool_results": False}).resolve("foreman").clear_tool_results is False


def test_clear_tool_results_from_defaults():
    rig = make(defaults={"clear_tool_results": True},
               hands={"a": {"role": "A", "clear_tool_results": False}, "b": {"role": "B"}, "c": {"role": "C"}})
    assert rig.resolve("b").clear_tool_results is True
    # An explicit false wins over defaults true.
    assert rig.resolve("a").clear_tool_results is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"lines": ["a -> x"]},
        {"lines": ["a ->"]},
        {"lines": ["a -> b", "b -> a"]},
        {"hands": {"a": {"role": "A", "tools": ["rm_rf"]}}, "lines": []},
        {"hands": {}, "lines": []},
        {"hands": {"a/b": {"role": "A"}}, "lines": []},
        {"hands": {"../x": {"role": "A"}}, "lines": []},
        {"hands": {"a#1": {"role": "A"}}, "lines": []},
        {"hands": {"code review": {"role": "A"}}, "lines": []},
        {"hands": {"x.y": {"role": "A"}}, "lines": []},
        {"hands": {"": {"role": "A"}}, "lines": []},
        {"hands": {"a\n": {"role": "A"}}, "lines": []},
        {"defaults": {"max_turns": 0}},
        {"defaults": {"max_turns": -1}},
        {"defaults": {"max_tokens": 0}},
        {"defaults": {"max_tokens": -1}},
        {"hands": {"a": {"role": "A", "max_turns": 0}}, "lines": []},
        {"hands": {"a": {"role": "A", "max_turns": -1}}, "lines": []},
        {"hands": {"a": {"role": "A", "max_tokens": 0}}, "lines": []},
        {"hands": {"a": {"role": "A", "max_tokens": -1}}, "lines": []},
        {"lines": [], "foreman": {"max_turns": 0}},
        {"lines": [], "foreman": {"max_delegations": 0}},
        {"lines": [], "foreman": {"max_delegations": -1}},
    ],
)
def test_invalid_rigs_rejected(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)


def test_minimum_limits_accepted():
    rig = make(defaults={"max_turns": 1, "max_tokens": 1},
               hands={"a": {"role": "A", "max_turns": 1, "max_tokens": 1}, "b": {"role": "B"}, "c": {"role": "C"}},
               lines=[], foreman={"max_delegations": 1})
    assert rig.foreman.max_delegations == 1
    assert (rig.resolve("a").max_turns, rig.resolve("a").max_tokens) == (1, 1)


def test_unset_limits_resolve_to_defaults():
    rig = make()
    assert (rig.resolve("a").max_turns, rig.resolve("a").max_tokens) == (20, 16000)


def test_bad_hand_names_reported_together():
    with pytest.raises(ValidationError, match=r"hand names must be plain names \(letters, digits, - _\): "
                                              r"\['code review', 'a/b'\]"):
        make(hands={"ok": {"role": "O"}, "code review": {"role": "A"}, "a/b": {"role": "B"}}, lines=[])


def test_bad_hand_name_reported_before_lines():
    with pytest.raises(ValidationError, match=r"hand names must be plain names .*\['a->b'\]"):
        make(hands={"a->b": {"role": "A"}, "c": {"role": "C"}}, lines=["a->b -> c"])


def test_plain_hand_names_accepted():
    names = ["coder-2", "my_hand", "리뷰어", "A1"]
    rig = make(hands={n: {"role": "R"} for n in names}, lines=["coder-2 -> my_hand -> 리뷰어 -> A1"])
    assert list(rig.hands) == names


@pytest.mark.parametrize("path", [*sorted((Path(__file__).parents[1] / "src" / "rig").glob("template*.yaml")),
                                  Path(__file__).parents[1] / "self.rig.yaml"], ids=lambda p: p.name)
def test_shipped_rigs_load(path):
    assert load(path).hands


@pytest.mark.parametrize(
    "text, message",
    [
        ("name: a\nhands:\n  a: {role: A}\nname: b\n", 'duplicate key "name" at line 4 (first at line 1); remove or rename one'),
        ("name: t\nhands:\n  a:\n    role: A\n    model: m\n    role: B\n",
         'duplicate key "role" in hands.a at line 6 (first at line 4); remove or rename one'),
        ("x:\n  - {k: 1}\n  - k: 1\n    k: 2\n", 'duplicate key "k" in x[1] at line 4 (first at line 3); remove or rename one'),
        ("hands:\n  a: {role: A, role: B}\n", 'duplicate key "role" in hands.a at line 2 (first at line 2); remove or rename one'),
        ("&k a: 1\n*k : 2\n", 'duplicate key "a" at line 2 (first at line 1); remove or rename one'),
    ],
    ids=["top-level", "nested", "list-item", "flow-mapping", "aliased-key"],
)
def test_duplicate_keys_rejected(text, message):
    with pytest.raises(RigFileError) as excinfo:
        parse_yaml(text)
    assert str(excinfo.value) == message


def test_duplicate_key_lines_in_crlf_file():
    with pytest.raises(RigFileError, match=r'duplicate key "a" in hands at line 4 \(first at line 3\)'):
        parse_yaml("name: t\r\nhands:\r\n  a: {role: A}\r\n  a: {role: B}\r\n")


def test_non_utf8_byte_position_in_crlf_file(tmp_path):
    (tmp_path / "rig.yaml").write_bytes(b"name: t\r\nhands:\r\n  a: {role: \xff}\r\n")
    with pytest.raises(RigFileError) as excinfo:
        load(tmp_path / "rig.yaml")
    assert str(excinfo.value) == "not UTF-8 (byte 0xff at line 3, column 13); save the file as UTF-8"


def test_utf8_bom_loads(tmp_path):
    (tmp_path / "rig.yaml").write_bytes(b"\xef\xbb\xbf" + "name: t\nhands:\n  a: {role: 역할}\n".encode("utf-8"))
    assert load(tmp_path / "rig.yaml").hands["a"].role == "역할"


def test_repeated_and_recursive_aliases_parse():
    assert parse_yaml("a: &x {k: 1}\nb: *x\n") == {"a": {"k": 1}, "b": {"k": 1}}
    data = parse_yaml("a: &x {self: *x}\n")
    assert data["a"]["self"] is data["a"]


@pytest.mark.parametrize("text", ["", "# just a comment\n# and another\n"], ids=["empty", "comments-only"])
def test_empty_file_is_invalid(tmp_path, text):
    (tmp_path / "rig.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load(tmp_path / "rig.yaml")


def test_merge_keys_may_be_overridden(tmp_path):
    (tmp_path / "rig.yaml").write_text(
        "name: t\nhands:\n  r: &base {role: R, max_turns: 3}\n  a:\n    <<: *base\n    role: A\n  b: {<<: *base}\n",
        encoding="utf-8",
    )
    rig = load(tmp_path / "rig.yaml")
    assert rig.hands["a"].role == "A" and rig.hands["a"].max_turns == 3 and rig.hands["b"].role == "R"


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


def test_shift_json_counts_findings(tmp_path):
    from rig.hand import HandResult

    class Finding:
        async def run(self, hand, prompt, toolbox, check=None):
            if hand.name == "a":
                return HandResult(hand.name, json.dumps({"findings": [{"title": "x"}, {"title": "y"}]}), "end_turn", 1)
            return HandResult(hand.name, "plain text", "end_turn", 1)

    shift = asyncio.run(run_shift(make(), "do it", Finding(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok and json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["findings"] == 2


def test_shift_json_counts_no_findings(tmp_path):
    shift = asyncio.run(run_shift(make(), "do it", EchoWorker(), root=tmp_path, on_event=lambda _: None))
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["findings"] == 0


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
