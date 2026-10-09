"""Running a shift: every hand along the lines, or whatever the foreman delegates."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rig import cost, publish, worktree
from rig.graph import layers, upstreams
from rig.hand import HandResult, Worker
from rig.spec import Publish, Rig
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
    # One per git repository the shift works in (several with `workspaces`), branch rig/<id> in each
    # (or rig/<id>-2, ... in a repo where that's already taken).
    worktrees: list[worktree.Worktree] = field(default_factory=list)
    outcomes: list[worktree.Outcome] = field(default_factory=list)
    # Checked `rig run -i` values with defaults filled in.
    inputs: dict[str, str] = field(default_factory=dict)
    # Running cost and stop state shared with the worker.
    meter: cost.Meter = field(default_factory=cost.Meter)
    # Pull requests opened for the branch (publish.pr), one per changed repo.
    prs: list[publish.PullRequest] = field(default_factory=list)
    # "<Type>: <message>" if the shift crashed, "interrupted" on Ctrl-C/cancel; None otherwise.
    error: str | None = None

    @property
    def worktree(self) -> worktree.Worktree | None:
        """The (first) worktree; the only one with a single workspace."""
        return self.worktrees[0] if self.worktrees else None

    @property
    def outcome(self) -> worktree.Outcome | None:
        return self.outcomes[0] if self.outcomes else None

    @property
    def branch(self) -> str | None:
        """The branch holding the shift's changes in the first repo that changed, if any.

        Usually rig/<id> everywhere, but a repo where that was taken gets rig/<id>-2, ...
        """
        return next((wt.branch for wt, o in zip(self.worktrees, self.outcomes) if o.changed), None)

    def committed(self) -> list[tuple[worktree.Worktree, worktree.Outcome]]:
        """Worktrees whose changes were committed to the branch."""
        return [(wt, o) for wt, o in zip(self.worktrees, self.outcomes) if o.changed and not o.kept_at]


def build_prompt(
    task: str, handoffs: dict[str, str], instructions: str | None = None, inputs: dict[str, str] | None = None
) -> str:
    parts = [f"<task>\n{task}\n</task>"]
    if inputs:
        lines = "\n".join(f'<input name="{name}">{value}</input>' for name, value in inputs.items())
        parts.append(f"<inputs>\n{lines}\n</inputs>")
    for name, output in handoffs.items():
        parts.append(f'<handoff from="{name}">\n{output}\n</handoff>')
    if instructions:
        parts.append(f'<instructions from="foreman">\n{instructions}\n</instructions>')
    return "\n\n".join(parts)


def new_shift(root: Path) -> Shift:
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    shifts = root / ".rig" / "shifts"
    shifts.mkdir(parents=True, exist_ok=True)
    # Two shifts in the same second get base, base-2, base-3, ...; mkdir claims each one atomically.
    # "-" sorts before digits, so these still sort after base and before the next second.
    n = 1
    while True:
        shift_id = base if n == 1 else f"{base}-{n}"
        d = shifts / shift_id
        try:
            d.mkdir(exist_ok=False)
        except FileExistsError:
            n += 1
            continue
        return Shift(id=shift_id, dir=d)


PROGRESS_LOG = "progress.log"
RUNNING = "running.json"


class _Progress:
    """Copies progress lines to the shift folder, so `rig serve` can show a shift started from the CLI.

    `running.json` (pid, rig, task) is there while the shift runs and removed when it ends.
    """

    def __init__(self, shift: Shift, rig: str, task: str, show: Event):
        self.show = show
        self.running = shift.dir / RUNNING
        self.running.write_text(json.dumps({"pid": os.getpid(), "rig": rig, "task": task,
                                            "started": datetime.now().isoformat(timespec="seconds")},
                                           ensure_ascii=False), encoding="utf-8")
        self.log = (shift.dir / PROGRESS_LOG).open("a", encoding="utf-8")

    def event(self, line: str) -> None:
        self.show(line)
        if not self.log.closed:
            self.log.write(line + "\n")
            self.log.flush()

    def close(self) -> None:
        self.log.close()
        self.running.unlink(missing_ok=True)


async def run_shift(
    rig: Rig,
    task: str,
    worker: Worker,
    root: Path,
    on_event: Event = print,
    use_worktree: bool = False,
    verbose: bool = True,
    inputs: dict[str, str] | None = None,
    meter: cost.Meter | None = None,
) -> Shift:
    use_worktree = use_worktree or rig.publish.pr  # a PR needs the branch a worktree shift makes
    dirs = rig.workspace_dirs(root)
    # One folder, or {name: folder} for several projects.
    workspace: Path | dict[str, Path] = dirs if rig.workspaces else dirs["."]
    values = rig.resolve_inputs(inputs or {})  # raises InputError before anything is created
    shift = new_shift(root)
    progress = _Progress(shift, rig.name, task, on_event)
    on_event = progress.event
    try:
        shift.inputs = values
        meter = meter or cost.Meter()
        if meter.limit is None:
            meter.limit = rig.max_cost_usd
        shift.meter = meter
        if hasattr(worker, "meter"):
            worker.meter = meter
        mode = "foreman" if rig.foreman else "lines"
        on_event(f"shift {shift.id} · rig '{rig.name}' · {mode}")

        env = None
        if use_worktree:
            # Raises GitError before any hand runs if a workspace isn't in a git repo.
            sync = rig.publish if rig.publish.pr and rig.publish.sync else None
            workspace, run_wt = _make_worktrees(shift, workspace, root, rig.run.workspace, on_event, sync)
            env = run_wt.env  # so hands' `run git ...` works in the worktree too

        def tools(names: list[str], label: str, extra: dict | None = None) -> Toolbox:
            on_call = (lambda line: on_event(f"    · {label:<12} {line}")) if verbose else None
            return Toolbox(workspace, names, extra=extra, run_policy=rig.run, env=env, on_call=on_call, search_policy=rig.search)

        def record(key: str, res: HandResult) -> None:
            shift.results[key] = res
            (shift.dir / f"{key.replace('#', '-')}.md").write_text(res.output, encoding="utf-8")
            if res.stop_reason == "refusal":
                # Say which hand was declined and why, right in the progress output.
                on_event(f"  ✗ {key}: {res.output}")

        try:
            if rig.foreman:
                await _run_foreman(rig, task, worker, shift, record, on_event, tools)
            else:
                await _run_lines(rig, task, worker, shift, record, on_event, tools)
        except Exception as e:
            # Keep what the shift got done; shift.json records the crash instead of a traceback.
            shift.ok = False
            shift.error = f"{type(e).__name__}: {e}"
            on_event(f"✗ shift failed: {shift.error}")
        except BaseException:
            shift.ok = False
            shift.error = "interrupted"
            raise
        finally:
            if shift.worktrees:
                # Even if the shift crashed, keep whatever the hands wrote on the branch.
                first = task.strip().splitlines()[0][:60] if task.strip() else "shift"
                message = f"rig: {first}\n\nShift {shift.id} of rig '{rig.name}' ({'ok' if shift.ok else 'incomplete'})."
                several = len(shift.worktrees) > 1
                for wt in shift.worktrees:
                    try:
                        out = worktree.finish(wt, message)
                    except Exception as e:
                        # finish can still fail outside its git guard (diff/remove, missing dir);
                        # report the worktree as kept so the other worktrees and shift.json still happen.
                        out = worktree.Outcome(changed=True, commit=None, stat="", kept_at=wt.path,
                                               error=f"{type(e).__name__}: {e}")
                    shift.outcomes.append(out)
                    _report_worktree(wt, out, on_event, label=f" {wt.repo.name}" if several else "")
            summary = _write_summary(shift, rig, mode, task, meter)

        # A crashed shift keeps its branch for a look, but doesn't go to GitHub.
        if rig.publish.pr and shift.committed() and not shift.error:
            await _publish(rig, task, shift, on_event)
            summary = _write_summary(shift, rig, mode, task, meter)  # again, now with the PRs

        on_event(cost.summary(summary["totals"]))
        if meter.stop_reason:
            on_event(f"✗ stopped: {meter.message()}")
        on_event(f"logs → {shift.dir}")
        return shift
    finally:
        progress.close()


def _write_summary(shift: Shift, rig: Rig, mode: str, task: str, meter: cost.Meter) -> dict[str, Any]:
    summary = {
        "rig": rig.name,
        "mode": mode,
        "task": task,
        "inputs": shift.inputs,
        "ok": shift.ok,
        "error": shift.error,
        "branch": shift.branch,
        "worktrees": [
            {"repo": str(wt.repo), "branch": wt.branch, "base": wt.base, "commit": out.commit, "changed": out.changed,
             "kept_at": str(out.kept_at) if out.kept_at else None}
            for wt, out in zip(shift.worktrees, shift.outcomes)
        ],
        "hands": {
            k: {"stop_reason": r.stop_reason, "turns": r.turns, "model": r.model, "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens, "cache_read_tokens": r.cache_read_tokens,
                "cache_write_tokens": r.cache_write_tokens, "cost_usd": r.cost_usd}
            for k, r in shift.results.items()
        },
        "totals": _totals(list(shift.results.values())),
        "prs": [pr.as_dict() for pr in shift.prs],
        "max_cost_usd": meter.limit,
        "stopped": meter.stop_reason,
    }
    (shift.dir / "shift.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def _totals(results: list[HandResult]) -> dict[str, Any]:
    """Token sums over all hands; cost_usd is None if any hand that used tokens has no known price."""
    totals: dict[str, Any] = {
        key: sum(getattr(r, key) for r in results)
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }
    costs = [r.cost_usd for r in results if r.tokens]
    totals["cost_usd"] = None if None in costs else sum(costs, 0.0)
    return totals


def _latest(results: dict[str, HandResult], hand: str | None) -> HandResult | None:
    """The hand's last result: key `hand` in lines mode, the last `hand#n` in foreman mode."""
    found = [r for k, r in results.items() if hand and k.split("#")[0] == hand]
    return found[-1] if found else None


