"""rig serve against a real local server on a free port."""

import asyncio
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from rig import spec
from rig.runner import ResumeError, ShiftEvent, run_shift
from rig.serve import App, Run, make_handler

RIG = """\
name: review
workspace: proj
inputs:
  module: {required: true}
hands:
  a: {role: "Review {{ inputs.module }}.", tools: [read_file]}
  b: {role: B}
lines: ["a -> b"]
"""


@pytest.fixture
def server(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "review.rig.yaml").write_text(RIG, encoding="utf-8")
    (tmp_path / "other.yaml").write_text("services: {}\n", encoding="utf-8")  # not a rig: ignored
    app = App(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, f"http://localhost:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, body=None, headers=None):
    hdrs = {"X-Rig": "1", "Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as r:
            data = r.read().decode()
            return r.status, (json.loads(data) if r.headers["Content-Type"].startswith("application/json") else data)
    except urllib.error.HTTPError as e:
        data = e.read().decode()
        return e.code, (json.loads(data) if data.startswith("{") else data)


def test_page_and_rigs(server):
    _, base = server
    status, page = call(base + "/")
    assert status == 200 and "<title>rig</title>" in page
    status, rigs = call(base + "/api/rigs")
    assert [r["file"] for r in rigs] == ["review.rig.yaml"]
    r = rigs[0]
    assert r["stages"] == [["a"], ["b"]] and r["workspace_ok"] and not r["writes"]
    assert r["inputs"] == [{"name": "module", "description": "", "required": True, "default": ""}]


def test_unreadable_rig_files_show_their_error(tmp_path):
    (tmp_path / "dup.yaml").write_text("name: d\nhands:\n  a: {role: A}\n  a: {role: B}\n", encoding="utf-8")
    (tmp_path / "korean.yaml").write_bytes("name: t\nhands:\n  a: {role: 역할}\n".encode("cp949"))
    (tmp_path / "notes.yaml").write_bytes("title: 역할\n".encode("cp949"))  # not a rig: ignored
    rigs = App(tmp_path).rigs()
    assert [(r["file"], r["error"]) for r in rigs] == [
        ("dup.yaml", 'duplicate key "a" in hands at line 4 (first at line 3); remove or rename one'),
        ("korean.yaml", "not UTF-8 (byte 0xbf at line 3, column 13); save the file as UTF-8"),
    ]
    with pytest.raises(ValueError, match="not UTF-8"):
        App(tmp_path).set_workspaces("korean.yaml", [{"name": "", "path": "proj"}])
    with pytest.raises(ValueError, match='duplicate key "a"'):
        App(tmp_path).set_workspaces("dup.yaml", [{"name": "", "path": "proj"}])


def test_set_workspace_keeps_the_rest(server, tmp_path):
    _, base = server
    (tmp_path / "other proj").mkdir()
    assert call(base + "/api/workspaces", {"file": "review.rig.yaml", "workspaces": [{"name": "", "path": str(tmp_path / "other proj")}]})[0] == 200
    text = (tmp_path / "review.rig.yaml").read_text(encoding="utf-8")
    assert f'workspace: "{(tmp_path / "other proj").as_posix()}"' in text and "inputs:" in text
    assert call(base + "/api/rigs")[1][0]["workspace_ok"]


def test_dry_run_end_to_end(server):
    app, base = server
    status, body = call(base + "/api/run", {"file": "review.rig.yaml", "task": "look", "inputs": {"module": "orders"}, "dry": True})
    assert status == 200, body
    for _ in range(100):
        state = call(base + "/api/run?since=0")[1]
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "done" and state["shift_id"]
    assert any("▶ a" in line for line in state["lines"])
    shifts = call(base + "/api/shifts")[1]
    assert shifts[0]["id"] == state["shift_id"] and shifts[0]["task"] == "look"
    status, page = call(f"{base}/shifts/{state['shift_id']}/report")
    assert status == 200 and "<code>module</code> = orders" in page


@pytest.mark.parametrize(
    "body, message",
    [
        ({"file": "review.rig.yaml", "task": "x"}, "missing required inputs: module"),
        ({"file": "review.rig.yaml", "task": " ", "inputs": {"module": "m"}}, "enter a task"),
        ({"file": "../review.rig.yaml", "task": "x"}, "no rig file"),
        ({"file": "other.yaml", "task": "x"}, "no rig file"),
    ],
)
def test_run_errors(server, body, message):
    _, base = server
    status, data = call(base + "/api/run", body)
    assert status == 400 and message in data["error"]


def test_concurrent_starts_start_one_run(server):
    app, _ = server
    app._work = lambda *args: None  # the run stays "running"; nothing really runs
    barrier = threading.Barrier(8)
    started, refused = [], []

    def start():
        barrier.wait()
        try:
            started.append(app.start("review.rig.yaml", "x", {"module": "m"}, dry=True, use_worktree=False))
        except ValueError as e:
            refused.append(str(e))

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(started) == 1 and app.run is started[0]
    assert refused == ["a run is already in progress"] * 7


def test_crashed_shift_shows_as_failed(server, monkeypatch):
    app, _ = server

    async def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("rig.runner._run_lines", crash)
    app.start("review.rig.yaml", "x", {"module": "m"}, dry=True, use_worktree=False)
    for _ in range(100):
        if app.run.status != "running":
            break
        time.sleep(0.02)
    assert app.run.status == "failed"
    assert "✗ shift failed: RuntimeError: boom" in app.run.lines


def test_shift_id_comes_from_the_status_event_not_the_log_text(tmp_path):
    run = Run(file="f", task="t", dry=True)
    run.log("shift fake-id · rig 'x' · lines")
    assert run.shift_id is None
    run.status_event(ShiftEvent("started", "abc", tmp_path))
    assert run.shift_id == "abc" and run.lines == ["shift fake-id · rig 'x' · lines"]


def test_shift_id_is_known_while_running(server, monkeypatch):
    app, _ = server
    release = threading.Event()

    async def wait(*args, **kwargs):
        release.wait(5)

    monkeypatch.setattr("rig.runner._run_lines", wait)
    run = app.start("review.rig.yaml", "x", {"module": "m"}, dry=True, use_worktree=False)
    try:
        for _ in range(100):
            if run.shift_id:
                break
            time.sleep(0.02)
        assert run.status == "running" and (app.shifts_dir / run.shift_id).is_dir()
    finally:
        release.set()
    for _ in range(100):
        if run.status != "running":
            break
        time.sleep(0.02)


def test_lone_surrogate_in_live_log_still_answers(server):
    app, base = server
    app.run = Run(file="review.rig.yaml", task="task \udcff", dry=True)
    app.run.log("  ✗ a: bad \udcff name")
    status, state = call(base + "/api/run?since=0")  # call decodes the body as strict UTF-8
    assert status == 200 and state["task"] == "task ?" and state["lines"] == ["  ✗ a: bad ? name"]


def test_guards(server):
    _, base = server
    # Other Host names (DNS rebinding) and POSTs without the X-Rig header (other sites) are refused.
    assert call(base + "/api/rigs", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/run", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert call(base + "/shifts/..%2F..%2Fetc/report")[0] == 404


def test_progress_log_of_a_finished_run(server):
    app, base = server
    call(base + "/api/run", {"file": "review.rig.yaml", "task": "look", "inputs": {"module": "orders"}, "dry": True})
    for _ in range(100):
        state = call(base + "/api/run?since=0")[1]
        if state["status"] != "running":
            break
        time.sleep(0.05)
    shift_dir = app.shifts_dir / state["shift_id"]
    assert not (shift_dir / "running.json").exists()
    status, log = call(f"{base}/shifts/{state['shift_id']}/log")
    assert status == 200 and not log["running"] and log["ok"] is True
    assert log["lines"] == state["lines"][: log["total"]] and any("▶ a" in line for line in log["lines"])
    assert call(base + "/api/shifts")[1][0]["log"]


def test_shift_started_from_the_terminal_shows_as_running(server):
    import os

    app, base = server
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "running.json").write_text(json.dumps({"pid": os.getpid(), "rig": "review", "task": "from cli\nmore"}), encoding="utf-8")
    (d / "progress.log").write_text("shift 20261009-120000 · rig 'review' · lines\n  ▶ a\n  ■ a  do", encoding="utf-8")

    row = call(base + "/api/shifts")[1][0]
    assert row["running"] and row["rig"] == "review" and row["task"] == "from cli" and row["log"]
    log = call(f"{base}/shifts/{d.name}/log")[1]
    assert log["running"] and log["ok"] is None and log["total"] == 2 and log["lines"][1] == "  ▶ a"  # the half-written line waits
    with (d / "progress.log").open("a", encoding="utf-8") as f:
        f.write("ne\n")
    assert call(f"{base}/shifts/{d.name}/log?since=2")[1]["lines"] == ["  ■ a  done"]

    (d / "running.json").unlink()
    assert not call(base + "/api/shifts")[1][0]["running"]


def test_history_project_folders(server):
    import os

    app, base = server
    live_dir = app.shifts_dir / "20261009-130000"
    live_dir.mkdir(parents=True)
    folders = [{"name": "", "dir": "D:/work/proj"}]
    (live_dir / "running.json").write_text(json.dumps({"pid": os.getpid(), "rig": "review", "task": "t",
                                                       "workspaces": folders + [{"name": "x"}, "junk"]}), encoding="utf-8")
    old = app.shifts_dir / "20261009-120000"
    old.mkdir()
    (old / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "findings": 0}), encoding="utf-8")
    rows = call(base + "/api/shifts")[1]
    assert [r["workspaces"] for r in rows] == [folders, []]  # malformed entries dropped; older shift.json: none
    assert 'id="hist-mine"' in call(base + "/")[1]


