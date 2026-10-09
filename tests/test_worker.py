"""ClaudeWorker against a fake client: request shape, tool loop, parallel tool calls."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from rig import cost
from rig.hand import FALLBACK_BETA, ClaudeWorker, _call
from rig.spec import Rig
from rig.tools import Toolbox


def block(type, **kw):
    return NS(type=type, **kw)


def response(stop_reason, *content):
    return NS(stop_reason=stop_reason, content=list(content), usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=None), stop_details=None)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kwargs):
        # Snapshot the history; the worker keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def hand():
    rig = Rig.model_validate({"name": "t", "hands": {"coder": {"role": "Code.", "tools": ["write_file", "read_file"]}}})
    return rig.resolve("coder")


def test_tool_loop(tmp_path):
    client = FakeClient([
        response(
            "tool_use",
            block("text", text="writing"),
            block("tool_use", id="t1", name="write_file", input={"path": "a.txt", "content": "A"}),
            block("tool_use", id="t2", name="read_file", input={"path": "missing.txt"}),
        ),
        response("end_turn", block("text", text="done")),
    ])
    h = hand()
    res = asyncio.run(ClaudeWorker(client).run(h, "go", Toolbox(tmp_path, h.tools)))

    assert res.ok and res.output == "done" and res.turns == 2
    assert res.model == "claude-opus-5-5"
    assert res.input_tokens == 20
    assert res.cache_read_tokens == 200 and res.cache_write_tokens == 0
    assert (tmp_path / "a.txt").read_text() == "A"

    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["system"] == "Code."
    assert first["output_config"] == {"effort": "medium"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["betas"] == [FALLBACK_BETA] and first["fallbacks"] == "default"
    assert [t["name"] for t in first["tools"]] == ["write_file", "read_file"]

    # Both tool results come back in a single user message, errors flagged.
    results = client.calls[1]["messages"][-1]
    assert results["role"] == "user"
    assert [(r["tool_use_id"], r["is_error"]) for r in results["content"]] == [("t1", False), ("t2", True)]


def test_refusal_stops(tmp_path):
    r = response("refusal")
    r.stop_details = NS(category="cyber")
    h = hand()
    res = asyncio.run(ClaudeWorker(FakeClient([r])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert not res.ok and res.output.startswith("[거절됨: cyber] ")
    assert "취약점을 찾는 리뷰는 허용" in res.output


def test_api_error_ends_hand_cleanly(tmp_path):
    import anthropic
    import httpx2

    class Failing(FakeClient):
        async def create(self, **kwargs):
            req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                "Your credit balance is too low", response=httpx2.Response(400, request=req), body=None
            )

    h = hand()
    res = asyncio.run(ClaudeWorker(Failing([])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.stop_reason == "api_error" and not res.ok
    assert "400" in res.output and "credit balance" in res.output


def test_finish_check_continues_the_loop(tmp_path):
    client = FakeClient([response("end_turn", block("text", text="early")), response("end_turn", block("text", text="final"))])
    objections = iter(["review first", None])
    h = hand()
    res = asyncio.run(ClaudeWorker(client).run(h, "go", Toolbox(tmp_path, h.tools), check=lambda: next(objections)))
    assert res.output == "final" and res.turns == 2
    assert client.calls[1]["messages"][-1] == {"role": "user", "content": "review first"}


def test_pause_turn_continues_the_loop(tmp_path):
    paused = response("pause_turn", block("text", text="partial"))
    client = FakeClient([paused, response("end_turn", block("text", text="done"))])
    h = hand()
    res = asyncio.run(ClaudeWorker(client).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.ok and res.output == "done" and res.turns == 2
    assert res.input_tokens == 20 and res.output_tokens == 10
    assert len(client.calls) == 2
    # The paused turn is resent as-is; no user message follows it.
    last = client.calls[1]["messages"][-1]
    assert last["role"] == "assistant" and last["content"] == paused.content


def test_unpriced_model_marks_meter(tmp_path):
    h = hand().model_copy(update={"model": "claude-unknown-9"})
    meter = cost.Meter(limit=1.0)
    client = FakeClient([response("end_turn", block("text", text="ok"))])
    res = asyncio.run(ClaudeWorker(client, meter=meter).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.ok and res.model == "claude-unknown-9" and res.cost_usd is None
    assert meter.unpriced is True and meter.spent == 0
    assert meter.stop_reason is None


def test_priced_model_counts_toward_meter(tmp_path):
    h = hand()
    meter = cost.Meter(limit=1.0)
    client = FakeClient([response("end_turn", block("text", text="ok"))])
    res = asyncio.run(ClaudeWorker(client, meter=meter).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert meter.unpriced is False and meter.spent > 0
    assert res.cost_usd == pytest.approx(meter.spent)


def test_max_tokens_ends_hand(tmp_path):
    client = FakeClient([response("max_tokens", block("text", text="cut "), block("text", text="off"))])
    checked = []
    h = hand()
    res = asyncio.run(ClaudeWorker(client).run(
        h, "go", Toolbox(tmp_path, h.tools), check=lambda: checked.append(True) or "should not be used",
    ))
    assert res.stop_reason == "max_tokens" and not res.ok
    assert res.output == "cut off" and res.turns == 1
    assert len(client.calls) == 1
    # The finish check only runs on end_turn.
    assert checked == []


def test_unexpected_tool_exception_is_a_tool_error(tmp_path, monkeypatch):
    # Raised directly: what a bad path raises differs by OS (Linux: ValueError for a NUL byte;
    # Windows: no error, just "no such file").
    def broken(self, name, args):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(Toolbox, "run", broken)
    h = hand()
    call = block("tool_use", id="t1", name="read_file", input={"path": "a"})
    result = asyncio.run(_call(Toolbox(tmp_path, h.tools), call))
    assert result["is_error"] and result["content"] == "Error: ValueError: embedded null byte"


def test_unexpected_client_exception_keeps_partial_result(tmp_path):
    class Breaking(FakeClient):
        async def create(self, **kwargs):
            if self.responses:
                return await super().create(**kwargs)
            raise RuntimeError("sdk bug")

    turn = response("tool_use", block("tool_use", id="t1", name="read_file", input={"path": "x"}))
    h = hand()
    res = asyncio.run(ClaudeWorker(Breaking([turn])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.stop_reason == "error" and not res.ok
    assert res.output == "[error: RuntimeError: sdk bug]"
    assert res.turns == 2 and res.input_tokens == 10 and res.output_tokens == 5


def test_max_turns(tmp_path):
    h = hand().model_copy(update={"max_turns": 2})
    loop = lambda i: response("tool_use", block("tool_use", id=f"t{i}", name="read_file", input={"path": "x"}))
    res = asyncio.run(ClaudeWorker(FakeClient([loop(1), loop(2)])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.stop_reason == "max_turns" and not res.ok


@pytest.mark.parametrize("category", ["reasoning_extraction", "bio", "frontier_llm", "general_harms", "new_category", None])
def test_refusal_messages(category):
    from rig.hand import refusal_message

    msg = refusal_message(category)
    assert msg.startswith(f"[거절됨: {category or '분류 없음'}] ")
    if category == "reasoning_extraction":
        assert "생각 과정을 보여줘" in msg and "자동 재시도되지 않습니다" in msg
