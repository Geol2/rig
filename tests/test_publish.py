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

    def __init__(self, checks=None, fail=None, merges=None, mergeable=None, state="OPEN", head=None, out=None):
        self.calls = []
        self.checks = list(checks or [])
        self.fail = fail or set()
        self.out = out or {}  # what a call in `fail` prints (default "<call>: boom")
        self.merges = list(merges or [])  # successive `gh pr merge` exit codes (default 0)
        self.mergeable = list(mergeable or [])  # successive `gh pr view --json mergeable` answers
        self.state = state  # `gh pr view --json state` answer (None: the call fails)
        self.sha = "abc123"  # `git rev-parse` answer
        self.head = head  # `gh pr view --json headRefOid` answer (default: the pushed sha)

    def __call__(self, args, cwd, stdin=None):
        self.calls.append((args, stdin))
        key = " ".join(args[:3])
        if key in self.fail:
            return 1, self.out.get(key, f"{key}: boom")
        if args[:3] == ["gh", "pr", "create"]:
            return 0, f"Creating pull request\n{URL}"
        if args[:3] == ["gh", "pr", "merge"] and self.merges and self.merges.pop(0):
            return 1, "Pull request is not mergeable"
        if args[:2] == ["git", "rev-parse"]:
            return 0, self.sha + "\n"
        if args[:3] == ["gh", "pr", "view"] and "state" in args:
            return (1, "HTTP 502") if self.state is None else (0, self.state + "\n")
        if args[:3] == ["gh", "pr", "view"] and "headRefOid" in args:
            return 0, (self.sha if self.head is None else self.head) + "\n"
        if args[:3] == ["gh", "pr", "view"]:
            return 0, self.mergeable.pop(0) if self.mergeable else "UNKNOWN"
        if args[:3] == ["gh", "pr", "checks"]:
            answer = self.checks.pop(0) if len(self.checks) > 1 else (self.checks[0] if self.checks else None)
            return (0, json.dumps(answer)) if answer is not None else (1, "no checks reported on the 'x' branch")
        return 0, ""

    def ran(self, *prefix):
        return [c for c in self.calls if c[0][: len(prefix)] == list(prefix)]


def go(gh, *, can_merge=True, why_not="", review="looks good\nLGTM", cancelled=lambda: False, existing=None,
       where="", **policy):
    events = []
    t = [0.0]
    pr = publish.publish(
        repo=__import__("pathlib").Path("."), branch="rig/1", base="main", title="rig: t", body="body",
        review=review, can_merge=can_merge, why_not=why_not,
        policy=Publish(pr=True, approver="reviewer", **policy), on_event=events.append, run=gh,
        sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0], cancelled=cancelled, existing=existing,
        where=where,
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


PASS = [{"name": "tests", "bucket": "pass"}]


def test_base_moved_on_updates_branch_and_merges():
    gh = FakeGh(checks=[PASS], merges=[1, 0], mergeable=["MERGEABLE"])
    pr, events = go(gh, auto_merge=True)
    assert pr.merged and not pr.closed
    assert gh.ran("gh", "pr", "update-branch", URL)
    assert len(gh.ran("gh", "pr", "merge")) == 2
    assert any("updating the PR branch" in e for e in events)


def test_ci_failing_after_update_leaves_pr_open():
    gh = FakeGh(checks=[PASS, [{"name": "tests", "bucket": "fail"}]], merges=[1], mergeable=["MERGEABLE"])
    pr, _ = go(gh, auto_merge=True)
    assert not pr.merged and not pr.closed and "after updating the branch: CI failed: tests" in pr.note
    assert len(gh.ran("gh", "pr", "merge")) == 1


def test_conflict_closes_pr_with_comment():
    # GitHub may still be computing mergeability at first.
    gh = FakeGh(checks=[PASS], merges=[1], mergeable=["UNKNOWN", "CONFLICTING", "CONFLICTING"])
    pr, events = go(gh, auto_merge=True)
    assert pr.closed and not pr.merged and "conflicts with main" in pr.note
    (close,) = gh.ran("gh", "pr", "close", URL)
    assert "--comment" in close[0] and "rig/1" in close[0][-1]
    assert not gh.ran("gh", "pr", "update-branch")
    assert any(e.startswith("  ✗") and "PR closed" in e for e in events)
    assert pr.as_dict()["closed"] is True


