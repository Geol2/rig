"""rig command line."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from rig import __version__
from rig.cost import summary as cost_summary
from rig.cost import Meter, price_warnings, usd
from rig.graph import layers
from rig.spec import InputError, Rig, RigFileError, load

DEFAULT_FILE = "rig.yaml"


def _load_or_exit(path: Path) -> Rig:
    if not path.exists():
        sys.exit(f"rig: {path} not found (run `rig init` to create one)")
    try:
        return load(path)
    except (ValidationError, yaml.YAMLError, RigFileError) as e:
        sys.exit(f"rig: {path} is invalid\n{e}")


def _check_workspace(rig: Rig, path: Path) -> None:
    for name, folder in rig.workspace_dirs(path.resolve().parent).items():
        if not folder.is_dir():
            if name == ".":
                sys.exit(f"rig: workspace {rig.workspace} not found (set `workspace` in {path} to the project folder)")
            sys.exit(f"rig: workspace {name} ({rig.workspaces[name]}) not found (fix it under `workspaces` in {path})")


def cmd_init(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if path.exists() and not args.force:
        sys.exit(f"rig: {path} already exists (use --force to overwrite)")
    template = {"lines": "template.yaml", "foreman": "template-foreman.yaml", "review": "template-review.yaml",
                "fix": "template-fix.yaml"}[args.template]
    path.write_text(resources.files("rig").joinpath(template).read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {path} ({args.template})")


def cmd_check(args: argparse.Namespace) -> None:
    rig = _load_or_exit(Path(args.file))
    _check_workspace(rig, Path(args.file))
    if rig.foreman:
        print(f"✓ {rig.name}: foreman + {len(rig.crew)} hands")
        print(f"  foreman → {' | '.join(rig.crew)}  (up to {rig.foreman.max_delegations} delegations)")
    else:
        print(f"✓ {rig.name}: {len(rig.hands)} hands, {len(rig.edges)} lines")
        for i, layer in enumerate(layers(list(rig.hands), rig.edges), 1):
            print(f"  stage {i}: {' | '.join(layer)}")
    if rig.workspaces:
        print(f"  workspaces: {', '.join(f'{n} ({p})' for n, p in rig.workspaces.items())}")
    if rig.inputs:
        def describe(s) -> str:
            if s.default is not None:
                return f"default {s.default_text()}"
            return "required" if s.required else "optional"

        print(f"  inputs: {', '.join(f'{n} ({describe(s)})' for n, s in rig.inputs.items())}")
    for line in [*rig.hand_warnings(), *price_warnings(rig.models(), rig.max_cost_usd)]:
        print(line)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            sys.exit(f"rig: bad input {pair!r}; expected -i name=value")
        values[name.strip()] = value
    return values


def cmd_run(args: argparse.Namespace) -> None:
    from rig.hand import ClaudeWorker, EchoWorker
    from rig.runner import run_shift

    path = Path(args.file)
    rig = _load_or_exit(path)
    _check_workspace(rig, path)
    if args.max_cost is not None and args.max_cost <= 0:
        sys.exit("rig: --max-cost must be more than 0")
    inputs = _parse_inputs(args.input)
    try:
        rig.resolve_inputs(inputs)  # fail before reading stdin or starting the shift
    except InputError as e:
        sys.exit(f"rig: {e}")
    task = args.task if args.task is not None else sys.stdin.read()
    if not task.strip():
        sys.exit("rig: empty task (pass it as an argument or on stdin)")
    from rig.worktree import GitError

    worker = EchoWorker() if args.dry else ClaudeWorker()
    try:
        shift = asyncio.run(run_shift(rig, task, worker, root=path.resolve().parent, use_worktree=args.worktree,
                                      verbose=not args.quiet, inputs=inputs, meter=Meter(args.max_cost)))
    except GitError as e:
        sys.exit(f"rig: {e}")
    for r in shift.finals:
        print(f"\n── {r.name} ──\n{r.output}")
    committed = shift.committed()
    for wt, _ in committed:
        where = f" in {wt.repo}" if len(shift.worktrees) > 1 else ""
        print(f"\nchanges are on branch {wt.branch}{where} (review: git diff {wt.base[:7]}..{wt.branch})")
    for pr in shift.prs:
        state = "merged" if pr.merged else pr.note or "open"
        print(f"PR {pr.url or '(not opened)'}: {state}")
    if not shift.ok:
        sys.exit(1)


def _status(summary: dict, running: bool, unreadable: bool = False) -> str:
    if running:
        return "running"
    if unreadable:  # a broken shift.json: no telling how the shift ended
        return "unreadable"
    if summary.get("error"):  # a crash or "interrupted" says more than the stop that may have led to it
        return "error"
    if summary.get("stopped"):
        return "stopped"
    return "ok" if summary.get("ok") else "incomplete"


def _stop_line(summary: dict) -> str:
    if summary["stopped"] != "budget":
        return "✗ stopped: stopped by user"
    spent = usd((summary.get("totals") or {}).get("cost_usd"))
    limit = summary.get("max_cost_usd")
    if limit is None:
        return f"✗ stopped: cost limit reached ({spent} spent)"
    return f"✗ stopped: cost limit {usd(limit)} reached ({spent} spent)"


def _natural(name: str) -> list:
    """Sort key that compares digit runs as numbers, so coder-2 comes before coder-10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _outputs(shift: Path, summary: dict) -> list[tuple[str, Path]]:
    """(heading, file) per hand output: shift.json's hands in run order, then any other .md."""
    out = [(key, shift / f"{key.replace('#', '-')}.md") for key in summary.get("hands") or {}]
    out = [(key, f) for key, f in out if f.exists()]
    seen = {f.name for _, f in out}
    rest = sorted((f for f in shift.glob("*.md") if f.name not in seen), key=lambda f: _natural(f.stem))
    return out + [(f.stem, f) for f in rest]


