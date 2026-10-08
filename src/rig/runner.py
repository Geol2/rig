"""Running a shift: every hand along the lines, or whatever the foreman delegates."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rig import worktree
from rig.graph import layers, upstreams
from rig.hand import HandResult, Worker
from rig.spec import Rig
from rig.tools import Toolbox, ToolError

Event = Callable[[str], None]
ToolFactory = Callable[..., Toolbox]


@dataclass
class Shift:
    id: str
    dir: Path
    # Keyed by hand name in lines mode; "foreman" and "<hand>#<n>" in foreman mode.
    results: dict[str, HandResult] = field(default_factory=dict)
    final: HandResult | None = None
    ok: bool = False
    worktree: worktree.Worktree | None = None
    outcome: worktree.Outcome | None = None


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
    use_worktree: bool = False,
    verbose: bool = True,
) -> Shift:
    workspace = (root / rig.workspace).resolve()
    shift = new_shift(root)
    mode = "foreman" if rig.foreman else "lines"
    on_event(f"shift {shift.id} · rig '{rig.name}' · {mode}")

    env = None
    if use_worktree:
        # Raises GitError before any hand runs if the workspace isn't in a git repo.
        wt = worktree.create(workspace, root / ".rig" / "worktrees" / shift.id, f"rig/{shift.id}")
        shift.worktree = wt
        workspace = wt.map(workspace)
        env = wt.env  # so hands' `run git ...` works in the worktree too
        on_event(f"  worktree {wt.path} · branch {wt.branch} from {wt.base[:7]}")
        if wt.dirty:
            on_event("  ! the repo has uncommitted changes; they are not in the worktree")

    def tools(names: list[str], label: str, extra: dict | None = None) -> Toolbox:
        on_call = (lambda line: on_event(f"    · {label:<12} {line}")) if verbose else None
        return Toolbox(workspace, names, extra=extra, run_policy=rig.run, env=env, on_call=on_call)

    def record(key: str, res: HandResult) -> None:
        shift.results[key] = res
        (shift.dir / f"{key.replace('#', '-')}.md").write_text(res.output, encoding="utf-8")

    try:
        if rig.foreman:
            await _run_foreman(rig, task, worker, shift, record, on_event, tools)
        else:
            await _run_lines(rig, task, worker, shift, record, on_event, tools)
    finally:
        if shift.worktree:
            # Even if the shift crashed, keep whatever the hands wrote on the branch.
            first = task.strip().splitlines()[0][:60] if task.strip() else "shift"
            message = f"rig: {first}\n\nShift {shift.id} of rig '{rig.name}' ({'ok' if shift.ok else 'incomplete'})."
            shift.outcome = worktree.finish(shift.worktree, message)
            _report_worktree(shift, on_event)

    summary = {
        "rig": rig.name,
        "mode": mode,
        "task": task,
        "ok": shift.ok,
        "branch": shift.worktree.branch if shift.outcome and shift.outcome.changed else None,
        "hands": {
            k: {"stop_reason": r.stop_reason, "turns": r.turns, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
                "cache_read_tokens": r.cache_read_tokens, "cache_write_tokens": r.cache_write_tokens}
            for k, r in shift.results.items()
        },
    }
    (shift.dir / "shift.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    on_event(f"logs → {shift.dir}")
    return shift


def _report_worktree(shift: Shift, on_event: Event) -> None:
    wt, out = shift.worktree, shift.outcome
    if not out.changed:
        on_event("  worktree: no changes; branch removed")
        return
    if out.kept_at:
        on_event(f"  ✗ couldn't commit on {wt.branch}; worktree kept at {out.kept_at}")
        on_event(f"    {out.error}")
        return
    on_event(f"  worktree: committed {out.commit} on {wt.branch}")
    for line in out.stat.rstrip().splitlines():
        on_event(f"    {line}")
    on_event(f"  review:  git diff {wt.base[:7]}..{wt.branch}")
    on_event(f"  merge:   git merge {wt.branch}    discard: git branch -D {wt.branch}")


def _done(res: HandResult) -> str:
    cached = f", {res.cache_read_tokens} cached" if res.cache_read_tokens else ""
    return f"[{res.stop_reason}, {res.turns} turns, {res.input_tokens}/{res.output_tokens} tok{cached}]"


async def _run_lines(rig, task, worker, shift, record, on_event, tools: ToolFactory) -> None:
    edges = rig.edges

    async def run_hand(name: str) -> HandResult:
        hand = rig.resolve(name)
        inputs = {u: shift.results[u].output for u in upstreams(name, edges)}
        on_event(f"  ▶ {name}" + (f"  ← {', '.join(inputs)}" if inputs else ""))
        res = await worker.run(hand, build_prompt(task, inputs), tools(hand.tools, name))
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


async def _run_foreman(rig, task, worker, shift, record, on_event, tools: ToolFactory) -> None:
    foreman = rig.foreman
    crew = rig.crew
    counts: dict[str, int] = {}
    total = 0
    # Completion order of successful delegations, for `require`.
    seq = 0
    last_other = 0
    last_required: dict[str, int] = {}
    unmet_at_finish: list[str] = []

    def check() -> str | None:
        pending = [r for r in foreman.require if last_required.get(r, 0) <= last_other]
        if not pending:
            return None
        if total >= foreman.max_delegations:
            # Can't delegate any more; let it finish but mark the shift incomplete.
            unmet_at_finish[:] = pending
            on_event(f"  ✗ delegation limit reached before {', '.join(pending)} reviewed the latest work")
            return None
        on_event(f"  ↺ foreman tried to finish; still required: {', '.join(pending)}")
        return (
            f"You can't finish yet: {', '.join(pending)} must run on the latest work first. "
            "Delegate to them with everything they need to check, address what they report, "
            "then give the final result."
        )

    async def delegate(args: dict[str, Any]) -> str:
        nonlocal seq, last_other
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
        res = await worker.run(hand, build_prompt(task, {}, instructions), tools(hand.tools, key))
        on_event(f"  ■ {key}  {_done(res)}")
        record(key, res)
        if not res.ok:
            raise ToolError(f"{name} did not finish cleanly ({res.stop_reason}): {res.output}")
        seq += 1
        if name in foreman.require:
            last_required[name] = seq
        else:
            last_other = seq
        return res.output

    roster = "\n".join(f"- {n}: {rig.hands[n].role.strip()}" for n in crew)
    rules = ""
    if foreman.require:
        rules = (
            f"\n\nBefore you finish, {', '.join(foreman.require)} must run after the last change by any "
            "other hand. If they report problems, send the fix back and have them check again."
        )
    hand = rig.resolve("foreman")
    hand = hand.model_copy(update={"role": f"{hand.role.strip()}\n\n<crew>\n{roster}\n</crew>{rules}"})
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
    toolbox = tools(hand.tools, "foreman", extra={"delegate": (delegate_def, delegate)})

    on_event(f"  ▶ foreman  crew: {', '.join(crew)}")
    res = await worker.run(hand, build_prompt(task, {}), toolbox, check=check if foreman.require else None)
    on_event(f"  ■ foreman  {_done(res)} · {total} delegations")
    record("foreman", res)
    shift.final = res
    shift.ok = res.ok and not unmet_at_finish
