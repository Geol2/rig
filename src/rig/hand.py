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
CONTEXT_EDITING_BETA = "context-management-2025-06-27"

# Server-side context editing: once the prompt passes the trigger, the API replaces old tool
# results with a placeholder, keeping the latest 5. Clearing invalidates the prompt cache, so
# clear_at_least makes each clear worth it. The client-side `messages` list stays append-only
# and unchanged, so the transcript still holds everything.
CLEAR_TOOL_RESULTS: dict[str, Any] = {"edits": [{
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 100_000},
    "keep": {"type": "tool_uses", "value": 5},
    "clear_at_least": {"type": "input_tokens", "value": 20_000},
}]}


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

    def transcript_json(self) -> list[Any]:
        """The transcript as plain JSON values (SDK blocks via model_dump)."""
        return _jsonable(self.transcript)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return str(value)


# What each refusal category (response.stop_details.category) means, and what to do about it.
REFUSAL_HELP = {
    "reasoning_extraction": (
        "모델의 안전 필터가 이 요청을 '내부 추론 과정을 답변에 써 달라는 요청'으로 판단했습니다. "
        "요구사항이나 role에서 '생각 과정을 보여줘', '단계별로 추론을 적어줘' 같은 표현을 빼고 다시 실행하세요. "
        "결과나 바꾼 점을 요약해 달라는 요청은 괜찮습니다. 이 거절은 다른 모델로 자동 재시도되지 않습니다."
    ),
    "cyber": (
        "사이버 보안상 위험할 수 있는 요청(악성코드, 공격 코드 작성 등)으로 판단했습니다. "
        "소스 코드에서 취약점을 찾는 리뷰는 허용됩니다. 요구사항 표현을 확인하고 다시 실행하세요."
    ),
    "bio": "생물학적 위험이 있을 수 있는 요청으로 판단했습니다. 관련 없는 작업이라면 표현을 바꿔 다시 실행하세요.",
    "frontier_llm": "AI 모델 개발을 돕는 요청으로 판단했습니다. 관련 없는 작업이라면 표현을 바꿔 다시 실행하세요.",
    "general_harms": "이용 정책에 어긋날 수 있는 요청으로 판단했습니다. 정상적인 작업이 잘못 걸렸을 수 있으니 표현을 바꿔 다시 실행하세요.",
}


def refusal_message(category: str | None) -> str:
    """The hand's output when the model declines: the category, why, and what to try."""
    help = REFUSAL_HELP.get(category or "", "모델이 요청을 거절했습니다. 요구사항 표현을 바꿔 다시 실행해 보세요.")
    return f"[거절됨: {category or '분류 없음'}] {help}"


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
        betas: list[str] = []
        if hand.fallbacks:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = hand.fallbacks
        if hand.clear_tool_results:
            betas.append(CONTEXT_EDITING_BETA)
            params["context_management"] = CLEAR_TOOL_RESULTS
        if betas:
            params["betas"] = betas

        try:
            return await self._loop(hand, params, messages, result, toolbox, check)
        except Exception as e:
            # An SDK bug, a check() that raises, ...: keep the partial result and the tokens already counted.
            result.stop_reason = "error"
            result.output = f"[error: {type(e).__name__}: {e}]"
            return result

    async def _loop(
        self, hand: ResolvedHand, params: dict[str, Any], messages: list[dict[str, Any]], result: HandResult,
        toolbox: Toolbox, check: FinishCheck | None,
    ) -> HandResult:
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
                result.output = refusal_message(category)
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
    except Exception as e:  # e.g. ValueError for a NUL byte in a path; the model can still recover
        content, is_error = f"Error: {type(e).__name__}: {e}", True
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