def cmd_logs(args: argparse.Namespace) -> None:
    from rig.report import read_summary
    from rig.runner import live

    shifts_dir = Path(args.file).resolve().parent / ".rig" / "shifts"
    shifts = sorted(d for d in shifts_dir.iterdir() if d.is_dir()) if shifts_dir.is_dir() else []
    if not shifts:
        sys.exit("rig: no shifts yet")
    if args.shift is None:
        read = [read_summary(s) for s in shifts]
        # The branch column only appears when some shift ran with --worktree and left changes.
        branch_width = max((len(m.get("branch") or "") for m, _ in read), default=0)
        for s, (summary, reason) in zip(shifts, read):
            info = live(s)
            status = _status(summary, bool(info), reason is not None)
            # Shifts from before cost tracking have no totals; a running shift has no shift.json numbers yet.
            price = usd(summary["totals"].get("cost_usd")) if "totals" in summary and not info else ""
            mode = "" if info else summary.get("mode", "")
            branch = f"{'' if info else summary.get('branch') or '':<{branch_width}}  " if branch_width else ""
            task = (summary.get("task") or (info or {}).get("task") or "").strip().splitlines()
            print(f"{s.name}  {mode:<8}  {status:<10}  {price:>9}  {branch}{task[0][:60] if task else ''}")
        return
    shift = shifts[-1] if args.shift == "last" else shifts_dir / args.shift
    if not shift.is_dir():
        sys.exit(f"rig: no shift {args.shift}")
    summary, reason = read_summary(shift)
    head = []
    if reason is not None:
        head.append(f"✗ cannot read shift.json ({reason}); showing hand outputs only")
    if summary.get("error"):
        head.append(f"✗ shift failed: {summary['error']}")
    if summary.get("stopped"):
        head.append(_stop_line(summary))
    info = live(shift)
    if info:
        head.append(f"still running (pid {info.get('pid')}). Output so far:")
    if "totals" in summary:
        head.append(cost_summary(summary["totals"]))
    if head:
        print("\n".join(head) + "\n")
    for heading, f in _outputs(shift, summary):
        print(f"── {heading} ──\n{f.read_text(encoding='utf-8')}\n")


def cmd_report(args: argparse.Namespace) -> None:
    from rig import report

    shifts_dir = Path(args.file).resolve().parent / ".rig" / "shifts"
    shifts = sorted(shifts_dir.iterdir()) if shifts_dir.is_dir() else []
    if not shifts:
        sys.exit("rig: no shifts yet")
    shift = shifts[-1] if args.shift == "last" else shifts_dir / args.shift
    if not shift.is_dir():
        sys.exit(f"rig: no shift {args.shift}")
    path = report.write(shift)
    print(f"report → {path}")
    if not args.no_open:
        import webbrowser

        webbrowser.open(path.as_uri())


def cmd_serve(args: argparse.Namespace) -> None:
    from rig.serve import serve

    serve(Path(args.file).resolve().parent, port=args.port, open_browser=not args.no_open)


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # Line-buffered so progress shows up live even when piped (e.g. through grep or tee);
        # errors="replace" so a lone surrogate in a hand's output prints as "?" instead of raising.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    p = argparse.ArgumentParser(prog="rig", description="Define a multi-agent harness, then run it.")
    p.add_argument("--version", action="version", version=f"rig {__version__}")
    p.add_argument("-f", "--file", default=DEFAULT_FILE, help="rig definition (default: rig.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter rig.yaml")
    s.add_argument("--force", action="store_true")
    s.add_argument("--template", choices=["lines", "foreman", "review", "fix"], default="lines",
                   help="fixed handoff lines, a foreman that delegates at run time, a read-only code review, "
                        "or a fix-and-check rig for one problem")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("check", help="validate rig.yaml and show the stages")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="run a shift")
    s.add_argument("task", nargs="?", help="task text (default: read from stdin)")
    s.add_argument("--dry", action="store_true", help="no API calls; show what each hand would receive")
    s.add_argument("--worktree", action="store_true",
                   help="work in a new git worktree; changes are committed to branch rig/<shift-id> for review")
    s.add_argument("-q", "--quiet", action="store_true", help="don't show individual tool calls")
    s.add_argument("--max-cost", type=float, metavar="USD",
                   help="stop once the shift's estimated cost reaches this (overrides max_cost_usd)")
    s.add_argument("-i", "--input", action="append", default=[], metavar="NAME=VALUE",
                   help="value for an input declared under `inputs` (repeatable)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("logs", help="list shifts, or show one (`last` or a shift id)")
    s.add_argument("shift", nargs="?")
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("report", help="write a shift's results as an HTML page and open it")
    s.add_argument("shift", nargs="?", default="last", help="`last` (default) or a shift id")
    s.add_argument("--no-open", action="store_true", help="only write the file")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("serve", help="open a local web page to run rigs and browse reports")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-open", action="store_true", help="don't open the browser")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
