"""Running one hand: a Claude tool-use loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from rig.spec import ResolvedHand
from rig.tools import Toolbox, ToolError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class HandResult:
    name: str
    output: str
    stop_reason: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason == "end_turn"


class Worker(Protocol):
    async def run(self, hand: ResolvedHand, prompt: str, toolbox: Toolbox) -> HandResult: ...


class ClaudeWorker:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None):
        self.client = client or anthropic.AsyncAnthropic()

    async def run(self, hand: ResolvedHand, prompt: str, toolbox: Toolbox) -> HandResult:
        # Append-only history: response content (including thinking blocks) goes back unchanged.
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        result = HandResult(name=hand.name, output="", stop_reason="", turns=0, transcript=messages)

        params: dict[str, Any] = {
            "model": hand.model,
            "max_tokens": hand.max_tokens,
            "system": hand.role,
            "output_config": {"effort": hand.effort},
        }
        if toolbox.definitions:
            params["tools"] = toolbox.definitions
        if hand.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = hand.fallbacks

        while result.turns < hand.max_turns:
            result.turns += 1
            response = await self.client.beta.messages.create(messages=messages, **params)
            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens
            messages.append({"role": "assistant", "content": response.content})
            result.stop_reason = response.stop_reason or ""

            if response.stop_reason == "refusal":
                category = response.stop_details.category if response.stop_details else None
                result.output = f"[refused: {category}]"
                return result
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason != "tool_use":
                result.output = "".join(b.text for b in response.content if b.type == "text")
                return result

            # Parallel tool calls run concurrently; all results go back in one user message.
            calls = [b for b in response.content if b.type == "tool_use"]
            tool_results = await asyncio.gather(*(_call(toolbox, b) for b in calls))
            messages.append({"role": "user", "content": list(tool_results)})

        result.stop_reason = "max_turns"
        result.output = f"[stopped after {hand.max_turns} turns]"
        return result


async def _call(toolbox: Toolbox, block: Any) -> dict[str, Any]:
    try:
        content, is_error = await toolbox.call(block.name, dict(block.input)), False
    except (ToolError, KeyError, OSError) as e:
        content, is_error = f"Error: {e}", True
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error}


class EchoWorker:
    """Offline worker for `rig run --dry`: no API calls, just shows what each hand would receive."""

    async def run(self, hand: ResolvedHand, prompt: str, toolbox: Toolbox) -> HandResult:
        tools = [d["name"] for d in toolbox.definitions] or "-"
        output = f"({hand.name} on {hand.model}, effort={hand.effort}, tools={tools})\n{prompt}"
        return HandResult(name=hand.name, output=output, stop_reason="end_turn", turns=0)