async def _publish(rig: Rig, task: str, shift: Shift, on_event: Event) -> None:
    policy = rig.publish
    review = _latest(shift.results, policy.approver)
    if not shift.ok:
        why_not = "the shift didn't finish cleanly"
    elif policy.approver and not (review and review.ok and publish.approved(review.output, policy.approve_word)):
        why_not = f"{policy.approver} didn't approve (no {policy.approve_word!r} in its last reply)"
    else:
        why_not = ""
    first = task.strip().splitlines()[0][:70] if task.strip() else f"shift {shift.id}"
    body = "\n\n".join(filter(None, [
        shift.final.output.strip() if shift.final else "",
        f"---\nShift `{shift.id}` of rig `{rig.name}` · est. cost {cost.usd(shift.meter.spent)}",
    ]))
    for wt, _ in shift.committed():
        base = policy.base or wt.base_branch
        if base in ("", "HEAD"):
            note = f"{wt.repo.name} was on a detached HEAD; set publish.base to the branch PRs should target"
            on_event(f"  ✗ no PR: {note}")
            shift.prs.append(publish.PullRequest(repo=wt.repo, branch=wt.branch, note=note))
            continue
        on_event(f"  publishing {wt.branch} → {base}" + (f" in {wt.repo.name}" if len(shift.worktrees) > 1 else ""))
        shift.prs.append(await asyncio.to_thread(
            publish.publish, wt.repo, wt.branch, base, f"rig: {first}", body,
            review.output if review else None, not why_not, why_not, policy, on_event,
            cancelled=lambda: shift.meter.stop_reason is not None,
        ))


