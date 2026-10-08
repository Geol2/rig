"""publish: push the shift's branch, open a PR, post the review, merge only when allowed."""

import asyncio
import json
import subprocess

import pytest
from pydantic import ValidationError

from rig import publish
from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import Publish, Rig
from rig.worktree import git

URL = "https://github.com/o/r/pull/7"


class FakeGh:
    """Answers git push / gh calls; `checks` is a list of successive `gh pr checks` answers."""

    def __init__(self, checks=None, fail=None):
        self.calls = []
        self.checks = list(checks or [])
        self.fail = fail or set()

    def __call__(self, args, cwd, stdin=None):
        self.calls.append((args, stdin))
        key = " ".join(args[:3])
        if key in self.fail:
            return 1, f"{key}: boom"
        if args[:3] == ["gh", "pr", "create"]:
            return 0, f"Creating pull request\n{URL}"
        if args[:3] == ["gh", "pr", "checks"]:
            answer = self.checks.pop(0) if len(self.checks) > 1 else (self.checks[0] if self.checks else None)
            return (0, json.dumps(answer)) if answer is not None else (1, "no checks reported on the 'x' branch")
        return 0, ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[0][: len(prefix)] == list(prefix)]


def go(gh, *, can_merge=True, why_not="", review="looks good\nLGTM", cancelled=lambda: False, **policy):
    events = []
    t = [0.0]
    pr = publish.publish(
        repo=__import__("pathlib").Path("."), branch="rig/1", base="main", title="rig: t", body="body",
        review=review, can_merge=can_merge, why_not=why_not,
        policy=Publish(pr=True, approver="reviewer", **policy), on_event=events.append, run=gh,
        sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], cancelled=cancelled,
    )
    return pr, events


@pytest.mark.parametrize(
    "text, ok",
    [
        ("All good.\nLGTM", True),
        ("**LGTM**", True),
        ("- LGTM, ship it", True),
        ("Not LGTM yet: tests fail", False),
        ("It would be LGTM once the test passes", False),
        ("LGTM-ish", False),
        ("", False),
        (None, False),
    ],
)
def test_approved(text, ok):
    assert publish.approved(text, "LGTM") is ok


def test_opens_pr_and_posts_review_without_merging():
    gh = FakeGh()
    pr, events = go(gh)
    assert pr.url == URL and pr.number == 7 and not pr.merged
    assert "auto_merge is off" in pr.note
    assert gh.ran("git", "push", "-u", "origin", "rig/1")
    (create,) = gh.ran("gh", "pr", "create")
    assert create[0][create[0].index("--base") + 1] == "main" and create[1] == "body"
    (review,) = gh.ran("gh", "pr", "review")
    assert "--comment" in review[0] and review[1] == "looks good\nLGTM"
    assert not gh.ran("gh", "pr", "merge")
    assert f"  PR opened: {URL}" in events


def test_merges_when_ci_passes():
    gh = FakeGh(checks=[[{"name": "tests", "bucket": "pending"}], [{"name": "tests", "bucket": "pass"}, {"name": "lint", "bucket": "skipping"}]])
    pr, events = go(gh, auto_merge=True, merge_method="merge")
    assert pr.merged
    (merge,) = gh.ran("gh", "pr", "merge")
    assert merge[0] == ["gh", "pr", "merge", URL, "--merge", "--delete-branch"]


@pytest.mark.parametrize(
    "kwargs, checks, note",
    [
        ({"can_merge": False, "why_not": "reviewer didn't approve"}, [], "not merged: reviewer didn't approve"),
        ({}, [[{"name": "tests", "bucket": "fail"}]], "CI failed: tests"),
        ({}, [None], "no CI checks ran"),
        ({"ci_timeout": 60}, [[{"name": "tests", "bucket": "pending"}]], "CI still running after 60s"),
        ({"cancelled": lambda: True}, [[{"name": "tests", "bucket": "pending"}]], "stopped by user"),
    ],
)
def test_does_not_merge(kwargs, checks, note):
    gh = FakeGh(checks=checks)
    pr, _ = go(gh, auto_merge=True, **kwargs)
    assert not pr.merged and note in pr.note and pr.url == URL
    assert not gh.ran("gh", "pr", "merge")