def test_history_folders_prefer_shift_json(tmp_path):
    import os

    app = App(tmp_path)
    done = [{"name": "backend", "dir": "D:/work/api"}, {"name": "frontend", "dir": "D:/work/web"}]
    finished = app.shifts_dir / "20261009-120000"
    finished.mkdir(parents=True)
    (finished / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "workspaces": done}), encoding="utf-8")
    both = app.shifts_dir / "20261009-130000"
    both.mkdir()
    (both / "shift.json").write_text(json.dumps({"rig": "review", "workspaces": done}), encoding="utf-8")
    (both / "running.json").write_text(json.dumps({"pid": os.getpid(), "rig": "review", "task": "t",
                                                   "workspaces": [{"name": "", "dir": "D:/other"}]}), encoding="utf-8")
    rows = {r["id"]: r for r in app.shifts()}
    assert rows[finished.name]["workspaces"] == done
    assert rows[both.name]["running"] and rows[both.name]["workspaces"] == done


def test_history_reads_findings_count_from_shift_json(server):
    app, base = server
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "hands": {"a": {}}, "findings": 3}), encoding="utf-8")
    (d / "a.md").write_text("no findings here", encoding="utf-8")
    assert call(base + "/api/shifts")[1][0]["findings"] == 3  # only shift.json is read


