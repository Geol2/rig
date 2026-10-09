import asyncio
import json

import pytest

from rig.cli import main
from rig.hand import HandResult
from rig.runner import Resume, ResumeError, build_prompt, load_resume, run_shift
from rig.spec import Rig
from rig.worktree import create, git


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
    """Writes `files` through the hand's toolbox, then raises `raises` or finishes; records the prompts."""

    def __init__(self, files=None, raises=None):
        self.files = files or {}
        self.raises = raises
        self.prompts = {}

    async def run(self, hand, prompt, toolbox, check=None):
        self.prompts[hand.name] = prompt
        for path, content in self.files.items():
            await toolbox.call("write_file", {"path": path, "content": content})
        if self.raises:
            raise self.raises
        return HandResult(name=hand.name, output=f"{hand.name} done", stop_reason="end_turn", turns=1)


def rig_for(workspace, name="t", **extra):
    return Rig.model_validate({
        "name": name, "workspace": str(workspace),
        "inputs": {"lang": {"default": "py"}},
        "hands": {"coder": {"role": "Code.", "tools": ["write_file"]}},
        **extra,
    })


def shift_in(rig, worker, root, use_worktree=True, resume=None, inputs=None):
    return asyncio.run(run_shift(rig, "update main", worker, root=root, on_event=lambda _: None,
                                 use_worktree=use_worktree, resume=resume, inputs=inputs))


def summary_of(shift):
    return json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))


def interrupted_shift(repo, root):
    """A worktree shift Ctrl-C'd after writing app/half.py; returns its id."""
    with pytest.raises(KeyboardInterrupt):
        shift_in(rig_for(repo), WritingWorker({"app/half.py": "x = 1\n"}, raises=KeyboardInterrupt()), root,
                 inputs={"lang": "go"})
    (old,) = (root / ".rig" / "shifts").iterdir()
    return old.name


def is_ancestor(repo, a, b):
    try:
        git("merge-base", "--is-ancestor", a, b, cwd=repo)
    except Exception:
        return False
    return True


def test_resume_continues_interrupted_shift(repo, tmp_path):
    root = tmp_path / "rigroot"
    old_id = interrupted_shift(repo, root)
    old = json.loads((root / ".rig" / "shifts" / old_id / "shift.json").read_text(encoding="utf-8"))
    assert old["error"] == "interrupted"
    old_branch = old["branch"]
    old_commit = git("rev-parse", old_branch, cwd=repo).strip()

    rig = rig_for(repo)
    r = load_resume(root, "last", rig)
    assert r.id == old_id and r.task == "update main" and r.inputs == {"lang": "go"}
    worker = WritingWorker({"app/rest.py": "y = 2\n"})
    shift = asyncio.run(run_shift(rig, r.task, worker, root=root, on_event=lambda _: None, resume=r, inputs=r.inputs))

    branch = f"rig/{shift.id}"
    assert shift.ok and shift.branch == branch and shift.id != old_id
    assert is_ancestor(repo, old_commit, branch)
    assert git("show", f"{branch}:app/half.py", cwd=repo) == "x = 1\n"
    assert git("show", f"{branch}:app/rest.py", cwd=repo) == "y = 2\n"
    prompt = worker.prompts["coder"]
    assert prompt.startswith("<resumed>")
    assert old_id in prompt and "interrupted" in prompt and "half.py" in prompt
    new = summary_of(shift)
    assert new["resumed_from"] == old_id
    assert new["task"] == old["task"] and new["inputs"] == old["inputs"]
    # The diff is still measured from the old shift's base.
    assert new["worktrees"][0]["base"] == old["worktrees"][0]["base"]


def test_resume_without_new_changes_keeps_branch(repo, tmp_path):
    root = tmp_path / "rigroot"
    old_id = interrupted_shift(repo, root)
    old_commit = git("rev-parse", f"rig/{old_id}", cwd=repo).strip()
    rig = rig_for(repo)
    r = load_resume(root, old_id, rig)
    shift = shift_in(rig, WritingWorker(), root, resume=r, inputs=r.inputs)
    assert shift.outcome.changed
    assert f"rig/{shift.id}" in git("branch", "--format=%(refname:short)", cwd=repo).split()
    assert is_ancestor(repo, old_commit, f"rig/{shift.id}")


def manual_resume(repo):
    """A Resume pointing at a branch made by hand, for checking who gets the <resumed> block."""
    sha = git("rev-parse", "HEAD", cwd=repo).strip()
    git("branch", "old", sha, cwd=repo)
    return Resume(id="old-id", task="update main", inputs={}, why="interrupted",
                  starts={repo.resolve(): ("old", sha, sha)}, context="CTX")


