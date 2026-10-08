"""Running one hand: a Claude tool-use loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import anthropic

from rig import cost
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
    # Prompt cache usage; input_tokens above counts only the uncached part.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason == "end_turn"

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cost_usd(self) -> float | None:
        # Turns served by a server-side fallback model are still priced at `model`.
        return cost.cost(self.model, self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)


# Called when a hand tries to finish. Returns None to allow it, or a message
# telling the hand what is still missing (the loop then continues).
FinishCheck = Callable[[], str | None]


class Worker(Protocol):
    async def run(
        self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check: FinishCheck | None = None
    ) -> HandResult: ...


class ClaudeWorker:
    def __init__(self, client: anthropic.AsyncAnthropic | None = None, meter: cost.Meter | None = None):
        self.client = client or anthropic.AsyncAnthropic()
        # Shared by every hand of a shift: running cost, limit, stop request. run_shift sets it.
        self.meter = meter

    async def run(
        self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check: FinishCheck | None = None
    ) -> HandResult:
        # Append-only history: response content (including thinking blocks) goes back unchanged.
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        result = HandResult(name=hand.name, output="", stop_reason="", turns=0, model=hand.model, transcript=messages)

        params: dict[str, Any] = {
            "model": hand.model,
            "max_tokens": hand.max_tokens,
            "system": hand.role,
            "output_config": {"effort": hand.effort},
            # Each turn resends the whole history; caching the prefix makes repeat reads ~10x cheaper.
            "cache_control": {"type": "ephemeral"},
        }
        if hand.output_schema:
            # Structured outputs: the final reply is JSON matching the schema; tool calls still work as usual.
            params["output_config"]["format"] = {"type": "json_schema", "schema": hand.output_schema}
        if toolbox.definitions:
            params["tools"] = toolbox.definitions
        if hand.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = hand.fallbacks

        while result.turns < hand.max_turns:
            if self.meter and self.meter.stop_reason:
                result.stop_reason = self.meter.stop_reason
                result.output = f"[{self.meter.message()}]"
                return result
            result.turns += 1
            try:
                response = await self.client.beta.messages.create(messages=messages, **params)
            except anthropic.APIStatusError as e:
                # The SDK already retried 429/5xx; what reaches here won't succeed on a blind retry.
                result.stop_reason = "api_error"
                result.output = f"[API error {e.status_code}: {e.message}]"
                return result
            except anthropic.APIConnectionError as e:
                result.stop_reason = "api_error"
                result.output = f"[API connection error: {e}]"
                return result
            result.input_tokens += response.usage.input_tokens
            result.output_tokens += response.usage.output_tokens
            result.cache_read_tokens += response.usage.cache_read_input_tokens or 0
            result.cache_write_tokens += response.usage.cache_creation_input_tokens or 0
            if self.meter:
                self.meter.add(cost.cost(
                    hand.model, response.usage.input_tokens, response.usage.output_tokens,
                    response.usage.cache_read_input_tokens or 0, response.usage.cache_creation_input_tokens or 0,
                ))
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
                objection = check() if check and response.stop_reason == "end_turn" else None
                if objection is None:
                    return result
                messages.append({"role": "user", "content": objection})
                continue

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

    meter: cost.Meter | None = None

    async def run(
        self, hand: ResolvedHand, prompt: str, toolbox: Toolbox, check: FinishCheck | None = None
    ) -> HandResult:
        if self.meter and self.meter.stop_reason:
            return HandResult(name=hand.name, output=f"[{self.meter.message()}]", stop_reason=self.meter.stop_reason,
                              turns=0, model=hand.model)
        tools = [d["name"] for d in toolbox.definitions] or "-"
        output = f"({hand.name} on {hand.model}, effort={hand.effort}, tools={tools}{', output=json' if hand.output_schema else ''})\n{prompt}"
        return HandResult(name=hand.name, output=output, stop_reason="end_turn", turns=0, model=hand.model)