def test_history_survives_broken_shift_json(server):
    app, base = server
    good = app.shifts_dir / "20261009-120000"
    good.mkdir(parents=True)
    (good / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "findings": 1}), encoding="utf-8")
    broken = app.shifts_dir / "20261009-130000"
    broken.mkdir()
    (broken / "shift.json").write_text('{"rig": "x", "ok": tr', encoding="utf-8")
    (broken / "a.md").write_text(json.dumps({"findings": [{"title": "x"}]}), encoding="utf-8")

    rows = call(base + "/api/shifts")[1]
    assert [r["id"] for r in rows] == [broken.name, good.name]
    assert rows[0]["ok"] is None and rows[0]["findings"] == 0
    assert rows[1]["ok"] is True and rows[1]["findings"] == 1
    assert call(f"{base}/shifts/{broken.name}/log")[1]["ok"] is None
    assert app.shift_log(broken.name, 0)["ok"] is None


def test_history_and_log_status_match_rig_logs(tmp_path):
    app = App(tmp_path)
    shifts = {
        "20261009-120000": None,  # ended without a shift.json
        "20261009-120001": '{"rig": "x", "ok": tr',
        "20261009-120002": json.dumps({"ok": False, "error": "RuntimeError: boom"}),
        "20261009-120003": json.dumps({"ok": False, "stopped": "budget"}),
        "20261009-120004": json.dumps({"ok": False, "stopped": "stopped"}),
        "20261009-120005": json.dumps({"ok": True}),
        "20261009-120006": json.dumps({"ok": False, "error": "x " * 60, "stopped": "budget"}),
    }
    for name, text in shifts.items():
        d = app.shifts_dir / name
        d.mkdir(parents=True)
        (d / "progress.log").write_text("done\n", encoding="utf-8")
        if text is not None:
            (d / "shift.json").write_text(text, encoding="utf-8")
    want = [("incomplete", None), ("unreadable", ...), ("error", "RuntimeError: boom"),
            ("stopped", "budget"), ("stopped", "user"), ("ok", None), ("error", ...)]
    rows = {r["id"]: r for r in app.shifts()}
    for name, (status, reason) in zip(shifts, want):
        for got in (rows[name], app.shift_log(name, 0)):
            assert got["status"] == status and not got["running"], name
            if reason is ...:
                assert isinstance(got["reason"], str) and got["reason"], name
            else:
                assert got["reason"] == reason, name
    long = app.shift_log("20261009-120006", 0)["reason"]
    assert len(long) <= 80 and long.endswith("…") and "  " not in long
    assert rows["20261009-120001"]["ok"] is None and rows["20261009-120005"]["ok"] is True  # old keys stay