def test_no_checks_allowed_when_not_required():
    gh = FakeGh(checks=[None])
    pr, _ = go(gh, auto_merge=True, require_checks=False)
    assert pr.merged


@pytest.mark.parametrize("step, note", [("git push -u", "push failed"), ("gh pr create", "couldn't open a PR")])
def test_failures_stop_early(step, note):
    gh = FakeGh(fail={step})
    pr, events = go(gh, auto_merge=True)
    assert note in pr.note and not pr.merged and any(e.startswith("  ✗") for e in events)


@pytest.mark.parametrize(
    "data, message",
    [
        ({"publish": {"auto_merge": True, "approver": "b"}}, "needs publish.pr"),
        ({"publish": {"pr": True, "auto_merge": True}}, "needs publish.approver"),
        ({"publish": {"pr": True, "approver": "ghost"}}, "isn't one of the hands"),
    ],
)
def test_spec_errors(data, message):
    with pytest.raises(ValidationError, match=message):
        Rig.model_validate({"name": "t", "hands": {"a": {"role": "A"}, "b": {"role": "B"}}, **data})


# --- end to end: a real repo and a real (local, bare) remote; gh is faked ------------------------

@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["add", "-A"], ["commit", "-q", "-m", "init"], ["remote", "add", "origin", str(remote)]):
        git(*args, cwd=repo)
    return repo, remote


class Crew:
    """The coder edits a.py; the reviewer replies with `verdict`."""

    def __init__(self, verdict):
        self.verdict = verdict

    async def run(self, hand, prompt, toolbox, check=None):
        if hand.name == "coder":
            await toolbox.call("write_file", {"path": "a.py", "content": "x = 2\n"})
            return HandResult(hand.name, "changed x", "end_turn", 1)
        return HandResult(hand.name, self.verdict, "end_turn", 1)


def shift_with(repo, verdict, monkeypatch, tmp_path):
    gh = FakeGh(checks=[[{"name": "tests", "bucket": "pass"}]])

    def run(args, cwd, stdin=None):
        if args[0] == "git":  # the push really happens, to the bare remote
            proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
            gh.calls.append((args, stdin))
            return proc.returncode, proc.stdout + proc.stderr
        return gh(args, cwd, stdin)

    monkeypatch.setattr(publish, "run_cmd", run)
    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo),
        "hands": {"coder": {"role": "C", "tools": ["write_file"]}, "reviewer": {"role": "R"}},
        "lines": ["coder -> reviewer"],
        "publish": {"pr": True, "approver": "reviewer", "auto_merge": True, "ci_poll": 1},
    })
    events = []
    # publish.pr turns on worktree mode by itself.
    shift = asyncio.run(run_shift(rig, "bump x\ndetails", Crew(verdict), root=tmp_path / "rigroot", on_event=events.append))
    return shift, gh, events


def test_end_to_end_merge(repo, monkeypatch, tmp_path):
    repo, remote = repo
    shift, gh, events = shift_with(repo, "Checked a.py.\nLGTM", monkeypatch, tmp_path)
    branch = f"rig/{shift.id}"
    assert git("show", f"{branch}:a.py", cwd=remote) == "x = 2\n"  # pushed
    (create,) = gh.ran("gh", "pr", "create")
    assert create[0][create[0].index("--title") + 1] == "rig: bump x"
    assert create[0][create[0].index("--base") + 1] == "main"
    assert gh.ran("gh", "pr", "merge")
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["prs"] == [{"repo": str(repo.resolve()), "branch": branch, "url": URL, "number": 7, "merged": True, "note": ""}]


def test_end_to_end_reviewer_did_not_approve(repo, monkeypatch, tmp_path):
    repo, _ = repo
    shift, gh, events = shift_with(repo, "Not LGTM: missing test", monkeypatch, tmp_path)
    (pr,) = shift.prs
    assert pr.url == URL and not pr.merged and "reviewer didn't approve" in pr.note
    assert not gh.ran("gh", "pr", "merge")
