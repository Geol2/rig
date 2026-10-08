"""Running a shift: every hand along the lines, or whatever the foreman delegates."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rig.graph import layers, upstreams
from rig.hand import HandResult, Worker
from rig.spec import Rig
from rig.tools import Toolbox, ToolError

Event = Callable[[str], None]


@dataclass
class Shift:
    id: str
    dir: Path
    # Keyed by hand name in lines mode; "foreman" and "<hand>#<n>" in foreman mode.
    results: dict[str, HandResult] = field(default_factory=dict)
    final: HandResult | None = None
    ok: bool = False


def build_prompt(task: str, inputs: dict[str, str], instructions: str | None = None) -> str:
    parts = [f"<task>\n{task}\n</task>"]
    for name, output in inputs.items():
        parts.append(f'<handoff from="{name}">\n{output}\n</handoff>')
    if instructions:
        parts.append(f'<instructions from="foreman">\n{instructions}\n</instructions>')
    return "\n\n".join(parts)


def new_shift(root: Path) -> Shift:
    shift_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / ".rig" / "shifts" / shift_id
    d.mkdir(parents=True, exist_ok=False)
    return Shift(id=shift_id, dir=d)


async def run_shift(
    rig: Rig,
    task: str,
    worker: Worker,
    root: Path,
    on_event: Event = print,
) -> Shift:
    workspace = (root / rig.workspace).resolve()
    shift = new_shift(root)
    mode = "foreman" if rig.foreman else "lines"
    on_event(f"shift {shift.id} · rig '{rig.name}' · {mode}")

    def record(key: str, res: HandResult) -> None:
        shift.results[key] = res
        (shift.dir / f"{key.replace('#', '-')}.md").write_text(res.output, encoding="utf-8")

    if rig.foreman:
        await _run_foreman(rig, task, worker, workspace, shift, record, on_event)
    else:
        await _run_lines(rig, task, worker, workspace, shift, record, on_event)

    summary = {
        "rig": rig.name,
        "mode": mode,
        "task": task,
        "ok": shift.ok,
        "hands": {
            k: {"stop_reason": r.stop_reason, "turns": r.turns, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens}
            for k, r in shift.results.items()
        },
    }
    (shift.dir / "shift.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    on_event(f"logs → {shift.dir}")
    return shift


def _done(res: HandResult) -> str:
    return f"[{res.stop_reason}, {res.turns} turns, {res.input_tokens}/{res.output_tokens} tok]"


async def _run_lines(rig, task, worker, workspace, shift, record, on_event) -> None:
    edges = rig.edges

    async def run_hand(name: str) -> HandResult:
        hand = rig.resolve(name)
        inputs = {u: shift.results[u].output for u in upstreams(name, edges)}
        on_event(f"  ▶ {name}" + (f"  ← {', '.join(inputs)}" if inputs else ""))
        res = await worker.run(hand, build_prompt(task, inputs), Toolbox(workspace, hand.tools))
        on_event(f"  ■ {name}  {_done(res)}")
        return res

    for layer in layers(list(rig.hands), edges):
        failed = [u for n in layer for u in upstreams(n, edges) if not shift.results[u].ok]
        if failed:
            on_event(f"  ✗ stopping: upstream hands did not finish cleanly: {sorted(set(failed))}")
            return
        for res in await asyncio.gather(*(run_hand(n) for n in layer)):
            record(res.name, res)

    shift.final = list(shift.results.values())[-1]
    shift.ok = all(r.ok for r in shift.results.values())


async def _run_foreman(rig, task, worker, workspace, shift, record, on_event) -> None:
    foreman = rig.foreman
    crew = rig.crew
    counts: dict[str, int] = {}
    total = 0

    async def delegate(args: dict[str, Any]) -> str:
        nonlocal total
        name, instructions = args["hand"], args["instructions"]
        if name not in crew:
            raise ToolError(f"no hand named {name!r}; crew: {crew}")
        if total >= foreman.max_delegations:
            raise ToolError(f"delegation limit reached ({foreman.max_delegations}); finish with what you have")
        total += 1
        counts[name] = counts.get(name, 0) + 1
        key = f"{name}#{counts[name]}"

        hand = rig.resolve(name)
        on_event(f"  ↳ {key}  {instructions.strip().splitlines()[0][:70] if instructions.strip() else ''}")
        res = await worker.run(hand, build_prompt(task, {}, instructions), Toolbox(workspace, hand.tools))
        on_event(f"  ■ {key}  {_done(res)}")
        record(key, res)
        if not res.ok:
            raise ToolError(f"{name} did not finish cleanly ({res.stop_reason}): {res.output}")
        return res.output

    roster = "\n".join(f"- {n}: {rig.hands[n].role.strip()}" for n in crew)
    hand = rig.resolve("foreman")
    hand = hand.model_copy(update={"role": f"{hand.role.strip()}\n\n<crew>\n{roster}\n</crew>"})
    delegate_def = {
        "name": "delegate",
        "description": (
            "Hand a piece of work to one hand on the crew and wait for its result. "
            "The hand starts fresh and sees only the task and your instructions. "
            "Call several times in one turn to run hands in parallel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hand": {"type": "string", "enum": crew, "description": "Which hand does the work."},
                "instructions": {"type": "string", "description": "Everything the hand needs to do its piece."},
            },
            "required": ["hand", "instructions"],
            "additionalProperties": False,
        },
        "strict": True,
    }
    toolbox = Toolbox(workspace, hand.tools, extra={"delegate": (delegate_def, delegate)})

    on_event(f"  ▶ foreman  crew: {', '.join(crew)}")
    res = await worker.run(hand, build_prompt(task, {}), toolbox)
    on_event(f"  ■ foreman  {_done(res)} · {total} delegations")
    record("foreman", res)
    shift.final = res
    shift.ok = res.ok