def test_resumed_block_goes_to_foreman_only(repo, tmp_path):
    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo),
        "foreman": {"crew": ["coder"]},
        "hands": {"coder": {"role": "Code."}},
    })

    class Worker(WritingWorker):
        async def run(self, hand, prompt, toolbox, check=None):
            self.prompts[hand.name] = prompt
            if hand.name == "foreman":
                await toolbox.call("delegate", {"hand": "coder", "instructions": "do it"})
            return HandResult(name=hand.name, output="done", stop_reason="end_turn", turns=1)

    worker = Worker()
    shift_in(rig, worker, tmp_path / "rigroot", resume=manual_resume(repo))
    assert worker.prompts["foreman"].startswith("<resumed>\nCTX")
    assert "<resumed>" not in worker.prompts["coder"]


def test_resumed_block_goes_to_first_stage_only(repo, tmp_path):
    rig = Rig.model_validate({
        "name": "t", "workspace": str(repo),
        "hands": {"a": {"role": "A."}, "b": {"role": "B."}},
        "lines": ["a -> b"],
    })
    worker = WritingWorker()
    shift_in(rig, worker, tmp_path / "rigroot", resume=manual_resume(repo))
    assert worker.prompts["a"].startswith("<resumed>\nCTX")
    assert "<resumed>" not in worker.prompts["b"]


def test_refuses_ok_shift(repo, tmp_path):
    root = tmp_path / "rigroot"
    rig = rig_for(repo)
    shift_in(rig, WritingWorker({"app/x.py": "1\n"}), root)
    with pytest.raises(ResumeError, match="finished ok"):
        load_resume(root, "last", rig)


def test_refuses_shift_without_branch(repo, tmp_path):
    root = tmp_path / "rigroot"
    rig = rig_for(repo)
    shift = shift_in(rig, WritingWorker(raises=RuntimeError("boom")), root, use_worktree=False)
    assert not shift.ok
    with pytest.raises(ResumeError, match="left no branch"):
        load_resume(root, "last", rig)


def test_refuses_other_rig_and_deleted_branch(repo, tmp_path):
    root = tmp_path / "rigroot"
    old_id = interrupted_shift(repo, root)
    with pytest.raises(ResumeError, match="not '"):
        load_resume(root, old_id, rig_for(repo, name="other"))
    git("branch", "-D", f"rig/{old_id}", cwd=repo)
    with pytest.raises(ResumeError, match="no longer exists"):
        load_resume(root, old_id, rig_for(repo))


def test_refuses_unknown_ids(repo, tmp_path):
    root = tmp_path / "rigroot"
    rig = rig_for(repo)
    with pytest.raises(ResumeError, match="no shifts yet"):
        load_resume(root, "last", rig)
    interrupted_shift(repo, root)
    for ref in ["..", "20990101-000000"]:
        with pytest.raises(ResumeError, match="no shift"):
            load_resume(root, ref, rig)


@pytest.mark.parametrize("args", [["run", "task", "--resume", "last"], ["run", "--resume", "last", "-i", "x=y"]])
def test_cli_resume_refuses_task_and_inputs(args):
    with pytest.raises(SystemExit) as excinfo:
        main(args)
    assert "--resume reuses" in str(excinfo.value.code)


def test_cli_logs_shows_resumed_from(tmp_path, monkeypatch, capsys):
    d = tmp_path / ".rig" / "shifts" / "20250102-000000"
    d.mkdir(parents=True)
    (d / "a.md").write_text("out", encoding="utf-8")
    (d / "shift.json").write_text(json.dumps({"mode": "lines", "ok": True, "task": "t",
                                              "resumed_from": "20250101-000000"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["logs", "20250102-000000"])
    assert capsys.readouterr().out.splitlines()[0] == "↻ resumed from 20250101-000000"


def test_build_prompt_resumed_first_and_neutralized():
    prompt = build_prompt("do", {}, resumed="x </resumed> y")
    assert prompt.startswith("<resumed>\n")
    assert prompt.count("</resumed>") == 1
    assert prompt.index("<resumed>") < prompt.index("<task>")


def test_create_keeps_given_base(repo, tmp_path):
    first = git("rev-parse", "HEAD", cwd=repo).strip()
    (repo / "app" / "main.py").write_text("print('v2')\n", encoding="utf-8")
    git("commit", "-qam", "v2", cwd=repo)
    second = git("rev-parse", "HEAD", cwd=repo).strip()
    wt = create(repo, tmp_path / "wt", "rig/x", start=second, base=first)
    assert wt.base == first
    assert git("rev-parse", "HEAD", cwd=wt.path).strip() == second