def _start(folder: Path, sync: Publish | None, on_event: Event) -> str | None:
    """The commit to start from when syncing with the remote first; None means the repo's HEAD."""
    if not sync:
        return None
    repo = worktree.repo_of(folder)
    branch = sync.base or worktree.current_branch(repo)
    if branch == "HEAD":
        return None  # detached: nothing to sync with
    start, note = worktree.newest_start(repo, sync.remote, branch)
    if note:
        on_event(f"  {note}")
    return start


def _make_worktrees(
    shift: Shift, workspace: Path | dict[str, Path], root: Path, run_in: str | None, on_event: Event,
    sync: Publish | None = None,
) -> tuple[Path | dict[str, Path], worktree.Worktree]:
    """A worktree per git repo the workspace(s) live in; returns the mapped workspace and `run`'s worktree."""
    branch = f"rig/{shift.id}"
    dest = root / ".rig" / "worktrees" / shift.id
    if isinstance(workspace, Path):
        wt = worktree.create(workspace, dest, branch, _start(workspace, sync, on_event))
        shift.worktrees.append(wt)
        _announce(wt, on_event)
        return wt.map(workspace), wt

    # Check every project before creating anything, so a bad one leaves nothing behind.
    repos = {name: worktree.repo_of(folder) for name, folder in workspace.items()}
    by_repo: dict[Path, worktree.Worktree] = {}
    try:
        for name, folder in workspace.items():
            repo = repos[name]
            if repo not in by_repo:  # projects in the same repo share its worktree
                by_repo[repo] = worktree.create(folder, dest / name, branch, _start(folder, sync, on_event))
                shift.worktrees.append(by_repo[repo])
                _announce(by_repo[repo], on_event, label=f" {repo.name}")
    except worktree.GitError:
        for wt in shift.worktrees:
            worktree.discard(wt)
        shift.worktrees.clear()
        raise
    mapped = {name: by_repo[repos[name]].map(folder) for name, folder in workspace.items()}
    return mapped, by_repo[repos[run_in or next(iter(workspace))]]