def test_page_shows_status_labels_and_reason(server):
    _, base = server
    page = call(base + "/")[1]
    assert "결과 불명" in page and 'id="v-reason"' in page and "reasonText" in page


def test_history_counts_findings_of_older_shift_json(server):
    app, base = server
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "hands": {"a": {}, "b": {}}}), encoding="utf-8")
    (d / "a.md").write_text(json.dumps({"findings": [{"title": "x"}, {"title": "y"}]}), encoding="utf-8")
    (d / "b.md").write_text("plain", encoding="utf-8")
    assert call(base + "/api/shifts")[1][0]["findings"] == 2


def test_running_json_of_a_dead_process_is_ignored(server):
    import subprocess
    import sys

    app, base = server
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "running.json").write_text(json.dumps({"pid": p.pid, "rig": "review", "task": "killed"}), encoding="utf-8")
    assert not call(base + "/api/shifts")[1][0]["running"]
    assert not call(f"{base}/shifts/{d.name}/log")[1]["running"]


def test_shift_prs_in_history_and_log(server):
    app, base = server
    d = app.shifts_dir / "20261009-130000"
    d.mkdir(parents=True)
    (d / "progress.log").write_text("done\n", encoding="utf-8")
    prs = [{"url": "https://github.com/o/r/pull/55", "number": 55, "merged": False, "closed": True},
           {"url": "https://github.com/o/r/pull/56", "number": 56, "merged": True},
           {"url": "https://github.com/o/r/pull/57", "number": 57}, {"note": "push failed"}]
    (d / "shift.json").write_text(json.dumps({"rig": "review", "task": "t", "ok": True, "prs": prs}), encoding="utf-8")
    want = [{"number": 55, "url": "https://github.com/o/r/pull/55", "state": "closed"},
            {"number": 56, "url": "https://github.com/o/r/pull/56", "state": "merged"},
            {"number": 57, "url": "https://github.com/o/r/pull/57", "state": "open"}]
    assert call(base + "/api/shifts")[1][0]["prs"] == want
    assert call(f"{base}/shifts/{d.name}/log")[1]["prs"] == want


def test_folder_picker_lists_subfolders(server, tmp_path):
    _, base = server
    (tmp_path / "proj" / "src").mkdir()
    (tmp_path / "proj" / ".git").mkdir()
    (tmp_path / "proj" / "Beta").mkdir()
    (tmp_path / "proj" / "notes.txt").write_text("x", encoding="utf-8")
    status, d = call(base + "/api/dirs?path=" + urllib.parse.quote(str(tmp_path / "proj")))
    assert status == 200
    assert d["path"] == (tmp_path / "proj").resolve().as_posix()
    assert d["dirs"] == ["Beta", "src"]  # folders only, hidden ones left out, case-insensitive order
    assert d["parent"] == tmp_path.resolve().as_posix() and d["roots"] and not d["more"]
    # A relative path is taken from the rig folder; no path starts at the home folder.
    assert call(base + "/api/dirs?path=proj")[1]["path"] == d["path"]
    assert call(base + "/api/dirs")[1]["path"] == Path.home().resolve().as_posix()


def test_folder_picker_errors(server, tmp_path):
    _, base = server
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    status, d = call(base + "/api/dirs?path=" + urllib.parse.quote(str(tmp_path / "file.txt")))
    assert status == 404 and "not a folder" in d["error"]
    status, d = call(base + "/api/dirs?path=" + urllib.parse.quote(str(tmp_path / "missing")))
    assert status == 404 and "not a folder" in d["error"]


# --- resuming a stopped shift ------------------------------------------------------------------

TASK = "build the app\nwith a second line"


class Interrupting:
    """Each hand writes `files` through the toolbox, then is interrupted (Ctrl-C)."""

    def __init__(self, files):
        self.files = files

    async def run(self, hand, prompt, toolbox, check=None):
        for path, content in self.files.items():
            await toolbox.call("write_file", {"path": path, "content": content})
        raise KeyboardInterrupt


def write_rig(root, repo, file="t.rig.yaml", name="t"):
    root.mkdir(exist_ok=True)
    (root / file).write_text(f"name: {name}\nworkspace: {json.dumps(repo.as_posix())}\n"
                             "hands:\n  coder: {role: Code., tools: [write_file]}\n", encoding="utf-8")


def interrupted_shift(root, files=None):
    """A shift of t.rig.yaml that wrote app/partial.py (or `files`), then was interrupted; returns its id."""
    worker = Interrupting({"app/partial.py": "x = 1\n"} if files is None else files)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_shift(spec.load(root / "t.rig.yaml"), TASK, worker, root=root,
                              on_event=lambda _: None, use_worktree=True))
    return sorted(d.name for d in (root / ".rig" / "shifts").iterdir())[-1]


