"""Several projects in one shift: `workspaces: {name: folder}`."""

import asyncio
import os

import pytest
import yaml
from pydantic import ValidationError

from rig.cli import main
from rig.hand import EchoWorker
from rig.runner import run_shift
from rig.serve import App
from rig.spec import RunPolicy, Rig
from rig.tools import Toolbox, ToolError
from rig.worktree import GitError

TOOLS = ["list_dir", "glob", "search", "read_file", "write_file", "edit_file"]


@pytest.fixture
def projects(tmp_path):
    files = {
        "api/src/order/OrderController.java": '@GetMapping("/api/orders")\n',
        "web/src/pages/Order.js": 'fetch("/api/orders")\n',
        "web/node_modules/lib/index.js": 'fetch("/api/orders")\n',
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return {"backend": tmp_path / "api", "frontend": tmp_path / "web"}


def test_top_level_lists_projects(projects):
    tb = Toolbox(projects, TOOLS)
    assert tb.run("list_dir", {"path": "."}) == "backend/\nfrontend/"
    assert tb.run("list_dir", {"path": "frontend/src"}) == "pages/"


def test_glob_and_search_span_projects(projects):
    tb = Toolbox(projects, TOOLS)
    assert tb.run("glob", {"pattern": "**/*.java"}) == "backend/src/order/OrderController.java"
    assert tb.run("glob", {"pattern": "frontend/**/*.js"}) == "frontend/src/pages/Order.js"
    assert tb.run("glob", {"pattern": "**/*.js", "path": "frontend/src"}) == "frontend/src/pages/Order.js"
    assert tb.run("search", {"pattern": "/api/orders"}).splitlines() == [
        'backend/src/order/OrderController.java:1: @GetMapping("/api/orders")',
        'frontend/src/pages/Order.js:1: fetch("/api/orders")',
    ]
    assert tb.run("search", {"pattern": "fetch", "glob": "frontend/**"}) == 'frontend/src/pages/Order.js:1: fetch("/api/orders")'


def test_read_write_edit_by_project_path(projects):
    tb = Toolbox(projects, TOOLS)
    tb.run("write_file", {"path": "frontend/src/new.js", "content": "a = 1\n"})
    assert (projects["frontend"] / "src/new.js").read_text() == "a = 1\n"
    tb.run("edit_file", {"path": "frontend/src/new.js", "old": "1", "new": "2"})
    assert tb.run("read_file", {"path": "frontend/src/new.js"}) == "1\ta = 2"


@pytest.mark.parametrize(
    "path, message",
    [
        ("src/pages/Order.js", "doesn't start with a project name"),
        ("database/x.sql", "doesn't start with a project name"),
        ("backend/../web/src/pages/Order.js", "escapes workspace"),
        ("/etc/passwd", "doesn't start with a project name"),
    ],
)
def test_paths_must_name_a_project(projects, path, message):
    with pytest.raises(ToolError, match=message):
        Toolbox(projects, TOOLS).run("read_file", {"path": path})


def test_descriptions_name_the_projects(projects):
    tb = Toolbox(projects, [*TOOLS, "run"], run_policy=RunPolicy(allow=["python -c"], workspace="frontend"))
    defs = {d["name"]: d["description"] for d in tb.definitions}
    assert all("(backend/, frontend/)" in defs[n] for n in TOOLS)
    assert defs["run"].endswith("Commands run in frontend/.")


def test_run_uses_its_project(projects):
    tb = Toolbox(projects, ["run"], run_policy=RunPolicy(allow=["python -c"], workspace="frontend"))
    out = tb.run("run", {"command": "python -c \"import os; print(os.getcwd())\""})
    assert os.path.realpath(out.splitlines()[1]) == os.path.realpath(projects["frontend"])


def test_single_workspace_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    tb = Toolbox({".": tmp_path}, TOOLS)
    assert tb.roots is None and tb.run("list_dir", {"path": "."}) == "a.txt"


@pytest.mark.parametrize(
    "data, message",
    [
        ({"workspace": "x", "workspaces": {"a": "a"}}, "either `workspace` or `workspaces`"),
        ({"workspaces": {"my app": "a"}}, "plain folder-like names"),
        ({"workspaces": {"a": "a", "b": "b"}, "run": {"allow": ["make"]},
          "hands": {"t": {"role": "T", "tools": ["run"]}}}, "set run.workspace"),
        ({"workspaces": {"a": "a"}, "run": {"allow": ["make"], "workspace": "zzz"}}, "isn't one of `workspaces`"),
    ],
)
def test_spec_errors(data, message):
    with pytest.raises(ValidationError, match=message):
        Rig.model_validate({"name": "t", "hands": {"h": {"role": "H"}}, **data})


def make_rig(projects, **extra):
    return Rig.model_validate({
        "name": "t", "workspaces": {n: str(p) for n, p in projects.items()},
        "hands": {"a": {"role": "A", "tools": ["list_dir"]}}, **extra,
    })


def test_shift_runs_across_projects(projects, tmp_path):
    shift = asyncio.run(run_shift(make_rig(projects), "look", EchoWorker(), root=tmp_path, on_event=lambda _: None))
    assert shift.ok


def test_worktree_refused(projects, tmp_path):
    with pytest.raises(GitError, match="doesn't support several `workspaces`"):
        asyncio.run(run_shift(make_rig(projects), "x", EchoWorker(), root=tmp_path, on_event=lambda _: None, use_worktree=True))
    assert not (tmp_path / ".rig").exists()


def test_cli_check(projects, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rig.yaml").write_text("name: t\nworkspaces:\n  backend: api\n  frontend: web\nhands:\n  a: {role: A}\n", encoding="utf-8")
    main(["check"])
    assert "workspaces: backend (api), frontend (web)" in capsys.readouterr().out
    (tmp_path / "rig.yaml").write_text("name: t\nworkspaces:\n  backend: api\n  frontend: nope\nhands:\n  a: {role: A}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"workspace frontend \(nope\) not found"):
        main(["check"])


RIG = """\
name: review   # keep me
# The project to review.
workspace: api
hands:
  a: {role: A}
"""


def test_serve_switches_between_one_and_several(projects, tmp_path):
    (tmp_path / "review.rig.yaml").write_text(RIG, encoding="utf-8")
    app = App(tmp_path)
    app.set_workspaces("review.rig.yaml", [{"name": "backend", "path": "api"}, {"name": "frontend", "path": "web"}])
    text = (tmp_path / "review.rig.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["workspaces"] == {"backend": "api", "frontend": "web"}
    assert "workspace:" not in text and "# keep me" in text and "# The project to review." in text
    entry = app.rigs()[0]
    assert entry["workspaces"] == [{"name": "backend", "path": "api", "ok": True}, {"name": "frontend", "path": "web", "ok": True}]

    app.set_workspaces("review.rig.yaml", [{"name": "", "path": "web"}])
    data = yaml.safe_load((tmp_path / "review.rig.yaml").read_text(encoding="utf-8"))
    assert data["workspace"] == "web" and "workspaces" not in data and data["hands"] == {"a": {"role": "A"}}


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "at least one"),
        ([{"name": "backend", "path": "api"}, {"name": "", "path": "web"}], "give each one a name"),
        ([{"name": "x", "path": "api"}, {"name": "x", "path": "web"}], "must be different"),
        ([{"name": "my app", "path": "api"}, {"name": "b", "path": "web"}], "plain folder-like names"),
    ],
)
def test_serve_rejects_bad_projects(projects, tmp_path, rows, message):
    (tmp_path / "review.rig.yaml").write_text(RIG, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        App(tmp_path).set_workspaces("review.rig.yaml", rows)
    assert (tmp_path / "review.rig.yaml").read_text(encoding="utf-8") == RIG