def _announce(wt: worktree.Worktree, on_event: Event, label: str = "") -> None:
    on_event(f"  worktree{label} {wt.path} · branch {wt.branch} from {wt.base[:7]}")
    if wt.dirty:
        on_event(f"  ! {wt.repo.name} has uncommitted changes; they are not in the worktree")


def _report_worktree(wt: worktree.Worktree, out: worktree.Outcome, on_event: Event, label: str = "") -> None:
    if not out.changed:
        on_event(f"  worktree{label}: no changes; branch removed")
        return
    if out.kept_at:
        on_event(f"  ✗ couldn't commit on {wt.branch}{label and ' in' + label}; worktree kept at {out.kept_at}")
        on_event(f"    {out.error}")
        return
    on_event(f"  worktree{label}: committed {out.commit} on {wt.branch}")
    for line in out.stat.rstrip().splitlines():
        on_event(f"    {line}")
    where = f"  (in {wt.repo})" if label else ""
    on_event(f"  review:  git diff {wt.base[:7]}..{wt.branch}{where}")
    on_event(f"  merge:   git merge {wt.branch}    discard: git branch -D {wt.branch}")


def _done(res: HandResult, meter: cost.Meter | None = None) -> str:
    cached = f", {res.cache_read_tokens} cached" if res.cache_read_tokens else ""
    # The shift's running total, so a long run shows what it has cost so far.
    total = ""
    if meter and res.tokens:
        total = f" · shift {'≥' if meter.unpriced else ''}{cost.usd(meter.spent)}"
        if meter.limit is not None:
            total += f" of {cost.usd(meter.limit)}"
    return f"[{res.stop_reason}, {res.turns} turns, {res.input_tokens}/{res.output_tokens} tok{cached}]{total}"


async def _guarded_run(worker: Worker, hand, label: str, prompt: str, toolbox: Toolbox, on_event: Event) -> HandResult:
    """worker.run, with a crash turned into an error result so parallel hands still finish and get recorded."""
    try:
        return await worker.run(hand, prompt, toolbox)
    except Exception as e:
        on_event(f"  ✗ {label} crashed: {type(e).__name__}: {e}")
        return HandResult(name=hand.name, output=f"[error: {type(e).__name__}: {e}]", stop_reason="error",
                          turns=0, model=hand.model)


async def _run_lines(rig, task, worker, shift, record, on_event, tools: ToolFactory) -> None:
    edges = rig.edges

    async def run_hand(name: str) -> HandResult:
        hand = rig.resolve(name, shift.inputs)
        handoffs = {u: shift.results[u].output for u in upstreams(name, edges)}
        on_event(f"  ▶ {name}" + (f"  ← {', '.join(handoffs)}" if handoffs else ""))
        res = await _guarded_run(worker, hand, name, build_prompt(task, handoffs, inputs=shift.inputs),
                                 tools(hand.tools, name), on_event)
        on_event(f"  ■ {name}  {_done(res, shift.meter)}")
        return res

    for layer in layers(list(rig.hands), edges):
        if shift.meter.stop_reason:
            on_event(f"  ✗ not starting {', '.join(layer)}: {shift.meter.message()}")
            return
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

        hand = rig.resolve(name, shift.inputs)
        on_event(f"  ↳ {key}  {instructions.strip().splitlines()[0][:70] if instructions.strip() else ''}")
        res = await _guarded_run(worker, hand, key, build_prompt(task, {}, instructions, inputs=shift.inputs),
                                 tools(hand.tools, key), on_event)
        on_event(f"  ■ {key}  {_done(res, shift.meter)}")
        record(key, res)
        if not res.ok:
            raise ToolError(f"{name} did not finish cleanly ({res.stop_reason}): {res.output}")
        seq += 1
        if name in foreman.require:
            last_required[name] = seq
        else:
            last_other = seq
        return res.output

    roster = "\n".join(f"- {n}: {rig.resolve(n, shift.inputs).role.strip()}" for n in crew)
    rules = ""
    if foreman.require:
        rules = (
            f"\n\nBefore you finish, {', '.join(foreman.require)} must run after the last change by any "
            "other hand. If they report problems, send the fix back and have them check again."
        )
    hand = rig.resolve("foreman", shift.inputs)
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
    res = await worker.run(hand, build_prompt(task, {}, inputs=shift.inputs), toolbox, check=check if foreman.require else None)
    on_event(f"  ■ foreman  {_done(res, shift.meter)} · {total} delegations")
    record("foreman", res)
    shift.final = res
    shift.ok = res.ok and not unmet_at_finish