def test_other_merge_failure_leaves_pr_open():
    gh = FakeGh(checks=[PASS], merges=[1], mergeable=[])  # mergeability stays UNKNOWN
    pr, _ = go(gh, auto_merge=True)
    assert not pr.merged and not pr.closed and pr.note.startswith("merge failed")
    assert not gh.ran("gh", "pr", "close") and not gh.ran("gh", "pr", "update-branch")


def test_no_checks_allowed_when_not_required():
    gh = FakeGh(checks=[None])
    pr, _ = go(gh, auto_merge=True, require_checks=False)
    assert pr.merged


@pytest.mark.parametrize("step, note", [("git push -u", "push failed"), ("gh pr create", "couldn't open a PR")])
def test_failures_stop_early(step, note):
    gh = FakeGh(fail={step})
    pr, events = go(gh, auto_merge=True)
    assert note in pr.note and not pr.merged and any(e.startswith("  ✗") for e in events)


OLD_URL = "https://github.com/o/r/pull/3"


def old_pr():
    return publish.PullRequest(repo=__import__("pathlib").Path("."), branch="rig/0", url=OLD_URL, number=3,
                               note="not merged: CI failed: tests")


def test_existing_pr_is_updated():
    gh = FakeGh(checks=[PASS])
    pr, events = go(gh, auto_merge=True, existing=old_pr())
    assert not gh.ran("gh", "pr", "create") and not gh.ran("git", "push", "-u")
    assert gh.ran("git", "push", "origin", "rig/1:refs/heads/rig/0")
    assert (pr.branch, pr.url, pr.number) == ("rig/0", OLD_URL, 3)
    assert f"  PR updated: {OLD_URL}" in events
    assert gh.ran("gh", "pr", "review", OLD_URL) and gh.ran("gh", "pr", "checks", OLD_URL)
    assert pr.merged and pr.note == ""
    assert not any("hasn't shown" in e or "couldn't read" in e for e in events)


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_existing_pr_no_longer_open_opens_new_one(state):
    gh = FakeGh(checks=[PASS], state=state)
    pr, events = go(gh, existing=old_pr(), where=" in r")
    assert f"  ! PR #3 is {state.lower()}; opening a new PR instead" in events
    assert "  publishing rig/1 → main in r" in events
    assert gh.ran("git", "push", "-u", "origin", "rig/1") and gh.ran("gh", "pr", "create")
    assert (pr.branch, pr.url, pr.number) == ("rig/1", URL, 7)


@pytest.mark.parametrize("state", [None, "", "DRAFT"])
def test_existing_pr_with_unreadable_state_is_updated_anyway(state):
    gh = FakeGh(checks=[PASS], state=state)
    pr, events = go(gh, auto_merge=True, existing=old_pr())
    assert "  ! couldn't read the state of PR #3; updating it anyway" in events
    assert not gh.ran("gh", "pr", "create") and not gh.ran("git", "push", "-u")
    assert gh.ran("git", "push", "origin", "rig/1:refs/heads/rig/0")
    assert (pr.branch, pr.url, pr.number) == ("rig/0", OLD_URL, 3) and pr.merged


REJECTED = "push to rig/0 rejected (branch changed on GitHub since the last shift); PR #3 not updated"


@pytest.mark.parametrize("out, note", [
    (" ! [rejected]        rig/1 -> rig/0 (fetch first)\nerror: failed to push some refs", REJECTED),
    ("hint: Updates were refused because of a Non-Fast-Forward", REJECTED),
    ("fatal: Authentication failed for 'https://github.com/o/r.git/'",
     "push to rig/0 failed: fatal: Authentication failed for 'https://github.com/o/r.git/'; PR #3 not updated"),
    (" ! [remote rejected] rig/1 -> rig/0 (protected branch hook declined)\nerror: failed to push some refs",
     "push to rig/0 failed:  ! [remote rejected] rig/1 -> rig/0 (protected branch hook declined)\n"
     "error: failed to push some refs; PR #3 not updated"),
])
def test_existing_pr_push_fails(out, note):
    gh = FakeGh(checks=[PASS], fail={"git push origin"}, out={"git push origin": out})
    pr, events = go(gh, auto_merge=True, existing=old_pr())
    assert pr.note == note
    assert f"  ✗ {note}" in events
    assert not gh.ran("gh", "pr", "create") and not gh.ran("gh", "pr", "merge") and not pr.merged


