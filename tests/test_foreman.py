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

    def __init__(self, script):
        self.script = script
        self.foreman_role = ""

    async def run(self, hand: ResolvedHand, prompt: str, toolbox: Toolbox) -> HandResult:
        if hand.name == "foreman":
            self.foreman_role = hand.role
            output = await self.script(toolbox)
        else:
            output = f"{hand.name} did: {prompt.split('<instructions from=\"foreman\">')[1].split('</')[0].strip()}"
        return HandResult(name=hand.name, output=output, stop_reason="end_turn", turns=1)


def run(rig, script, tmp_path):
    worker = ScriptedWorker(script)
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
    assert {p.name for p in shift.dir.iterdir()} == {"coder-1.md", "coder-2.md", "reviewer-1.md", "foreman.md", "shift.json"}
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"lines": ["coder -> reviewer"]},
        {"foreman": {"crew": ["ghost"]}},
        {"hands": {"foreman": {"role": "x"}}, "foreman": {}},
    ],
)
def test_invalid_foreman_rigs(overrides):
    with pytest.raises(ValidationError):
        make(**overrides)
