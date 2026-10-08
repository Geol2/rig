"""ClaudeWorker against a fake client: request shape, tool loop, parallel tool calls."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from rig.hand import FALLBACK_BETA, ClaudeWorker
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
    assert res.input_tokens == 20
    assert res.cache_read_tokens == 200 and res.cache_write_tokens == 0
    # No response.model on the fake, so it's priced as the hand's model (Opus 5.5).
    assert res.cost_usd == pytest.approx(2 * (10 * 4 + 5 * 20 + 100 * 0.20) / 1e6)
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
    assert not res.ok and res.output == "[refused: cyber]"


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


def test_max_turns(tmp_path):
    h = hand().model_copy(update={"max_turns": 2})
    loop = lambda i: response("tool_use", block("tool_use", id=f"t{i}", name="read_file", input={"path": "x"}))
    res = asyncio.run(ClaudeWorker(FakeClient([loop(1), loop(2)])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.stop_reason == "max_turns" and not res.ok


def test_cost_follows_the_serving_model(tmp_path):
    # After a fallback the response names the model that actually ran; unknown models make the cost n/a.
    served = response("end_turn", block("text", text="ok"))
    served.model = "claude-sonnet-5-5"
    h = hand()
    res = asyncio.run(ClaudeWorker(FakeClient([served])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.cost_usd == pytest.approx((10 * 2 + 5 * 10 + 100 * 0.20) / 1e6)

    unknown = response("end_turn", block("text", text="ok"))
    unknown.model = "mystery-model"
    res = asyncio.run(ClaudeWorker(FakeClient([unknown])).run(h, "go", Toolbox(tmp_path, h.tools)))
    assert res.cost_usd is None
