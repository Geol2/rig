"""ClaudeCodeWorker against tests/fake_claude.py, which talks MCP to the real ccbridge.py."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from rig import claude_code
from rig.claude_code import INSTALL_HELP, ClaudeCodeWorker
from rig.cost import Meter
from rig.spec import ResolvedHand
from rig.tools import Toolbox

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def hand(**kw):
    return ResolvedHand(**{"name": "coder", "role": "You code.", "model": "claude-sonnet-5-5", "effort": "medium",
                           "max_tokens": 1000, "max_turns": 7, "tools": ["write_file"], "fallbacks": None, **kw})


def run(worker, toolbox, check=None, **kw):
    return asyncio.run(worker.run(hand(**kw), "do it", toolbox, check))


def test_hand_gets_rigs_tools_through_the_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("FAKE_CALLS", json.dumps([
        {"name": "write_file", "input": {"path": "a.txt", "content": "안녕"}},
        {"name": "write_file", "input": {"path": "../out.txt", "content": "x"}},  # outside: rig refuses
    ]))
    calls = []
    res = run(ClaudeCodeWorker(FAKE), Toolbox(tmp_path, ["write_file"], on_call=calls.append))
    seen = json.loads(res.output)
    assert res.ok and res.turns == 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "안녕"
    assert not (tmp_path.parent / "out.txt").exists()
    assert seen["server"] == "rig" and seen["tools"] == ["write_file"] and seen["schemas"]
    assert seen["calls"][0]["isError"] is False and seen["calls"][1]["isError"] is True
    assert seen["prompt"] == "do it" and calls and calls[0].startswith("write_file")
    # The subscription, not the API key; no built-in tools; the hand's model, effort and turn limit.
    assert seen["api_key"] is False
    args = seen["args"]
    assert args[args.index("--tools") + 1] == "" and args[args.index("--model") + 1] == "claude-sonnet-5-5"
    assert args[args.index("--effort") + 1] == "medium" and args[args.index("--max-turns") + 1] == "7"
    assert args[args.index("--allowedTools") + 1] == "mcp__rig__*" and "--resume" not in args
    assert (res.input_tokens, res.output_tokens, res.cache_read_tokens, res.cache_write_tokens) == (10, 5, 100, 50)


def test_finish_check_objection_resumes_the_session(tmp_path):
    objections = ["still missing: tests"]
    res = run(ClaudeCodeWorker(FAKE), Toolbox(tmp_path, []), check=lambda: objections.pop() if objections else None)
    seen = json.loads(res.output)
    assert res.ok and res.turns == 4 and res.input_tokens == 20
    assert seen["prompt"] == "still missing: tests"
    assert seen["args"][seen["args"].index("--resume") + 1] == "sess-1"


def test_structured_output(tmp_path):
    res = run(ClaudeCodeWorker(FAKE), Toolbox(tmp_path, []), output_schema={"type": "object"})
    assert res.output == '{"ok": true}'


@pytest.mark.parametrize("out, reason, text", [
    ('{"type": "result", "subtype": "error_max_turns", "is_error": true, "num_turns": 7}', "max_turns", "error_max_turns"),
    ('{"type": "result", "subtype": "success", "is_error": true, "result": "Credit balance is too low"}',
     "error", "Credit balance is too low"),
    ("not json", "error", "exit code 1"),
    ('{"type": "result", "subtype": "error_during_execution", "is_error": true, '
     '"errors": ["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"]}',
     "error", "stopped mid-turn (interrupted, or rig/Claude Code was closed)"),
])
def test_failed_session(tmp_path, monkeypatch, out, reason, text):
    monkeypatch.setenv("FAKE_RESULT", out)
    res = run(ClaudeCodeWorker(FAKE), Toolbox(tmp_path, []))
    assert res.stop_reason == reason and text in res.output and not res.ok


def test_without_claude_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "find_claude", lambda: None)
    res = run(ClaudeCodeWorker(), Toolbox(tmp_path, []))
    assert res.stop_reason == "error" and INSTALL_HELP in res.output


def test_stop_ends_a_running_session(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    meter = Meter()

    async def go():
        task = asyncio.ensure_future(ClaudeCodeWorker(FAKE, meter=meter).run(hand(), "x", Toolbox(tmp_path, [])))
        await asyncio.sleep(0.2)
        meter.stop()
        return await asyncio.wait_for(task, 10)

    res = asyncio.run(go())
    assert res.stop_reason == meter.stop_reason and not res.ok


def test_stopped_before_start(tmp_path):
    meter = Meter()
    meter.stop()
    res = run(ClaudeCodeWorker(FAKE, meter=meter), Toolbox(tmp_path, []))
    assert res.stop_reason == meter.stop_reason and res.turns == 0
