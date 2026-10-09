"""Each hand's full conversation is saved as <key>.transcript.json next to its output."""

import asyncio
import json

from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

from rig.hand import ClaudeWorker, HandResult
from rig.runner import run_shift
from rig.spec import Rig
from test_foreman import ScriptedWorker
from test_foreman import make as make_foreman
from test_worker import FakeClient, block, response


def make():
    return Rig.model_validate({"name": "t", "hands": {"coder": {"role": "Code.", "tools": ["read_file"]}}})


def transcript(shift, stem):
    return json.loads((shift.dir / f"{stem}.transcript.json").read_text(encoding="utf-8"))


def test_sdk_blocks_become_plain_json():
    res = HandResult("coder", "", "end_turn", 1, transcript=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            BetaTextBlock(type="text", text="reading"),
            BetaToolUseBlock(type="tool_use", id="t1", name="read_file", input={"path": "a.txt"}),
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "A", "is_error": False}]},
    ])
    data = json.loads(json.dumps(res.transcript_json()))
    text, call = data[1]["content"]
    assert text["type"] == "text" and text["text"] == "reading"
    assert call["type"] == "tool_use" and call["name"] == "read_file" and call["input"] == {"path": "a.txt"}
    assert data[2]["content"][0]["tool_use_id"] == "t1"


def test_shift_writes_transcript_next_to_output(tmp_path):
    client = FakeClient([
        response("tool_use", block("tool_use", id="t1", name="read_file", input={"path": "missing.txt"})),
        response("end_turn", block("text", text="done")),
    ])
    shift = asyncio.run(run_shift(make(), "go", ClaudeWorker(client), root=tmp_path, on_event=lambda _: None))
    assert (shift.dir / "coder.md").read_text(encoding="utf-8") == "done"
    data = transcript(shift, "coder")
    assert [m["role"] for m in data] == ["user", "assistant", "user", "assistant"]
    assert data[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "missing.txt"}}
    assert data[2]["content"][0]["type"] == "tool_result" and data[2]["content"][0]["is_error"] is True
    assert data[3]["content"][0]["text"] == "done"


def test_stopped_hand_keeps_its_transcript(tmp_path):
    rig = Rig.model_validate({"name": "t", "hands": {"coder": {"role": "Code.", "tools": ["read_file"], "max_turns": 1}}})
    client = FakeClient([response("tool_use", block("tool_use", id="t1", name="read_file", input={"path": "x"}))])
    shift = asyncio.run(run_shift(rig, "go", ClaudeWorker(client), root=tmp_path, on_event=lambda _: None))
    assert shift.results["coder"].stop_reason == "max_turns"
    data = transcript(shift, "coder")
    assert data[1]["content"][0]["id"] == "t1" and data[2]["content"][0]["tool_use_id"] == "t1"


def test_errored_hand_keeps_its_transcript(tmp_path):
    class Breaking(FakeClient):
        async def create(self, **kwargs):
            if self.responses:
                return await super().create(**kwargs)
            raise RuntimeError("sdk bug")

    client = Breaking([response("tool_use", block("tool_use", id="t1", name="read_file", input={"path": "x"}))])
    shift = asyncio.run(run_shift(make(), "go", ClaudeWorker(client), root=tmp_path, on_event=lambda _: None))
    assert shift.results["coder"].stop_reason == "error"
    data = transcript(shift, "coder")
    assert [m["role"] for m in data] == ["user", "assistant", "user"]
    assert data[1]["content"][0]["type"] == "tool_use"


def test_delegated_hand_transcript_uses_dashed_key(tmp_path):
    class Recording(ScriptedWorker):
        async def run(self, hand, prompt, toolbox, check=None):
            res = await super().run(hand, prompt, toolbox, check)
            res.transcript = [{"role": "user", "content": prompt}, {"role": "assistant", "content": [block("text", text=res.output)]}]
            return res

    async def script(tb):
        return await tb.call("delegate", {"hand": "coder", "instructions": "write x"})

    shift = asyncio.run(run_shift(make_foreman(), "build it", Recording(script), root=tmp_path, on_event=lambda _: None))
    data = transcript(shift, "coder-1")
    assert data[1]["content"] == [{"type": "text", "text": "coder did: write x"}]
    assert transcript(shift, "foreman")[1]["content"][0]["text"] == "coder did: write x"


def test_unserializable_transcript_does_not_break_the_shift(tmp_path):
    class Odd:
        __slots__ = ()

        def __str__(self):
            return "odd"

    class Weird:
        async def run(self, hand, prompt, toolbox, check=None):
            return HandResult(hand.name, "ok", "end_turn", 1, transcript=[{"role": "assistant", "content": [Odd()]}])

    shift = asyncio.run(run_shift(make(), "go", Weird(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok and transcript(shift, "coder") == [{"role": "assistant", "content": ["odd"]}]


def test_lone_surrogate_in_transcript_does_not_break_the_shift(tmp_path):
    class Surrogate:
        async def run(self, hand, prompt, toolbox, check=None):
            return HandResult(hand.name, "ok", "end_turn", 1, transcript=[{"role": "user", "content": "bad \udcff name"}])

    shift = asyncio.run(run_shift(make(), "go", Surrogate(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok and (shift.dir / "coder.md").read_text(encoding="utf-8") == "ok"
    data = transcript(shift, "coder")
    assert data[0]["role"] == "user" and data[0]["content"].startswith("bad ")


def test_lone_surrogate_in_output_and_task_does_not_break_the_shift(tmp_path):
    class Surrogate:
        async def run(self, hand, prompt, toolbox, check=None):
            return HandResult(hand.name, "bad \udcff name", "end_turn", 1)

    shift = asyncio.run(run_shift(make(), "go \udcff", Surrogate(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok and (shift.dir / "coder.md").read_text(encoding="utf-8") == "bad ? name"
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["task"] == "go ?"
    assert not (shift.dir / "running.json").exists()


def test_lone_surrogate_in_refusal_reaches_progress_log(tmp_path):
    class Refusing:
        async def run(self, hand, prompt, toolbox, check=None):
            return HandResult(hand.name, "no \udcff way", "refusal", 1)

    shift = asyncio.run(run_shift(make(), "go", Refusing(), root=tmp_path, on_event=lambda _: None))
    assert shift.error is None and (shift.dir / "coder.md").read_text(encoding="utf-8") == "no ? way"
    assert "  ✗ coder: no ? way\n" in (shift.dir / "progress.log").read_text(encoding="utf-8")
