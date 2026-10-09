import asyncio
import json

import pytest

from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import Rig
from rig.worktree import GitError, git


class WritingWorker:
    """Writes `files` through the hand's toolbox, then finishes; records the workspace it saw."""

    def __init__(self, files=None, fail=False):
        self.files = files or {}
        self.fail = fail
        self.seen_workspace = None

    async def run(self, hand, prompt, toolbox, check=None):
        self.seen_workspace = toolbox.workspace
        for path, content in self.files.items():
            await toolbox.call("write_file", {"path": path, "content": content})
        if self.fail:
            raise RuntimeError("worker crashed")
        return HandResult(name=hand.name, output="done", stop_reason="end_turn", turns=1)


def rig_for(workspace):
    return Rig.model_validate({
        "name": "t", "workspace": str(workspace),
        "hands": {"coder": {"role": "Code.", "tools": ["write_file"]}},
    })


def shift_in(repo, worker, root, workspace=None):
    rig = rig_for(workspace or repo)
    return asyncio.run(run_shift(rig, "update main\nmore detail", worker, root=root, on_event=lambda _: None, use_worktree=True))


def branches(repo):
    return git("branch", "--format=%(refname:short)", cwd=repo).split()


def test_changes_land_on_branch_not_main(repo, tmp_path):
    worker = WritingWorker({"app/main.py": "print('v2')\n", "app/new.py": "x = 1\n"})
    shift = shift_in(repo, worker, tmp_path / "rigroot")

    assert worker.seen_workspace != repo.resolve()
    assert shift.ok and shift.outcome.changed and shift.outcome.commit
    branch = f"rig/{shift.id}"
    assert branch in branches(repo)
    # Main working tree untouched; the branch has the change; the worktree is gone.
    assert (repo / "app" / "main.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert git("show", f"{branch}:app/main.py", cwd=repo) == "print('v2')\n"
    assert "app/new.py" in shift.outcome.stat
    assert not shift.worktree.path.exists()
    assert git("log", "-1", "--format=%s", branch, cwd=repo).strip() == "rig: update main"
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["branch"] == branch


def test_workspace_subdir_maps_into_worktree(repo, tmp_path):
    worker = WritingWorker({"main.py": "print('sub')\n"})
    shift = shift_in(repo, worker, tmp_path / "rigroot", workspace=repo / "app")
    assert worker.seen_workspace == (shift.worktree.path / "app").resolve()
    assert git("show", f"rig/{shift.id}:app/main.py", cwd=repo) == "print('sub')\n"


def test_no_changes_removes_branch(repo, tmp_path):
    shift = shift_in(repo, WritingWorker(), tmp_path / "rigroot")
    assert not shift.outcome.changed
    assert branches(repo) == ["main"]
    assert not shift.worktree.path.exists()


def test_crash_still_commits_what_was_written(repo, tmp_path):
    worker = WritingWorker({"app/partial.py": "y = 2\n"}, fail=True)
    shift = shift_in(repo, worker, tmp_path / "rigroot")
    assert not shift.ok and shift.results["coder"].stop_reason == "error"
    (branch,) = [b for b in branches(repo) if b.startswith("rig/")]
    assert git("show", f"{branch}:app/partial.py", cwd=repo) == "y = 2\n"
    assert git("log", "-1", "--format=%b", branch, cwd=repo).strip().endswith("(incomplete).")


def test_dirty_repo_is_flagged(repo, tmp_path):
    (repo / "app" / "main.py").write_text("print('local edit')\n", encoding="utf-8")
    events = []
    rig = rig_for(repo)
    asyncio.run(run_shift(rig, "t", WritingWorker(), root=tmp_path / "r", on_event=events.append, use_worktree=True))
    assert any("uncommitted changes" in e for e in events)


def test_not_a_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError, match="git repository"):
        shift_in(plain, WritingWorker(), tmp_path / "rigroot")


def test_roots_sharing_a_repo_get_distinct_branches(repo, tmp_path, monkeypatch):
    from datetime import datetime

    import rig.runner

    class FrozenClock:
        @staticmethod
        def now():
            return datetime(2025, 1, 2, 3, 4, 5)

    monkeypatch.setattr(rig.runner, "datetime", FrozenClock)
    shifts = [shift_in(repo, WritingWorker({"app/main.py": f"print('{name}')\n"}), tmp_path / f"root{name}")
              for name in "ABC"]

    sid = shifts[0].id
    assert sid == "20250102-030405" and all(s.id == sid for s in shifts)
    expected = [f"rig/{sid}", f"rig/{sid}-2", f"rig/{sid}-3"]
    assert [s.branch for s in shifts] == expected
    for shift, branch, name in zip(shifts, expected, "ABC"):
        assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["branch"] == branch
        assert git("show", f"{branch}:app/main.py", cwd=repo) == f"print('{name}')\n"