def finish(run):
    for _ in range(200):
        if run.status != "running":
            break
        time.sleep(0.05)
    return run


@pytest.fixture
def stopped(repo, tmp_path):
    root = tmp_path / "root"
    write_rig(root, repo)
    return root, interrupted_shift(root)


def test_start_resumes_a_stopped_shift(stopped):
    root, old = stopped
    app = App(root)
    run = finish(app.start("t.rig.yaml", "", {}, dry=True, use_worktree=False, resume=old))
    assert run.status == "done" and run.shift_id != old and run.task == TASK
    summary = json.loads((app.shifts_dir / run.shift_id / "shift.json").read_text(encoding="utf-8"))
    assert summary["resumed_from"] == old and summary["task"] == TASK


def test_start_refuses_bad_resumes(stopped):
    root, old = stopped
    app = App(root)
    with pytest.raises(ValueError, match="don't send a task or inputs"):
        app.start("t.rig.yaml", "more work", {}, dry=True, use_worktree=False, resume=old)
    with pytest.raises(ValueError, match="don't send a task or inputs"):
        app.start("t.rig.yaml", "", {"x": "1"}, dry=True, use_worktree=False, resume=old)
    with pytest.raises(ResumeError, match="no shift 'nope'"):
        app.start("t.rig.yaml", "", {}, dry=True, use_worktree=False, resume="nope")
    ok = finish(app.start("t.rig.yaml", "plain", {}, dry=True, use_worktree=False)).shift_id
    with pytest.raises(ResumeError, match="finished ok"):
        app.start("t.rig.yaml", "", {}, dry=True, use_worktree=False, resume=ok)


def test_history_says_which_shifts_can_be_resumed(stopped, repo):
    root, old = stopped
    app = App(root)
    unchanged = interrupted_shift(root, files={})
    ok = finish(app.start("t.rig.yaml", "plain", {}, dry=True, use_worktree=False)).shift_id
    rows = {r["id"]: r for r in app.shifts()}
    assert {k: rows[old][k] for k in ("resumable", "resume_file", "resume_problem", "resume_task")} == {
        "resumable": True, "resume_file": "t.rig.yaml", "resume_problem": None, "resume_task": TASK}
    assert rows[old]["task"] == "build the app"  # the table keeps the first line
    for shift in (unchanged, ok):
        assert {k: rows[shift][k] for k in ("resumable", "resume_file", "resume_problem", "resume_task")} == {
            "resumable": False, "resume_file": None, "resume_problem": None, "resume_task": ""}

    write_rig(root, repo, file="copy.rig.yaml")
    row = next(r for r in app.shifts() if r["id"] == old)
    assert row["resumable"] and row["resume_file"] is None and row["resume_problem"] == "many_rigs"
    write_rig(root, repo, file="copy.rig.yaml", name="other")
    write_rig(root, repo, name="renamed")
    row = next(r for r in app.shifts() if r["id"] == old)
    assert row["resumable"] and row["resume_file"] is None and row["resume_problem"] == "no_rig"


def test_history_loads_rig_files_only_for_resumable_shifts(tmp_path, monkeypatch):
    app = App(tmp_path)
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "shift.json").write_text(json.dumps({"rig": "review", "task": "t", "ok": False, "error": "boom"}), encoding="utf-8")
    monkeypatch.setattr(App, "rigs", lambda self: pytest.fail("rigs() loaded"))
    assert not app.shifts()[0]["resumable"]


def test_run_api_passes_resume(server, monkeypatch):
    app, base = server
    got = {}

    def start(name, task, inputs, dry, use_worktree, max_cost=None, resume=None):
        got.update(name=name, task=task, resume=resume, max_cost=max_cost)
        if resume == "bad":
            raise ResumeError("shift bad finished ok; nothing to resume")
        return Run(file=name, task="old task", dry=dry)

    monkeypatch.setattr(app, "start", start)
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "resume": "20261009-120000", "dry": True, "max_cost": "2"})
    assert status == 200 and got == {"name": "review.rig.yaml", "task": "", "resume": "20261009-120000", "max_cost": 2.0}
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "resume": "bad"})
    assert status == 400 and data["error"] == "shift bad finished ok; nothing to resume"
    call(base + "/api/run", {"file": "review.rig.yaml", "task": "x", "resume": ""})
    assert got["resume"] is None


def test_page_has_resume_mode(server):
    _, base = server
    page = call(base + "/")[1]
    assert "이어서 실행" in page and "endResume" in page and "이어 하기 취소" in page
