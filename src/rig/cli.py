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

from rig import __version__, pricing
from rig.graph import layers
from rig.spec import Rig, load

DEFAULT_FILE = "rig.yaml"


def _load_or_exit(path: Path) -> Rig:
    if not path.exists():
        sys.exit(f"rig: {path} not found (run `rig init` to create one)")
    try:
        return load(path)
    except (ValidationError, yaml.YAMLError) as e:
        sys.exit(f"rig: {path} is invalid\n{e}")


def cmd_init(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if path.exists() and not args.force:
        sys.exit(f"rig: {path} already exists (use --force to overwrite)")
    template = {"lines": "template.yaml", "foreman": "template-foreman.yaml"}[args.template]
    path.write_text(resources.files("rig").joinpath(template).read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {path} ({args.template})")


def cmd_check(args: argparse.Namespace) -> None:
    rig = _load_or_exit(Path(args.file))
    if rig.foreman:
        print(f"✓ {rig.name}: foreman + {len(rig.crew)} hands")
        print(f"  foreman → {' | '.join(rig.crew)}  (up to {rig.foreman.max_delegations} delegations)")
        return
    print(f"✓ {rig.name}: {len(rig.hands)} hands, {len(rig.edges)} lines")
    for i, layer in enumerate(layers(list(rig.hands), rig.edges), 1):
        print(f"  stage {i}: {' | '.join(layer)}")


def cmd_run(args: argparse.Namespace) -> None:
    from rig.hand import ClaudeWorker, EchoWorker
    from rig.runner import run_shift

    path = Path(args.file)
    rig = _load_or_exit(path)
    task = args.task if args.task is not None else sys.stdin.read()
    if not task.strip():
        sys.exit("rig: empty task (pass it as an argument or on stdin)")
    from rig.worktree import GitError

    worker = EchoWorker() if args.dry else ClaudeWorker()
    try:
        shift = asyncio.run(run_shift(rig, task, worker, root=path.resolve().parent, use_worktree=args.worktree, verbose=not args.quiet))
    except GitError as e:
        sys.exit(f"rig: {e}")
    if shift.final:
        print(f"\n── {shift.final.name} ──\n{shift.final.output}")
    out = shift.outcome
    if out and out.changed and not out.kept_at:
        print(f"\nchanges are on branch {shift.worktree.branch} (review: git diff {shift.worktree.base[:7]}..{shift.worktree.branch})")
    if not shift.ok:
        sys.exit(1)


def cmd_logs(args: argparse.Namespace) -> None:
    shifts_dir = Path(args.file).resolve().parent / ".rig" / "shifts"
    shifts = sorted(shifts_dir.iterdir()) if shifts_dir.is_dir() else []
    if not shifts:
        sys.exit("rig: no shifts yet")
    if args.shift is None:
        for s in shifts:
            summary = json.loads((s / "shift.json").read_text(encoding="utf-8")) if (s / "shift.json").exists() else {}
            status = "ok" if summary.get("ok") else "incomplete"
            task = (summary.get("task") or "").strip().splitlines()
            # Shifts logged before cost tracking have no "usage"; show those blank rather than n/a.
            cost = pricing.usd(summary["usage"]["cost_usd"]) if "usage" in summary else ""
            print(f"{s.name}  {summary.get('mode', ''):<8}  {status:<10}  {cost:>9}  {task[0][:60] if task else ''}")
        return
    shift = shifts[-1] if args.shift == "last" else shifts_dir / args.shift
    if not shift.is_dir():
        sys.exit(f"rig: no shift {args.shift}")
    for f in sorted(shift.glob("*.md")):
        print(f"── {f.stem} ──\n{f.read_text(encoding='utf-8')}\n")


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
    s.add_argument("--template", choices=["lines", "foreman"], default="lines",
                   help="fixed handoff lines, or a foreman that delegates at run time")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("check", help="validate rig.yaml and show the stages")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="run a shift")
    s.add_argument("task", nargs="?", help="task text (default: read from stdin)")
    s.add_argument("--dry", action="store_true", help="no API calls; show what each hand would receive")
    s.add_argument("--worktree", action="store_true",
                   help="work in a new git worktree; changes are committed to branch rig/<shift-id> for review")
    s.add_argument("-q", "--quiet", action="store_true", help="don't show individual tool calls")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("logs", help="list shifts, or show one (`last` or a shift id)")
    s.add_argument("shift", nargs="?")
    s.set_defaults(func=cmd_logs)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
