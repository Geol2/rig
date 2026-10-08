"""rig command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from rig import __version__
from rig.cost import summary as cost_summary
from rig.cost import usd
from rig.graph import layers
from rig.spec import InputError, Rig, load

DEFAULT_FILE = "rig.yaml"


def _load_or_exit(path: Path) -> Rig:
    if not path.exists():
        sys.exit(f"rig: {path} not found (run `rig init` to create one)")
    try:
        return load(path)
    except (ValidationError, yaml.YAMLError) as e:
        sys.exit(f"rig: {path} is invalid\n{e}")


def _check_workspace(rig: Rig, path: Path) -> None:
    workspace = path.resolve().parent / rig.workspace
    if not workspace.is_dir():
        sys.exit(f"rig: workspace {rig.workspace} not found (set `workspace` in {path} to the project folder)")


def cmd_init(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if path.exists() and not args.force:
        sys.exit(f"rig: {path} already exists (use --force to overwrite)")
    template = {"lines": "template.yaml", "foreman": "template-foreman.yaml", "review": "template-review.yaml"}[args.template]
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
    if rig.inputs:
        def describe(s) -> str:
            if s.default is not None:
                return f"default {s.default_text()}"
            return "required" if s.required else "optional"

        print(f"  inputs: {', '.join(f'{n} ({describe(s)})' for n, s in rig.inputs.items())}")


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
                                      verbose=not args.quiet, inputs=inputs))
    except GitError as e:
        sys.exit(f"rig: {e}")
    if shift.final:
        print(f"\n── {shift.final.name} ──\n{shift.final.output}")
    out = shift.outcome
    if out and out.changed and not out.kept_at:
        print(f"\nchanges are on branch {shift.worktree.branch} (review: git diff {shift.worktree.base[:7]}..{shift.worktree.branch})")
    if not shift.ok:
        sys.exit(1)


def _read_summary(shift_dir: Path) -> dict:
    path = shift_dir / "shift.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cmd_logs(args: argparse.Namespace) -> None:
    shifts_dir = Path(args.file).resolve().parent / ".rig" / "shifts"
    shifts = sorted(shifts_dir.iterdir()) if shifts_dir.is_dir() else []
    if not shifts:
        sys.exit("rig: no shifts yet")
    if args.shift is None:
        summaries = [_read_summary(s) for s in shifts]
        # The branch column only appears when some shift ran with --worktree and left changes.
        branch_width = max((len(m.get("branch") or "") for m in summaries), default=0)
        for s, summary in zip(shifts, summaries):
            status = "ok" if summary.get("ok") else "incomplete"
            # Shifts from before cost tracking have no totals.
            price = usd(summary["totals"].get("cost_usd")) if "totals" in summary else ""
            branch = f"{summary.get('branch') or '':<{branch_width}}  " if branch_width else ""
            task = (summary.get("task") or "").strip().splitlines()
            print(f"{s.name}  {summary.get('mode', ''):<8}  {status:<10}  {price:>9}  {branch}{task[0][:60] if task else ''}")
        return
    shift = shifts[-1] if args.shift == "last" else shifts_dir / args.shift
    if not shift.is_dir():
        sys.exit(f"rig: no shift {args.shift}")
    summary = _read_summary(shift)
    if "totals" in summary:
        print(f"{cost_summary(summary['totals'])}\n")
    for f in sorted(shift.glob("*.md")):
        print(f"── {f.stem} ──\n{f.read_text(encoding='utf-8')}\n")


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
        # Line-buffered so progress shows up live even when piped (e.g. through grep or tee).
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser(prog="rig", description="Define a multi-agent harness, then run it.")
    p.add_argument("--version", action="version", version=f"rig {__version__}")
    p.add_argument("-f", "--file", default=DEFAULT_FILE, help="rig definition (default: rig.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter rig.yaml")
    s.add_argument("--force", action="store_true")
    s.add_argument("--template", choices=["lines", "foreman", "review"], default="lines",
                   help="fixed handoff lines, a foreman that delegates at run time, or a read-only code review")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("check", help="validate rig.yaml and show the stages")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="run a shift")
    s.add_argument("task", nargs="?", help="task text (default: read from stdin)")
    s.add_argument("--dry", action="store_true", help="no API calls; show what each hand would receive")
    s.add_argument("--worktree", action="store_true",
                   help="work in a new git worktree; changes are committed to branch rig/<shift-id> for review")
    s.add_argument("-q", "--quiet", action="store_true", help="don't show individual tool calls")
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
