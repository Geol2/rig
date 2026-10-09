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
    from_branch: str = ""  # branch of the resumed shift whose tip `base` is (`rig run --resume`)

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


MAX_BRANCH_TRIES = 100


def _branch_exists(repo: Path, name: str) -> bool:
    try:
        git("show-ref", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo)
    except GitError:
        return False
    return True


def current_branch(repo: Path) -> str:
    """The branch checked out in `repo` ("HEAD" if detached)."""
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()


def _is_ancestor(a: str, b: str, repo: Path) -> bool:
    try:
        git("merge-base", "--is-ancestor", a, b, cwd=repo)
        return True
    except GitError:
        return False


def newest_start(repo: Path, remote: str, branch: str) -> tuple[str, str]:
    """Fetch `remote`/`branch` and pick the commit a shift should start from, with a note.

    Local behind the remote (e.g. rig merged a PR on GitHub since): start from the remote.
    Local ahead (commits not pushed yet, like a new TODO item): start from local, so they're
    in. Diverged, or the fetch failed: start from local and say so.
    """
    head = git("rev-parse", "HEAD", cwd=repo).strip()
    try:
        git("fetch", "-q", remote, branch, cwd=repo)
        upstream = git("rev-parse", "FETCH_HEAD", cwd=repo).strip()
    except GitError as e:
        return head, f"! couldn't fetch {remote}/{branch}, starting from local {branch}: {str(e).splitlines()[0]}"
    if upstream == head or _is_ancestor(upstream, head, repo):
        return head, ""
    if _is_ancestor(head, upstream, repo):
        return upstream, f"local {branch} is behind {remote}; starting from {remote}/{branch} ({upstream[:7]})"
    return head, f"! local {branch} and {remote}/{branch} have diverged; starting from local {branch}"


def create(workspace: Path, dest: Path, branch: str, start: str | None = None) -> Worktree:
    """A new worktree at `dest` on a new branch named `branch` (or `branch-2`, ... if taken),
    from `start` (a commit) or else the repo's HEAD."""
    repo = repo_of(workspace)
    base = start or git("rev-parse", "HEAD", cwd=repo).strip()
    dirty = bool(git("status", "--porcelain", cwd=repo).strip())
    dest.parent.mkdir(parents=True, exist_ok=True)
    base_branch = current_branch(repo)
    # Rig roots sharing a repo can start shifts with the same id; take branch, branch-2, branch-3, ...
    # `git branch` claims each one atomically; if another root got it first, move on to the next.
    for n in range(1, MAX_BRANCH_TRIES + 1):
        name = branch if n == 1 else f"{branch}-{n}"
        if _branch_exists(repo, name):
            continue
        try:
            git("branch", name, base, cwd=repo)
        except GitError:
            if _branch_exists(repo, name):
                continue
            raise
        try:
            git("worktree", "add", str(dest), name, cwd=repo)
        except GitError:
            git("branch", "-D", name, cwd=repo)
            raise
        return Worktree(repo=repo, path=dest.resolve(), branch=name, base=base, dirty=dirty, base_branch=base_branch)
    raise GitError(f"no free branch name for {branch} after {MAX_BRANCH_TRIES} tries")


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
