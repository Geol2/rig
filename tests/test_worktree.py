import asyncio
import json

import pytest

from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import Rig
from rig.worktree import GitError, git


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("print('v1')\n", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


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
    with pytest.raises(RuntimeError):
        shift_in(repo, worker, tmp_path / "rigroot")
    (branch,) = [b for b in branches(repo) if b.startswith("rig/")]
    assert git("show", f"{branch}:app/partial.py", cwd=repo) == "y = 2\n"


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