def test_branch_is_first_changed_repos_branch(tmp_path, monkeypatch):
    from datetime import datetime

    import rig.runner

    class FrozenClock:
        @staticmethod
        def now():
            return datetime(2025, 1, 2, 3, 4, 5)

    monkeypatch.setattr(rig.runner, "datetime", FrozenClock)
    repos = {}
    for name in ("api", "web"):
        path = tmp_path / name
        path.mkdir()
        (path / "README.md").write_text("v1\n", encoding="utf-8")
        git("init", "-q", "-b", "main", cwd=path)
        git("config", "user.email", "test@example.com", cwd=path)
        git("config", "user.name", "test", cwd=path)
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", "init", cwd=path)
        repos[name] = path
    sid = "20250102-030405"
    git("branch", f"rig/{sid}", cwd=repos["web"])

    rig = Rig.model_validate({
        "name": "t", "workspaces": {"backend": str(repos["api"]), "frontend": str(repos["web"])},
        "hands": {"coder": {"role": "Code.", "tools": ["write_file"]}},
    })
    worker = WritingWorker({"frontend/b.js": "b\n"})  # only the second repo changes
    shift = asyncio.run(run_shift(rig, "t", worker, root=tmp_path / "rigroot", on_event=lambda _: None, use_worktree=True))

    assert shift.id == sid
    assert [wt.branch for wt in shift.worktrees] == [f"rig/{sid}", f"rig/{sid}-2"]
    assert shift.branch == f"rig/{sid}-2"
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["branch"] == f"rig/{sid}-2"
    assert git("show", f"rig/{sid}-2:b.js", cwd=repos["web"]) == "b\n"


def test_create_skips_existing_branch(repo, tmp_path):
    from rig import worktree

    git("branch", "rig/x", cwd=repo)
    wt = worktree.create(repo, tmp_path / "wt", "rig/x")
    try:
        assert wt.branch == "rig/x-2"
    finally:
        worktree.discard(wt)


def test_create_moves_on_when_branch_taken_after_check(repo, tmp_path, monkeypatch):
    from rig import worktree

    real = worktree._branch_exists
    raced = []

    def racing_exists(repo_path, name):
        # The first check sees rig/x free, then another root creates it before `git branch`.
        if name == "rig/x" and not raced:
            raced.append(name)
            git("branch", "rig/x", cwd=repo_path)
            return False
        return real(repo_path, name)

    monkeypatch.setattr(worktree, "_branch_exists", racing_exists)
    wt = worktree.create(repo, tmp_path / "wt", "rig/x")
    try:
        assert raced and wt.branch == "rig/x-2"
    finally:
        worktree.discard(wt)


def test_create_reraises_unrelated_failure(repo, tmp_path):
    from rig import worktree

    # The destination is an existing non-empty directory: not a branch clash, so no retrying.
    dest = tmp_path / "taken"
    dest.mkdir()
    (dest / "f").write_text("x", encoding="utf-8")
    with pytest.raises(GitError, match="worktree add"):
        worktree.create(repo, dest, "rig/x")
    assert branches(repo) == ["main"]


def test_trusting_env_appends_to_existing_git_config():
    from pathlib import Path

    from rig.worktree import trusting

    env = trusting(Path("D:/x/wt"), base={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "me"})
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("safe.directory", "D:/x/wt")
    assert (env["GIT_CONFIG_KEY_2"], env["GIT_CONFIG_VALUE_2"]) == ("core.longpaths", "true")


def test_commit_failure_keeps_worktree(repo, tmp_path, monkeypatch):
    import rig.worktree as wtmod

    real = wtmod.git

    def failing_commit(*args, **kw):
        if args[:1] == ("commit",):
            raise wtmod.GitError("git commit failed: boom")
        return real(*args, **kw)

    monkeypatch.setattr(wtmod, "git", failing_commit)
    shift = shift_in(repo, WritingWorker({"app/x.py": "1\n"}), tmp_path / "rigroot")
    assert shift.outcome.kept_at == shift.worktree.path and shift.worktree.path.exists()
    assert "boom" in shift.outcome.error
    # shift.json is still written.
    assert json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))["branch"] == f"rig/{shift.id}"


def test_finish_crash_keeps_worktree_and_writes_summary(repo, tmp_path, monkeypatch):
    import rig.worktree as wtmod

    real_finish = wtmod.finish

    def crashing_finish(wt, message):
        raise RuntimeError("diff exploded")

    monkeypatch.setattr(wtmod, "finish", crashing_finish)
    events = []
    rig = rig_for(repo)
    shift = asyncio.run(run_shift(rig, "t", WritingWorker({"app/x.py": "1\n"}), root=tmp_path / "rigroot",
                                  on_event=events.append, use_worktree=True))
    try:
        assert shift.outcome.kept_at == shift.worktree.path and shift.worktree.path.exists()
        assert shift.outcome.error == "RuntimeError: diff exploded"
        assert any("worktree kept at" in e for e in events)
        summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
        assert summary["worktrees"][0]["kept_at"] == str(shift.worktree.path)
    finally:
        real_finish(shift.worktree, "cleanup")