def test_existing_pr_head_not_shown_yet_checks_anyway():
    gh = FakeGh(checks=[PASS], head="old")
    pr, events = go(gh, auto_merge=True, existing=old_pr())
    assert len(gh.ran("gh", "pr", "view", OLD_URL, "--json", "headRefOid")) == 5
    assert "  ! GitHub hasn't shown the new commit on the PR yet; checking CI anyway" in events
    assert pr.merged


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

    def __init__(self, verdict, content="x = 2\n"):
        self.verdict = verdict
        self.content = content

    async def run(self, hand, prompt, toolbox, check=None):
        if hand.name == "coder":
            await toolbox.call("write_file", {"path": "a.py", "content": self.content})
            return HandResult(hand.name, "changed x", "end_turn", 1)
        return HandResult(hand.name, self.verdict, "end_turn", 1)


def shift_with(repo, verdict, monkeypatch, tmp_path, hands=None, lines=("coder -> reviewer",), checks=None,
               gh=None, crew=None, resume=None):
    gh = gh or FakeGh()
    gh.checks = list(checks or [[{"name": "tests", "bucket": "pass"}]])

    def run(args, cwd, stdin=None):
        if args[0] == "git":  # the push really happens, to the bare remote
            proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
            gh.calls.append((args, stdin))
            if args[:2] == ["git", "rev-parse"]:
                gh.sha = proc.stdout.strip()  # what the fake PR's head shows
            return proc.returncode, proc.stdout + proc.stderr
        return gh(args, cwd, stdin)

    monkeypatch.setattr(publish, "run_cmd", run)
    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo),
        "hands": {"coder": {"role": "C", "tools": ["write_file"]}, "reviewer": {"role": "R"}, **(hands or {})},
        "lines": list(lines),
        "publish": {"pr": True, "approver": "reviewer", "auto_merge": True, "ci_poll": 1},
    })
    events = []
    # publish.pr turns on worktree mode by itself.
    shift = asyncio.run(run_shift(rig, "bump x\ndetails", crew or Crew(verdict), root=tmp_path / "rigroot",
                                  on_event=events.append, resume=resume))
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
    assert summary["prs"] == [{"repo": str(repo.resolve()), "branch": branch, "url": URL, "number": 7, "merged": True, "closed": False, "note": ""}]


def test_pr_body_has_every_final_hand(repo, monkeypatch, tmp_path):
    repo, _ = repo
    shift, gh, events = shift_with(repo, "LGTM", monkeypatch, tmp_path, hands={"tester": {"role": "T"}},
                                   lines=["coder -> reviewer", "coder -> tester"])
    (create,) = gh.ran("gh", "pr", "create")
    assert create[1].startswith("## reviewer\n\nLGTM\n\n## tester\n\nLGTM\n\n---\nShift ")


def test_end_to_end_reviewer_did_not_approve(repo, monkeypatch, tmp_path):
    repo, _ = repo
    shift, gh, events = shift_with(repo, "Not LGTM: missing test", monkeypatch, tmp_path)
    (pr,) = shift.prs
    assert pr.url == URL and not pr.merged and "reviewer didn't approve" in pr.note
    assert not gh.ran("gh", "pr", "merge")


