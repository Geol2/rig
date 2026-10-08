"""After a --worktree shift: push its branch, open a pull request, and merge it when allowed.

Uses `git` and the GitHub CLI (`gh`, logged in with `gh auth login`) in the repository the
branch was made in. Merging needs all of: the shift finished cleanly, the approver hand's
last word is the approval word (e.g. LGTM), and every CI check on the PR passed.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rig.spec import Publish

# (args, cwd, stdin) -> (exit code, combined output). Swapped out in tests.
Runner = Callable[[list[str], Path, str | None], tuple[int, str]]


def run_cmd(args: list[str], cwd: Path, stdin: str | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, f"{args[0]} not found; install it (GitHub CLI: https://cli.github.com) and run `gh auth login`"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass
class PullRequest:
    repo: Path
    branch: str
    url: str | None = None
    number: int | None = None
    merged: bool = False
    note: str = ""  # why it wasn't opened or merged

    def as_dict(self) -> dict:
        return {"repo": str(self.repo), "branch": self.branch, "url": self.url, "number": self.number,
                "merged": self.merged, "note": self.note}


def approved(output: str | None, word: str) -> bool:
    """Whether a reviewer's reply approves: some line starts with the approval word.

    Only the start of a line counts (after Markdown marks like `**`, `-`, `#`), so
    "not LGTM yet" or "LGTM once the test passes" in the middle of a sentence doesn't.
    """
    pattern = re.compile(rf"^[\s*_#>`-]*{re.escape(word)}(?![\w-])", re.M)
    return bool(output) and pattern.search(output) is not None


def publish(
    repo: Path, branch: str, base: str, title: str, body: str, review: str | None, can_merge: bool,
    why_not: str, policy: Publish, on_event: Callable[[str], None], run: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
) -> PullRequest:
    pr = PullRequest(repo=repo, branch=branch)
    run = run or run_cmd

    def step(*args: str, stdin: str | None = None) -> tuple[int, str]:
        return run(list(args), repo, stdin)


    code, out = step("git", "push", "-u", policy.remote, branch)
    if code:
        pr.note = f"push failed: {out}"
        on_event(f"  ✗ {pr.note}")
        return pr
    code, out = step("gh", "pr", "create", "--head", branch, "--base", policy.base or base, "--title", title,
                     "--body-file", "-", stdin=body)
    if code:
        pr.note = f"couldn't open a PR: {out}"
        on_event(f"  ✗ {pr.note}")
        return pr
    pr.url = out.splitlines()[-1].strip()
    m = re.search(r"/pull/(\d+)", pr.url)
    pr.number = int(m.group(1)) if m else None
    on_event(f"  PR opened: {pr.url}")
    if review:
        # A comment, not an approval: GitHub doesn't let an account approve its own PR.
        code, out = step("gh", "pr", "review", pr.url, "--comment", "--body-file", "-", stdin=review)
        if code:
            on_event(f"  ! couldn't post the review: {out}")

    if not policy.auto_merge:
        pr.note = "auto_merge is off; merge it yourself after reviewing"
        return pr
    if not can_merge:
        pr.note = f"not merged: {why_not}"
        on_event(f"  PR left open ({why_not})")
        return pr

    on_event(f"  waiting for CI on the PR (up to {policy.ci_timeout}s)")
    verdict = _wait_for_checks(pr.url, step, policy, sleep, clock, cancelled)
    if verdict != "pass":
        pr.note = f"not merged: {verdict}"
        on_event(f"  PR left open ({verdict})")
        return pr
    code, out = step("gh", "pr", "merge", pr.url, f"--{policy.merge_method}", "--delete-branch")
    if code:
        pr.note = f"merge failed: {out}"
        on_event(f"  ✗ {pr.note}")
        return pr
    pr.merged = True
    on_event(f"  PR merged ({policy.merge_method}): {pr.url}")
    return pr


def _wait_for_checks(url: str, step, policy: Publish, sleep, clock, cancelled) -> str:
    """'pass', or why not: a check failed, none ran, time ran out, or the shift was stopped."""
    start = clock()
    while True:
        if cancelled():
            return "stopped by user while waiting for CI"
        code, out = step("gh", "pr", "checks", url, "--json", "name,bucket")
        try:
            checks = json.loads(out) if out.startswith("[") else []
        except ValueError:
            checks = []
        buckets = {c.get("bucket") for c in checks}
        failed = [c["name"] for c in checks if c.get("bucket") in ("fail", "cancel")]
        if failed:
            return f"CI failed: {', '.join(failed)}"
        if checks and buckets <= {"pass", "skipping"}:
            return "pass"
        elapsed = clock() - start
        if not checks and elapsed >= policy.ci_grace:
            if policy.require_checks:
                return "no CI checks ran on the PR (set publish.require_checks: false to merge without CI)"
            return "pass"
        if elapsed >= policy.ci_timeout:
            return f"CI still running after {policy.ci_timeout}s"
        sleep(policy.ci_poll)
