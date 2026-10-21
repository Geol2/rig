"""rig.todo: the open backlog of a project's TODO.md, as the `rig serve` page lists it."""

from pathlib import Path

from rig import todo
from rig.worktree import git

TODO = """# TODO

Intro text.

## Done

- [x] **old one**: finished
  long ago.

## Backlog

From a review:

- [ ] **`search` skips links**: `tools.py` walks
  with os.walk.
- [x] **checked here**: also done
- [ ] plain item without a bold title

## Needs a design first

- **crews**: reusable groups.
- [ ] **design later**: not picked yet
"""


def test_parse_lists_open_backlog_items_in_order():
    t = todo.parse(TODO)
    assert t["backlog"] == [{"title": "`search` skips links", "detail": "`tools.py` walks with os.walk."},
                            {"title": "plain item without a bold title", "detail": ""}]
    assert t["done"] == 2 and t["later"] == ["crews", "design later"]


def test_read_finds_the_first_project_with_a_todo(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "TODO.md").write_text(TODO, encoding="utf-8")
    t = todo.read({"api": tmp_path / "api", "web": tmp_path / "web"})
    assert t["found"] and t["file"] == "web/TODO.md" and t["from"] is None and len(t["backlog"]) == 2
    assert todo.read({"api": tmp_path / "api"}) == {"found": False}


def test_read_takes_the_newer_upstream_todo(tmp_path):
    # A shift merged its PR on the remote and fetched it; this checkout's TODO.md is behind.
    remote, local, other = tmp_path / "remote.git", tmp_path / "local", tmp_path / "other"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("clone", "-q", str(remote), str(local), cwd=tmp_path)
    for repo in (local,):
        git("config", "user.email", "t@t", cwd=repo)
        git("config", "user.name", "t", cwd=repo)
    (local / "TODO.md").write_text(TODO, encoding="utf-8")
    git("add", "-A", cwd=local)
    git("commit", "-q", "-m", "todo", cwd=local)
    git("push", "-q", "-u", "origin", "main", cwd=local)
    assert todo.read({".": local})["from"] is None  # up to date
    git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=other)
    git("config", "user.name", "t", cwd=other)
    (other / "TODO.md").write_text(TODO.replace("- [ ] **`search` skips links**", "- [x] **`search` skips links**"),
                                   encoding="utf-8")
    git("commit", "-q", "-am", "done", cwd=other)
    git("push", "-q", cwd=other)
    git("fetch", "-q", cwd=local)
    t = todo.read({".": local})
    assert t["from"] == "origin/main" and [i["title"] for i in t["backlog"]] == ["plain item without a bold title"]
    # Local commits of its own (not just behind): the checkout's file is what's shown.
    (local / "x.txt").write_text("x", encoding="utf-8")
    git("add", "-A", cwd=local)
    git("commit", "-q", "-m", "x", cwd=local)
    assert todo.read({".": local})["from"] is None
