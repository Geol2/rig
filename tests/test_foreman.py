import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import ResolvedHand, Rig, load
from rig.tools import Toolbox, ToolError


def make(**overrides):
    data = {
        "name": "t",
        "foreman": {"crew": ["coder", "reviewer"], "max_delegations": 3},
        "hands": {"coder": {"role": "Code."}, "reviewer": {"role": "Review."}, "idle": {"role": "Unused."}},
    }
    data.update(overrides)
    return Rig.model_validate(data)


class ScriptedWorker:
    """The foreman runs `script` against its toolbox; every other hand echoes its instructions."""

    def __init__(self, script, *retries):
        # `retries` run in turn each time the finish check objects, like the model's next turns.
        self.scripts = [script, *retries]
        self.foreman_role = ""
        self.objections: list[str] = []

    async def run(self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check=None) -> HandResult:
        if hand.name == "foreman":
            self.foreman_role = hand.role
            for script in self.scripts:
                output = await script(toolbox)
                objection = check() if check else None
                if objection is None:
                    break
                self.objections.append(objection)
        else:
            output = f"{hand.name} did: {prompt.split('<instructions from=\"foreman\">')[1].split('</')[0].strip()}"
        return HandResult(name=hand.name, output=output, stop_reason="end_turn", turns=1)


def run(rig, script, tmp_path, *retries):
    worker = ScriptedWorker(script, *retries)
    shift = asyncio.run(run_shift(rig, "build it", worker, root=tmp_path, on_event=lambda _: None))
    return shift, worker


def test_foreman_template_is_valid():
    rig = load(Path(__file__).parents[1] / "src" / "rig" / "template-foreman.yaml")
    assert rig.foreman and rig.crew == ["coder", "reviewer"]


def test_foreman_delegates_in_parallel_and_logs(tmp_path):
    async def script(tb):
        assert "delegate" in [d["name"] for d in tb.definitions]
        a, b = await asyncio.gather(
            tb.call("delegate", {"hand": "coder", "instructions": "write x"}),
            tb.call("delegate", {"hand": "coder", "instructions": "write y"}),
        )
        c = await tb.call("delegate", {"hand": "reviewer", "instructions": "check x and y"})
        return " | ".join([a, b, c])

    shift, worker = run(make(), script, tmp_path)
    assert shift.ok
    assert shift.final.output == "coder did: write x | coder did: write y | reviewer did: check x and y"
    assert set(shift.results) == {"coder#1", "coder#2", "reviewer#1", "foreman"}
    assert {p.name for p in shift.dir.iterdir()} == {"coder-1.md", "coder-2.md", "reviewer-1.md", "foreman.md", "shift.json", "progress.log"}
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["mode"] == "foreman"
    # The foreman's system prompt lists its crew, and only its crew.
    assert "- coder: Code." in worker.foreman_role and "idle" not in worker.foreman_role


def test_delegate_rejects_hands_outside_crew(tmp_path):
    async def script(tb):
        with pytest.raises(ToolError):
            await tb.call("delegate", {"hand": "idle", "instructions": "x"})
        return "done"

    shift, _ = run(make(), script, tmp_path)
    assert shift.ok and "idle#1" not in shift.results


def test_delegation_limit(tmp_path):
    async def script(tb):
        for _ in range(3):
            await tb.call("delegate", {"hand": "coder", "instructions": "again"})
        with pytest.raises(ToolError, match="limit"):
            await tb.call("delegate", {"hand": "coder", "instructions": "one more"})
        return "done"

    shift, _ = run(make(), script, tmp_path)
    assert len([k for k in shift.results if k.startswith("coder#")]) == 3


def requiring(max_delegations=6):
    return make(foreman={"crew": ["coder", "reviewer"], "require": ["reviewer"], "max_delegations": max_delegations})


def delegating(*hands):
    async def script(tb):
        for h in hands:
            await tb.call("delegate", {"hand": h, "instructions": "work"})
        return "final"
    return script


def test_require_pushes_back_until_reviewed(tmp_path):
    shift, worker = run(requiring(), delegating("coder"), tmp_path, delegating("reviewer"))
    assert len(worker.objections) == 1 and "reviewer" in worker.objections[0]
    assert shift.ok and "reviewer#1" in shift.results
    assert "reviewer must run after the last change" in worker.foreman_role


def test_review_must_follow_the_latest_change(tmp_path):
    # Reviewed, then the coder changed things again: a second review is needed.
    shift, worker = run(requiring(), delegating("coder", "reviewer", "coder"), tmp_path, delegating("reviewer"))
    assert len(worker.objections) == 1
    assert shift.ok and "reviewer#2" in shift.results


def test_reviewed_work_finishes_without_objection(tmp_path):
    shift, worker = run(requiring(), delegating("coder", "reviewer"), tmp_path)
    assert worker.objections == [] and shift.ok


def test_require_unmet_at_limit_marks_shift_incomplete(tmp_path):
    shift, worker = run(requiring(max_delegations=2), delegating("coder", "coder"), tmp_path)
    assert worker.objections == []
    assert shift.final.ok and not shift.ok


def test_no_require_no_check(tmp_path):
    shift, worker = run(make(), delegating("coder"), tmp_path)
    assert worker.objections == [] and shift.ok


def test_crashing_delegate_is_a_tool_error_for_the_foreman(tmp_path):
    class Crashing(ScriptedWorker):
        async def run(self, hand, prompt, toolbox, check=None):
            if hand.name == "reviewer":
                raise ValueError("embedded null byte")
            return await super().run(hand, prompt, toolbox, check)

    async def script(tb):
        with pytest.raises(ToolError, match=r"did not finish cleanly \(error\): \[error: ValueError: embedded null byte\]"):
            await tb.call("delegate", {"hand": "reviewer", "instructions": "check"})
        return "done anyway"

    worker = Crashing(script)
    shift = asyncio.run(run_shift(make(), "build it", worker, root=tmp_path, on_event=lambda _: None))
    assert shift.results["reviewer#1"].stop_reason == "error" and shift.error is None
    assert shift.final.output == "done anyway"
    assert (shift.dir / "reviewer-1.md").read_text(encoding="utf-8") == "[error: ValueError: embedded null byte]"


def test_crashing_foreman_still_writes_shift_json(tmp_path):
    async def script(tb):
        await tb.call("delegate", {"hand": "coder", "instructions": "write x"})
        raise RuntimeError("sdk bug")

    shift, _ = run(make(), script, tmp_path)
    assert not shift.ok and shift.error == "RuntimeError: sdk bug"
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["ok"] is False and summary["error"] == "RuntimeError: sdk bug"
    assert list(summary["hands"]) == ["coder#1"] and summary["hands"]["coder#1"]["turns"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"lines": ["coder -> reviewer"]},
        {"foreman": {"crew": ["coder"], "require": ["reviewer"]}},
        {"foreman": {"crew": ["ghost"]}},
        {"hands": {"foreman": {"role": "x"}}, "foreman": {}},
    ],
)
def test_invalid_foreman_rigs(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)