def test_resume_fixes_ci_on_the_same_pr(repo, monkeypatch, tmp_path):
    from rig.runner import load_resume

    repo, remote = repo
    gh = FakeGh()
    first, _, _ = shift_with(repo, "LGTM", monkeypatch, tmp_path, checks=[[{"name": "tests", "bucket": "fail"}]], gh=gh)
    old_branch = f"rig/{first.id}"
    summary = json.loads((first.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["ok"] and summary["prs"][0]["note"] == "not merged: CI failed: tests"

    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo),
        "hands": {"coder": {"role": "C", "tools": ["write_file"]}, "reviewer": {"role": "R"}},
        "lines": ["coder -> reviewer"], "publish": {"pr": True, "approver": "reviewer", "auto_merge": True},
    })
    resume = load_resume(tmp_path / "rigroot", rig, "last")
    assert resume.shift_id == first.id and resume.why == "CI failed on PR #7: tests"
    second, _, events = shift_with(repo, "LGTM", monkeypatch, tmp_path, gh=gh, crew=Crew("LGTM", "x = 3\n"),
                                   resume=resume)

    assert git("show", f"{old_branch}:a.py", cwd=remote) == "x = 3\n"  # the fix went onto the old branch
    assert len(gh.ran("gh", "pr", "create")) == 1  # over both shifts
    assert gh.ran("git", "push", "origin", f"rig/{second.id}:refs/heads/{old_branch}")
    assert f"  publishing rig/{second.id} → {old_branch} (PR #7)" in events
    summary = json.loads((second.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["resumed_from"] == first.id
    assert summary["prs"][0]["branch"] == old_branch and summary["prs"][0]["merged"]


def test_crashed_shift_is_not_published(repo, monkeypatch, tmp_path):
    repo, remote = repo
    real = __import__("rig.runner", fromlist=["build_prompt"]).build_prompt

    def broken(task, handoffs, *args, **kwargs):
        if handoffs:  # the reviewer's prompt: crash after the coder already changed a.py
            raise RuntimeError("bad prompt")
        return real(task, handoffs, *args, **kwargs)

    monkeypatch.setattr("rig.runner.build_prompt", broken)
    shift, gh, events = shift_with(repo, "LGTM", monkeypatch, tmp_path)
    assert shift.error == "RuntimeError: bad prompt"
    assert shift.committed()  # the change is kept on the local branch for a look...
    assert not gh.calls and not shift.prs  # ...but nothing is pushed or opened
    assert git("branch", "--list", f"rig/{shift.id}", cwd=remote) == ""
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["error"] == "RuntimeError: bad prompt" and summary["prs"] == []


# --- starting from the newest of local and remote (publish.sync) -------------------------------

@pytest.fixture
def synced(repo, tmp_path):
    """The repo with main pushed, plus a second clone standing in for "GitHub moved on"."""
    repo, remote = repo
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    other = tmp_path / "other"
    git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    git("config", "user.email", "o@example.com", cwd=other)
    git("config", "user.name", "o", cwd=other)
    return repo, remote, other


def commit(path, name, text):
    (path / name).write_text(text, encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", f"add {name}", cwd=path)


class Peek:
    """Records which files the hand sees, changes nothing."""

    def __init__(self):
        self.seen = None

    async def run(self, hand, prompt, toolbox, check=None):
        self.seen = toolbox.run("list_dir", {"path": "."}).split()
        return HandResult(hand.name, "nothing to do", "end_turn", 1)


def run_peek(repo, tmp_path, **publish_opts):
    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo), "hands": {"a": {"role": "A", "tools": ["list_dir"]}},
        "publish": {"pr": True, **publish_opts},
    })
    events, worker = [], Peek()
    asyncio.run(run_shift(rig, "x", worker, root=tmp_path / "rigroot", on_event=events.append))
    return worker.seen, events


def test_starts_from_remote_when_local_is_behind(synced, tmp_path):
    repo, _, other = synced
    commit(other, "merged.py", "from a merged PR\n")
    git("push", "-q", "origin", "main", cwd=other)
    seen, events = run_peek(repo, tmp_path)
    assert "merged.py" in seen  # no `git pull` needed
    assert any("local main is behind origin; starting from origin/main" in e for e in events)
    assert not (repo / "merged.py").exists()  # the user's checkout is left alone


def test_starts_from_local_when_it_has_unpushed_commits(synced, tmp_path):
    repo, _, _ = synced
    commit(repo, "todo_item.md", "- [ ] new item\n")  # committed, not pushed
    seen, events = run_peek(repo, tmp_path)
    assert "todo_item.md" in seen
    assert not any("behind" in e or "diverged" in e for e in events)


def test_diverged_starts_from_local_with_a_warning(synced, tmp_path):
    repo, _, other = synced
    commit(other, "theirs.py", "x\n")
    git("push", "-q", "origin", "main", cwd=other)
    commit(repo, "mine.py", "y\n")
    seen, events = run_peek(repo, tmp_path)
    assert "mine.py" in seen and "theirs.py" not in seen
    assert any(e.startswith("  ! local main and origin/main have diverged") for e in events)


def test_fetch_failure_starts_from_local(synced, tmp_path):
    repo, _, other = synced
    commit(other, "merged.py", "x\n")
    git("push", "-q", "origin", "main", cwd=other)
    git("remote", "set-url", "origin", str(tmp_path / "nowhere.git"), cwd=repo)
    seen, events = run_peek(repo, tmp_path)
    assert "merged.py" not in seen
    assert any(e.startswith("  ! couldn't fetch origin/main, starting from local main") for e in events)


def test_sync_off_starts_from_local(synced, tmp_path):
    repo, _, other = synced
    commit(other, "merged.py", "x\n")
    git("push", "-q", "origin", "main", cwd=other)
    seen, _ = run_peek(repo, tmp_path, sync=False)
    assert "merged.py" not in seen
