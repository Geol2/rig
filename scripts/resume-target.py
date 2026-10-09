"""Print the id of the shift scripts/next.sh should continue with `rig run --resume`, or nothing.

The newest shift is continued when it ran the same task, didn't end ok, hasn't been resumed
twice already (its `resumed_from` chain), and `load_resume` accepts it (branch with commits,
not running, same rig). Otherwise nothing is printed and next.sh starts afresh; when a shift
of the same task is passed over, stderr says why. Always exits 0.

    PYTHONPATH=src python scripts/resume-target.py self.rig.yaml "<task>"
"""

import sys
from pathlib import Path

MAX_RESUMES = 2  # a shift that is already the second resume in a row is not continued again


def _chain(shifts_dir: Path, summary: dict) -> int:
    """How many resumes led to this shift: follow `resumed_from` while the folders exist."""
    from rig.report import read_summary

    seen: set[str] = set()
    prev = summary.get("resumed_from")
    while isinstance(prev, str) and prev and prev not in seen:
        seen.add(prev)
        shift_dir = shifts_dir / prev
        if not (shift_dir / "shift.json").is_file():
            break
        prev = read_summary(shift_dir)[0].get("resumed_from")
    return len(seen)


def target(rig_file: str, task: str) -> str | None:
    from rig import spec
    from rig.report import read_summary
    from rig.runner import ResumeError, load_resume
    from rig.worktree import GitError

    root = Path(rig_file).resolve().parent
    try:
        rig = spec.load(rig_file)
    except Exception:
        return None
    shifts_dir = root / ".rig" / "shifts"
    shifts = sorted(d for d in shifts_dir.iterdir() if d.is_dir()) if shifts_dir.is_dir() else []
    if not shifts:
        return None
    shift_dir = shifts[-1]
    summary, reason = read_summary(shift_dir)
    if reason is not None or not summary:
        return None
    if str(summary.get("task") or "").strip() != task.strip():
        return None
    if summary.get("ok") and not summary.get("error") and not summary.get("stopped"):
        return None
    old = shift_dir.name
    prs = summary.get("prs")
    if isinstance(prs, list) and any(isinstance(pr, dict) and pr.get("merged") for pr in prs):
        print(f"! 이어 하지 않고 새로 시작: {old} (이미 병합된 PR 있음)", file=sys.stderr)
        return None
    if _chain(shifts_dir, summary) >= MAX_RESUMES:
        print(f"! 이어 하지 않고 새로 시작: {old} (이미 두 번 이어 했음)", file=sys.stderr)
        return None
    try:
        load_resume(root, rig, old)
    except (ResumeError, GitError) as exc:
        text = str(exc)
        why = ("브랜치에 이어 할 커밋 없음" if "left no committed branch" in text or "no longer exists" in text
               else "이어 할 수 없음")
        print(f"! 이어 하지 않고 새로 시작: {old} ({why})", file=sys.stderr)
        return None
    return old


def main() -> None:
    if len(sys.argv) != 3:
        return
    try:
        old = target(sys.argv[1], sys.argv[2])
    except (Exception, SystemExit):
        return  # anything unexpected: start afresh
    if old:
        print(old)


if __name__ == "__main__":
    main()
