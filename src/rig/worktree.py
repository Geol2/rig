"""Running a shift in a throwaway git worktree, so changes land on a branch for review."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class GitError(RuntimeError):
    pass


def trusting(path: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment that adds `safe.directory=<path>` for git, scoped to that process only.

    On filesystems without ownership info (FAT/exFAT drives) git refuses every new
    worktree path as "dubious ownership". rig created the worktree itself from a repo
    the user already works in, so it trusts exactly that path, without touching config.
    """
    env = dict(os.environ if base is None else base)
    n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    # core.longpaths: tools run in the worktree (e.g. `uv run` making a .venv) create
    # paths past Windows' 260-char limit, which git otherwise can't delete.
    for i, (key, value) in enumerate([("safe.directory", path.as_posix()), ("core.longpaths", "true")], n):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_CONFIG_COUNT"] = str(n + 2)
    return env


def _remove(wt: Worktree) -> None:
    """Remove the worktree directory and git's record of it, even with over-long paths."""
    try:
        git("worktree", "remove", "--force", str(wt.path), cwd=wt.repo, env=wt.env)
    except GitError:
        target = str(wt.path)
        if os.name == "nt" and not target.startswith("\\\\?\\"):
            target = "\\\\?\\" + target
        shutil.rmtree(target, ignore_errors=True)
        git("worktree", "prune", cwd=wt.repo)


def git(*args: str, cwd: Path, env: Mapping[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


@dataclass
class Worktree:
    repo: Path       # main working tree
    path: Path       # the worktree directory
    branch: str
    base: str        # commit the branch started from
    dirty: bool      # main working tree had uncommitted changes (not carried over)
    base_branch: str = ""  # branch checked out in the main working tree ("HEAD" if detached)

    def map(self, workspace: Path) -> Path:
        """The same location as `workspace`, inside the worktree."""
        return self.path / workspace.resolve().relative_to(self.repo)

    @property
    def env(self) -> dict[str, str]:
        """Environment for commands run inside the worktree (by rig or by hands)."""
        env = trusting(self.path)
        # rig's own virtualenv isn't the worktree's; let tools like uv pick the right one.
        env.pop("VIRTUAL_ENV", None)
        return env


def repo_of(workspace: Path) -> Path:
    """The git repository (main working tree) that holds `workspace`."""
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=workspace).strip()).resolve()
    except (GitError, FileNotFoundError, NotADirectoryError) as e:
        raise GitError(f"--worktree needs the workspace to be in a git repository: {workspace}") from e


def create(workspace: Path, dest: Path, branch: str) -> Worktree:
    repo = repo_of(workspace)
    base = git("rev-parse", "HEAD", cwd=repo).strip()
    dirty = bool(git("status", "--porcelain", cwd=repo).strip())
    dest.parent.mkdir(parents=True, exist_ok=True)
    base_branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()
    git("worktree", "add", "-b", branch, str(dest), base, cwd=repo)
    return Worktree(repo=repo, path=dest.resolve(), branch=branch, base=base, dirty=dirty, base_branch=base_branch)


def discard(wt: Worktree) -> None:
    """Undo `create`: remove the worktree and its branch (nothing was done in it yet)."""
    _remove(wt)
    git("branch", "-D", wt.branch, cwd=wt.repo)


@dataclass
class Outcome:
    changed: bool
    commit: str | None
    stat: str
    kept_at: Path | None  # worktree left in place (e.g. commit failed)
    error: str | None


def finish(wt: Worktree, message: str) -> Outcome:
    """Commit everything the shift changed onto the branch, then remove the worktree.

    No changes: the branch is deleted too. If committing fails, the worktree is kept
    so nothing is lost.
    """
    env = wt.env
    try:
        git("add", "-A", cwd=wt.path, env=env)
        if not git("status", "--porcelain", cwd=wt.path, env=env).strip():
            _remove(wt)
            git("branch", "-D", wt.branch, cwd=wt.repo)
            return Outcome(changed=False, commit=None, stat="", kept_at=None, error=None)
        git("commit", "-q", "-m", message, cwd=wt.path, env=env)
        commit = git("rev-parse", "--short", "HEAD", cwd=wt.path, env=env).strip()
    except GitError as e:
        # Leave the worktree as is so nothing the hands wrote is lost.
        return Outcome(changed=True, commit=None, stat="", kept_at=wt.path, error=str(e))
    stat = git("diff", "--stat", f"{wt.base}..{wt.branch}", cwd=wt.repo)
    _remove(wt)
    return Outcome(changed=True, commit=commit, stat=stat, kept_at=None, error=None)
