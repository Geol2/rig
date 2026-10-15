"""Running a hand with the locally installed Claude Code (`claude -p`) instead of the API.

Claude Code signs in with the user's own Claude subscription, so a shift costs no API credit.
Only the model loop moves: Claude Code's built-in tools are turned off and the hand gets rig's
own tools (the same Toolbox, with its workspace limits, `run` allow list, `delegate`, ...)
through a small MCP server (ccbridge.py) that forwards each call back to this process.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from rig import cost
from rig.hand import FinishCheck, HandResult, _call
from rig.spec import ResolvedHand
from rig.tools import Toolbox

BRIDGE = Path(__file__).with_name("ccbridge.py")
SERVER = "rig"  # MCP server name: Claude Code calls the tools mcp__rig__<name>
# Credentials that would make Claude Code bill the API instead of the subscription
# (an API key always wins over the subscription login in `claude -p`).
API_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Claude Code's own wait for one MCP tool call; `delegate` runs a whole hand, so allow hours.
TOOL_TIMEOUT_MS = 6 * 3600 * 1000
INSTALL_HELP = ("Claude Code(claude) not found. Install it and sign in with your subscription "
                "(`claude` then /login), or use the API: https://code.claude.com/docs/en/setup")


def find_claude() -> str | None:
    return shutil.which("claude")


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in API_CREDENTIALS}
    env["MCP_TOOL_TIMEOUT"] = str(TOOL_TIMEOUT_MS)
    return env


class ClaudeCodeWorker:
    """Worker that runs each hand as one `claude -p` session (resumed when a finish check objects)."""

    def __init__(self, command: list[str] | None = None, meter: cost.Meter | None = None):
        found = find_claude()
        self.command = command or ([found] if found else None)
        # Shared by every hand of a shift for the stop request. The subscription isn't billed per
        # call, so nothing is added to it; the report still shows tokens and their API price.
        self.meter = meter

    async def run(
        self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check: FinishCheck | None = None
    ) -> HandResult:
        result = HandResult(name=hand.name, output="", stop_reason="", turns=0, model=hand.model)
        if self.meter and self.meter.stop_reason:
            result.stop_reason = self.meter.stop_reason
            result.output = f"[{self.meter.message()}]"
            return result
        if not self.command:
            result.stop_reason = "error"
            result.output = f"[error: {INSTALL_HELP}]"
            return result
        token = secrets.token_hex(16)
        server = await asyncio.start_server(lambda r, w: _serve(r, w, toolbox, token), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory(prefix="rig-cc-") as tmp:
                return await self._session(hand, prompt, toolbox, check, result, Path(tmp), port, token)
        except Exception as e:  # keep the partial result, like ClaudeWorker
            result.stop_reason = "error"
            result.output = f"[error: {type(e).__name__}: {e}]"
            return result
        finally:
            server.close()

    async def _session(
        self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check: FinishCheck | None,
        result: HandResult, tmp: Path, port: int, token: str,
    ) -> HandResult:
        (tmp / "role.md").write_text(hand.role, encoding="utf-8")
        (tmp / "mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {
            "type": "stdio", "command": sys.executable, "args": [str(BRIDGE)],
            "env": {"RIG_BRIDGE_PORT": str(port), "RIG_BRIDGE_TOKEN": token},
        }}}), encoding="utf-8")
        args = [
            *self.command, "-p", "--output-format", "json",
            "--model", hand.model, "--effort", hand.effort,
            "--system-prompt-file", str(tmp / "role.md"),
            "--tools", "",  # no built-in tools: files and commands go through rig's Toolbox
            "--mcp-config", str(tmp / "mcp.json"), "--strict-mcp-config",
            "--allowedTools", f"mcp__{SERVER}__*", "--permission-mode", "dontAsk",
            "--max-turns", str(hand.max_turns),
        ]
        if hand.output_schema:
            args += ["--json-schema", json.dumps(hand.output_schema)]
        transcript: list[dict[str, Any]] = []
        result.transcript = transcript
        message, session = prompt, None
        while True:
            data = await self._claude([*args, *(["--resume", session] if session else [])], message, toolbox.workspace)
            transcript.append({"prompt": message, "result": data})
            if self.meter and self.meter.stop_reason:
                result.stop_reason = self.meter.stop_reason
                result.output = f"[{self.meter.message()}]"
                return result
            _add_usage(result, data)
            session = data.get("session_id") or session
            subtype = data.get("subtype")
            if data.get("is_error") or subtype != "success":
                result.stop_reason = "max_turns" if subtype == "error_max_turns" else "error"
                text = data.get("result") or "; ".join(map(str, data.get("errors") or [])) or subtype or "failed"
                result.output = f"[Claude Code: {text}]"
                return result
            structured = data.get("structured_output")
            result.output = json.dumps(structured, ensure_ascii=False) if structured is not None else data.get("result") or ""
            result.stop_reason = "end_turn"
            objection = check() if check else None
            if objection is None or not session:
                return result
            message = objection

    async def _claude(self, args: list[str], prompt: str, cwd: Path) -> dict[str, Any]:
        """Run `claude -p` with the prompt on stdin; stopped early when the shift is stopped."""
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, env=child_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        talk = asyncio.ensure_future(proc.communicate(prompt.encode("utf-8")))
        while not talk.done():
            await asyncio.wait({talk}, timeout=1)
            if not talk.done() and self.meter and self.meter.stop_reason:
                proc.kill()
        out, err = await talk
        try:
            data = json.loads(out.decode("utf-8", errors="replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            detail = (err or out).decode("utf-8", errors="replace").strip()[-500:]
            return {"is_error": True, "subtype": "error_during_execution",
                    "result": f"exit code {proc.returncode}: {detail or 'no output'}"}
        return data if isinstance(data, dict) else {"is_error": True, "result": str(data)}


def _add_usage(result: HandResult, data: dict[str, Any]) -> None:
    usage = data.get("usage") or {}
    result.turns += int(data.get("num_turns") or 0)
    result.input_tokens += int(usage.get("input_tokens") or 0)
    result.output_tokens += int(usage.get("output_tokens") or 0)
    result.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
    result.cache_write_tokens += int(usage.get("cache_creation_input_tokens") or 0)


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, toolbox: Toolbox, token: str) -> None:
    """One bridge request: list the hand's tools, or run one of them."""
    try:
        req = json.loads(await reader.readline())
        if not secrets.compare_digest(str(req.get("token", "")), token):
            return
        if req.get("op") == "list":
            answer: dict[str, Any] = {"tools": [
                {"name": d["name"], "description": d.get("description", ""), "inputSchema": d["input_schema"]}
                for d in toolbox.definitions
            ]}
        else:
            block = _Block(str(req.get("name")), req.get("input") or {})
            res = await _call(toolbox, block)
            content = res["content"]
            answer = {"content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                      "is_error": res["is_error"]}
        writer.write((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()


class _Block:
    """The shape of an API tool_use block, so the API worker's _call handles errors the same way."""

    def __init__(self, name: str, input: dict[str, Any]):
        self.id, self.name, self.input = "", name, input
