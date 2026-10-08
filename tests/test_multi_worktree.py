"""--worktree with several `workspaces`: a worktree per git repo, one branch name in each."""

import asyncio
import json

import pytest

from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import Rig
from rig.worktree import GitError, git


def make_repo(path):
    path.mkdir(parents=True)
    (path / "README.md").write_text("v1\n", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    git("config", "user.name", "test", cwd=path)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    return path


def branches(repo):
    return git("branch", "--format=%(refname:short)", cwd=repo).split()


class Writer:
    """Writes `files` (project-prefixed paths) through the toolbox; records where each project was mapped."""

    def __init__(self, files):
        self.files = files
        self.roots = None

    async def run(self, hand, prompt, toolbox, check=None):
        self.roots = dict(toolbox.roots)
        for path, content in self.files.items():
            await toolbox.call("write_file", {"path": path, "content": content})
        return HandResult(name=hand.name, output="done", stop_reason="end_turn", turns=1)


def run(workspaces, files, root):
    rig = Rig.model_validate({
        "name": "t", "workspaces": {n: str(p) for n, p in workspaces.items()},
        "hands": {"coder": {"role": "Code.", "tools": ["write_file"]}},
    })
    worker = Writer(files)
    events = []
    shift = asyncio.run(run_shift(rig, "change both", worker, root=root, on_event=events.append, use_worktree=True))
    return shift, worker, events


def test_each_repo_gets_the_branch(tmp_path):
    api, web, other = make_repo(tmp_path / "api"), make_repo(tmp_path / "web"), make_repo(tmp_path / "other")
    shift, worker, events = run(
        {"backend": api, "frontend": web, "docs": other},
        {"backend/src/a.py": "a\n", "frontend/src/b.js": "b\n"},  # docs unchanged
        tmp_path / "rigroot",
    )
    branch = f"rig/{shift.id}"
    assert shift.ok and shift.branch == branch
    # Hands worked inside the worktrees, never in the projects themselves.
    assert all(".rig" in str(p) for p in worker.roots.values())
    assert not (api / "src").exists() and not (web / "src").exists()
    # Changed repos have the branch with the change; the unchanged one doesn't keep it.
    assert git("show", f"{branch}:src/a.py", cwd=api) == "a\n"
    assert git("show", f"{branch}:src/b.js", cwd=web) == "b\n"
    assert branch not in branches(other)
    assert [wt.repo.name for wt, _ in shift.committed()] == ["api", "web"]
    assert not any(wt.path.exists() for wt in shift.worktrees)
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["branch"] == branch
    assert [(w["changed"], w["commit"] is not None) for w in summary["worktrees"]] == [(True, True), (True, True), (False, False)]
    assert any(e.startswith("  worktree api: committed") for e in events)
    assert any(e.startswith("  worktree other: no changes") for e in events)


def test_projects_in_one_repo_share_a_worktree(tmp_path):
    mono = make_repo(tmp_path / "mono")
    (mono / "server").mkdir()
    (mono / "client").mkdir()
    shift, worker, _ = run({"backend": mono / "server", "frontend": mono / "client"},
                           {"backend/x.py": "x\n", "frontend/y.js": "y\n"}, tmp_path / "rigroot")
    assert len(shift.worktrees) == 1
    assert worker.roots["backend"].name == "server" and worker.roots["frontend"].name == "client"
    assert git("show", f"rig/{shift.id}:server/x.py", cwd=mono) == "x\n"
    assert git("show", f"rig/{shift.id}:client/y.js", cwd=mono) == "y\n"


def test_a_non_repo_project_creates_nothing(tmp_path):
    api = make_repo(tmp_path / "api")
    (tmp_path / "plain").mkdir()
    with pytest.raises(GitError, match="plain"):
        run({"backend": api, "frontend": tmp_path / "plain"}, {}, tmp_path / "rigroot")
    assert branches(api) == ["main"]
    assert not list((tmp_path / "rigroot" / ".rig" / "worktrees").glob("*/*"))
