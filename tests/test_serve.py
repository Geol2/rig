"""rig serve against a real local server on a free port."""

import asyncio
import errno
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from rig import spec
from rig.cost import Meter
from rig.hand import HandResult
from rig.runner import ResumeError, ShiftEvent, run_shift
from rig.cli import main
from rig.serve import KIND_DESCRIPTIONS, App, PrStates, Run, _Server, _bind_error, _shutdown, make_handler, serve
from rig.worktree import git

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


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """The history asks GitHub for the state of open PRs; tests answer for it (default: unknown)."""
    states = {}
    monkeypatch.setattr(PrStates, "fetch", lambda self, url: states.get(url))
    return states


@pytest.fixture
def server(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "review.rig.yaml").write_text(RIG, encoding="utf-8")
    (tmp_path / "other.yaml").write_text("services: {}\n", encoding="utf-8")  # not a rig: ignored
    app = App(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    # A short poll so shutdown() doesn't wait the default half second in every test's teardown.
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    # 127.0.0.1, not localhost: on Windows urllib tries ::1 first and waits ~2s before IPv4.
    yield app, f"http://127.0.0.1:{srv.server_address[1]}"
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
    assert r["warnings"] == [] and "rig-warn" in page


def test_rigs_show_the_same_warnings_as_rig_check(tmp_path):
    (tmp_path / "crew.yaml").write_text(
        "name: crew\nhands:\n  a: {role: A}\n  lonely: {role: B}\nforeman: {crew: [a]}\n", encoding="utf-8")
    (tmp_path / "price.yaml").write_text(
        "name: price\nmax_cost_usd: 5\nhands:\n  a: {role: A, model: made-up-model}\n", encoding="utf-8")
    (tmp_path / "broken.yaml").write_text("name: b\nhands: {}\n", encoding="utf-8")
    rigs = {r["file"]: r for r in App(tmp_path).rigs()}
    [crew] = rigs["crew.yaml"]["warnings"]
    assert "hands not on foreman.crew never run" in crew and "lonely" in crew
    [price] = rigs["price.yaml"]["warnings"]
    assert "no price for model 'made-up-model'" in price and "cost limit $" in price
    assert "error" in rigs["broken.yaml"] and "warnings" not in rigs["broken.yaml"]


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


TAB_ERROR = ("YAML syntax error at line 3, column 1: found character '\\t' that cannot start any token; "
             "use spaces, not tabs, to indent")


def test_rig_files_with_yaml_syntax_errors_stay_listed(tmp_path):
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n\ta: {role: A}\n", encoding="utf-8")
    (tmp_path / "x.rig.yaml").write_text("name: x\nhands:\n  a: {role: A}\n  b\n", encoding="utf-8")
    (tmp_path / "y.yaml").write_text("name: y\nhands:\n  a: {role: A}\n b: {role: B}\n", encoding="utf-8")
    (tmp_path / "notes.yaml").write_text("title: n\n\tbody: x\n", encoding="utf-8")  # not a rig: ignored
    app = App(tmp_path)
    assert [(r["file"], r["error"]) for r in app.rigs()] == [
        ("rig.yaml", TAB_ERROR),
        ("x.rig.yaml", "YAML syntax error at line 5, column 1: could not find expected ':' (see line 4); "
                       "check the indentation and the ':' after each key on those lines"),
        ("y.yaml", "YAML syntax error at line 4, column 2: expected <block end>, but found '<block mapping start>' "
                   "(see line 1); check the indentation and the ':' after each key on those lines"),
    ]
    with pytest.raises(ValueError, match="YAML syntax error at line 3, column 1"):
        app.start("rig.yaml", "task", {}, dry=True, use_worktree=False)
    with pytest.raises(ValueError, match="YAML syntax error at line 3, column 1"):  # the file's line, not the rewrite's
        app.set_workspaces("rig.yaml", [{"name": "", "path": "proj"}])
    with pytest.raises(ValueError, match="no rig file"):
        app.set_workspaces("notes.yaml", [{"name": "", "path": "proj"}])


def test_non_utf8_rig_with_yaml_syntax_error_says_not_utf8(tmp_path):
    (tmp_path / "rig.yaml").write_bytes("name: t\nhands:\n\ta: {role: 역할}\n".encode("cp949"))
    assert [(r["file"], r["error"]) for r in App(tmp_path).rigs()] == [
        ("rig.yaml", "not UTF-8 (byte 0xbf at line 3, column 12); save the file as UTF-8"),
    ]


def test_set_workspaces_repairs_a_repeated_workspace_key(tmp_path):
    path = tmp_path / "rig.yaml"
    path.write_text("name: t\nworkspace: a\nworkspace: b\nhands:\n  a: {role: A}\n", encoding="utf-8")
    App(tmp_path).set_workspaces("rig.yaml", [{"name": "", "path": "proj"}])
    assert path.read_text(encoding="utf-8") == 'name: t\nworkspace: "proj"\nhands:\n  a: {role: A}\n'


def test_run_with_yaml_syntax_error_is_400(server, tmp_path):
    _, base = server
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n\ta: {role: A}\n", encoding="utf-8")
    status, body = call(base + "/api/run", {"file": "rig.yaml", "task": "t", "dry": True})
    assert (status, body) == (400, {"error": TAB_ERROR})


def test_set_workspace_keeps_the_rest(server, tmp_path):
    _, base = server
    (tmp_path / "other proj").mkdir()
    assert call(base + "/api/workspaces", {"file": "review.rig.yaml", "workspaces": [{"name": "", "path": str(tmp_path / "other proj")}]})[0] == 200
    text = (tmp_path / "review.rig.yaml").read_text(encoding="utf-8")
    assert f'workspace: "{(tmp_path / "other proj").as_posix()}"' in text and "inputs:" in text
    assert call(base + "/api/rigs")[1][0]["workspace_ok"]


def _no_text_mode_writes(monkeypatch):
    """Fail on Path.write_text: on Windows its newline translation turns a CRLF file's \\r\\n into \\r\\r\\n."""
    def write_text(self, *args, **kwargs):
        raise AssertionError(f"text-mode write to {self}")

    monkeypatch.setattr(Path, "write_text", write_text)


def test_set_workspaces_keeps_crlf(server, tmp_path, monkeypatch):
    app, _ = server
    path = tmp_path / "review.rig.yaml"
    path.write_bytes(RIG.replace("\n", "\r\n").encode("utf-8"))
    for d in ("api", "web"):
        (tmp_path / d).mkdir()
    _no_text_mode_writes(monkeypatch)
    app.set_workspaces("review.rig.yaml", [{"name": "", "path": "api"}])
    app.set_workspaces("review.rig.yaml", [{"name": "backend", "path": "api"}, {"name": "frontend", "path": "web"}])
    data = path.read_bytes()
    assert b"\r\r\n" not in data
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b"workspace:" not in data and b"workspaces:\r\n  backend: \"api\"\r\n  frontend: \"web\"\r\n" in data
    assert spec.load(path).workspaces == {"backend": "api", "frontend": "web"}
    app.set_workspaces("review.rig.yaml", [{"name": "", "path": "proj"}])
    assert path.read_bytes() == RIG.replace("workspace: proj", 'workspace: "proj"').replace("\n", "\r\n").encode("utf-8")


def test_set_workspaces_keeps_lf(server, tmp_path, monkeypatch):
    app, _ = server
    (tmp_path / "api").mkdir()
    (tmp_path / "review.rig.yaml").write_bytes(RIG.encode("utf-8"))  # the fixture's write_text is CRLF on Windows
    _no_text_mode_writes(monkeypatch)
    app.set_workspaces("review.rig.yaml", [{"name": "backend", "path": "api"}])
    data = (tmp_path / "review.rig.yaml").read_bytes()
    assert b"\r" not in data and b"workspaces:\n  backend: \"api\"\n" in data


def test_init_fix_writes_without_text_mode(tmp_path, monkeypatch):
    (tmp_path / "api").mkdir()
    _no_text_mode_writes(monkeypatch)
    assert App(tmp_path).init_fix([{"name": "", "path": "api"}]) == "fix.rig.yaml"
    data = (tmp_path / "fix.rig.yaml").read_bytes()
    assert b"\r" not in data
    assert spec.load(tmp_path / "fix.rig.yaml").workspace == "api"


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
    assert refused == ["this setting is already running; stop it or wait for it to finish"] * 7


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
    assert 'id="hist-tabs"' in call(base + "/")[1]


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


class UntilStopped:
    """Each hand writes `files` through the toolbox, then works until the shift is asked to stop."""

    files: dict[str, str] = {}
    meter: Meter | None = None

    async def run(self, hand, prompt, toolbox, check=None):
        for path, content in self.files.items():
            await toolbox.call("write_file", {"path": path, "content": content})
        while not self.meter.stop_reason:
            await asyncio.sleep(0.01)
        return HandResult(hand.name, f"[{self.meter.message()}]", self.meter.stop_reason, 0)


def wait_for(cond):
    for _ in range(400):
        if cond():
            return
        time.sleep(0.025)
    raise AssertionError("timed out")


def test_shutdown_stops_running_shifts_and_lets_them_save(repo, tmp_path, monkeypatch):
    root = tmp_path / "root"
    write_rig(root, repo)
    monkeypatch.setattr(UntilStopped, "files", {"app/x.py": "x = 1\n"})
    monkeypatch.setattr("rig.hand.EchoWorker", UntilStopped)
    app = App(root)
    run = app.start("t.rig.yaml", "task", {}, dry=True, use_worktree=True)
    wait_for(lambda: run.shift_id and (root / ".rig" / "worktrees" / run.shift_id / "app" / "x.py").exists())
    assert app.shutdown(10) is True
    assert run.status == "stopped" and not app.running()
    folder = app.shifts_dir / run.shift_id
    assert json.loads((folder / "shift.json").read_text(encoding="utf-8"))["stopped"] == "stopped"
    assert not (folder / "running.json").exists()
    assert git("show", f"rig/{run.shift_id}:app/x.py", cwd=repo).strip() == "x = 1"


def test_shutdown_with_nothing_running(tmp_path):
    assert App(tmp_path).shutdown(0) is True


class Stubborn:
    """Ignores the stop until `release` is set."""

    started = threading.Event()
    release = threading.Event()

    async def run(self, hand, prompt, toolbox, check=None):
        self.started.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return HandResult(hand.name, "done", "end_turn", 0)


def test_shutdown_times_out(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    monkeypatch.setattr(Stubborn, "started", threading.Event())
    monkeypatch.setattr(Stubborn, "release", threading.Event())
    monkeypatch.setattr("rig.hand.EchoWorker", Stubborn)
    app = App(tmp_path)
    run = app.start("rig.yaml", "go", {}, dry=True, use_worktree=False)
    assert Stubborn.started.wait(10)  # a stop before the hand starts would end the shift at once
    assert app.shutdown(0.2) is False and run.meter.stop_reason == "stopped"
    Stubborn.release.set()
    assert app.shutdown(10) is True and run.status != "running"


def test_ctrl_c_messages(tmp_path, monkeypatch):
    lines = []

    def out(line, flush=False):
        lines.append(line)

    _shutdown(App(tmp_path), out)
    assert lines == []  # nothing running: quits silently
    (tmp_path / "t.rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    monkeypatch.setattr("rig.hand.EchoWorker", UntilStopped)
    app = App(tmp_path)
    run = app.start("t.rig.yaml", "go", {}, dry=True, use_worktree=False)
    wait_for(lambda: run.shift_id)
    _shutdown(app, out)
    sid = run.shift_id
    assert lines == [
        f"rig serve: stopping 1 running shift (t.rig.yaml {sid}): hands finish their current request first; "
        "Ctrl+C again to quit now",
        f"rig serve: stopped {sid}: continue with `rig run --resume <id>` or the page's \"이어 하기\" button",
    ]


@pytest.fixture
def no_serving(monkeypatch):
    """serve() must never really serve or open a browser in these tests."""
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail(f"opened a browser at {url}"))
    monkeypatch.setattr(_Server, "serve_forever", lambda self, **kw: pytest.fail("started serving"))


def test_serve_on_a_port_in_use_says_so(tmp_path, capsys, no_serving):
    # A plain socket without SO_REUSEADDR, so on Windows too the port is really taken.
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            serve(tmp_path, port=port, open_browser=False)
    # Windows may give 10048 ("already in use") or 10013 ("in use or reserved by Windows").
    assert exc.value.code.startswith(f"rig: port {port} is ")
    assert f"`rig serve --port {port + 1}`" in exc.value.code
    assert capsys.readouterr().out == ""  # no startup line


def test_second_rig_serve_on_the_same_port_fails(tmp_path, capsys, no_serving):
    first = _Server(("127.0.0.1", 0), make_handler(App(tmp_path)))
    try:
        port = first.server_address[1]
        with pytest.raises(SystemExit) as exc:
            serve(tmp_path, port=port, open_browser=False)
    finally:
        first.server_close()
    assert exc.value.code.startswith(f"rig: port {port} is ") and f"--port {port + 1}" in exc.value.code
    assert capsys.readouterr().out == ""


def test_serve_on_port_0_shows_the_port_it_got(tmp_path, capsys, monkeypatch):
    def stop(self, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(_Server, "serve_forever", stop)
    serve(tmp_path, port=0, open_browser=False)
    out = capsys.readouterr().out
    port = int(out.split("http://localhost:")[1].split("/")[0])
    assert port != 0 and out.startswith(f"rig serve → http://localhost:{port}/")


def test_bind_error_messages():
    in_use = OSError(errno.EADDRINUSE, "Address already in use")
    assert "`rig serve --port 8001`" in _bind_error(8000, in_use)
    assert _bind_error(65535, in_use).endswith("`rig serve --port 8000`)")
    assert _bind_error(80, OSError(errno.EACCES, "Permission denied")) == (
        "rig: can't listen on port 80: Permission denied (try another port, e.g. `rig serve --port 8000`)")
    exclusive = OSError(errno.EACCES, "access denied")
    exclusive.winerror = 10013  # Windows, when another socket holds the port with SO_EXCLUSIVEADDRUSE
    message = _bind_error(8000, exclusive)  # 10013 may also mean a port reserved by Hyper-V/WSL/Docker
    assert "port 8000 is in use or reserved by Windows" in message and "excludedportrange" in message
    assert "`rig serve --port 8001`" in message
    win_in_use = OSError(errno.EADDRINUSE, "in use")
    win_in_use.winerror = 10048
    assert "port 8000 is already in use" in _bind_error(8000, win_in_use)


@pytest.mark.parametrize("port", ["70000", "-1"])
def test_cli_rejects_ports_out_of_range(port, no_serving):
    with pytest.raises(SystemExit, match="--port must be between 0 and 65535"):
        main(["serve", "--no-open", "--port", port])


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

    def start(name, task, inputs, dry, use_worktree, max_cost=None, resume=None, worker="api"):
        got.update(name=name, task=task, resume=resume, max_cost=max_cost, worker=worker)
        if resume == "bad":
            raise ResumeError("shift bad finished ok; nothing to resume")
        return Run(file=name, task="old task", dry=dry)

    monkeypatch.setattr(app, "start", start)
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "resume": "20261009-120000", "dry": True, "max_cost": "2"})
    assert status == 200 and got == {"name": "review.rig.yaml", "task": "", "resume": "20261009-120000", "max_cost": 2.0,
                                     "worker": "api"}
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "resume": "bad"})
    assert status == 400 and data["error"] == "shift bad finished ok; nothing to resume"
    call(base + "/api/run", {"file": "review.rig.yaml", "task": "x", "resume": "", "worker": "claude-code"})
    assert got["resume"] is None and got["worker"] == "claude-code"


def test_page_has_resume_mode(server):
    _, base = server
    page = call(base + "/")[1]
    assert "이어서 실행" in page and "endResume" in page and "이어 하기 취소" in page


def test_start_refuses_when_prs_cant_be_opened(tmp_path, monkeypatch):
    from rig import publish

    (tmp_path / "pr.rig.yaml").write_text("name: p\npublish: {pr: true}\nhands:\n  a: {role: A}\n", encoding="utf-8")
    app = App(tmp_path)
    app._work = lambda *args: None  # nothing really runs
    checked = []
    monkeypatch.setattr(publish, "check_rig", lambda rig, root: checked.append(root) or ["gh isn't logged in; run `gh auth login`"])
    with pytest.raises(ValueError) as excinfo:
        app.start("pr.rig.yaml", "x", {}, dry=False, use_worktree=False)
    assert str(excinfo.value) == "can't open PRs (publish.pr):\n- gh isn't logged in; run `gh auth login`"
    assert app.run is None and checked == [tmp_path.resolve()]

    checked.clear()
    app.start("pr.rig.yaml", "x", {}, dry=True, use_worktree=False)  # --dry doesn't check
    assert checked == [] and app.run is not None


def test_start_refuses_worktree_run_when_git_has_no_user(anonymous_repo):
    (anonymous_repo / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    app = App(anonymous_repo)
    worked = []
    app._work = lambda *args: worked.append(args)
    with pytest.raises(ValueError) as excinfo:
        app.start("rig.yaml", "x", {}, dry=False, use_worktree=True)
    assert str(excinfo.value).startswith("git can't commit the shift's changes:\n- anon: git doesn't know who you are")
    assert app.run is None and worked == []
    
    
def test_icons_are_served(server):
    _, base = server
    for path, ctype in [("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml")]:
        with urllib.request.urlopen(base + path) as r:
            assert r.status == 200 and r.headers["Content-Type"] == ctype and len(r.read()) > 100
    page = call(base + "/")[1]
    assert 'rel="icon" href="/favicon.svg"' in page and 'id="hist-tabs"' in page


def test_workers_api_says_whether_claude_code_is_installed(server, monkeypatch):
    from rig import claude_code

    _, base = server
    monkeypatch.setattr(claude_code, "find_claude", lambda: "/usr/bin/claude")
    assert call(base + "/api/workers")[1] == {"claude_code": True}
    monkeypatch.setattr(claude_code, "find_claude", lambda: None)
    assert call(base + "/api/workers")[1] == {"claude_code": False}


def test_run_with_claude_code_needs_it_installed(server, monkeypatch):
    from rig import claude_code

    _, base = server
    monkeypatch.setattr(claude_code, "find_claude", lambda: None)
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "task": "x", "worker": "claude-code"})
    assert status == 400 and data["error"] == claude_code.INSTALL_HELP
    status, data = call(base + "/api/run", {"file": "review.rig.yaml", "task": "x", "worker": "gpt"})
    assert status == 400 and "unknown worker 'gpt'" in data["error"]


def test_dry_run_with_claude_code_needs_no_claude(server, monkeypatch):
    from rig import claude_code

    app, base = server
    monkeypatch.setattr(claude_code, "find_claude", lambda: None)
    status, body = call(base + "/api/run", {"file": "review.rig.yaml", "task": "x", "inputs": {"module": "orders"},
                                            "dry": True, "worker": "claude-code"})
    assert status == 200, body
    for _ in range(100):
        state = call(base + "/api/run")[1]
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "done" and state["worker"] == "claude-code"


def test_page_has_the_worker_choice(server):
    _, base = server
    page = call(base + "/")[1]
    assert 'name="worker" value="claude-code"' in page and "/api/workers" in page


def test_history_shows_what_a_shift_changed(server):
    app, base = server
    shifts = app.shifts_dir
    new = shifts / "20261015-000002"
    new.mkdir(parents=True)
    (new / "shift.json").write_text(json.dumps({"rig": "r", "task": "TODO 항목 처리: x", "ok": True,
                                                "title": "serve: 지난 실행에 변경 제목 표시", "final": ["coder"]}), encoding="utf-8")
    # Recorded before shift.json kept the title: read from the final hand's reply.
    old = shifts / "20261015-000001"
    old.mkdir()
    (old / "shift.json").write_text(json.dumps({"rig": "r", "task": "TODO 항목 처리: y", "ok": True,
                                                "final": ["foreman"]}), encoding="utf-8")
    (old / "foreman.md").write_text("**TITLE:** tools: CP949 파일 보호\n\n요약", encoding="utf-8")
    # No TITLE line, and a final key that would be a path: no title.
    none = shifts / "20261015-000000"
    none.mkdir()
    (none / "shift.json").write_text(json.dumps({"rig": "r", "task": "z", "ok": True,
                                                 "final": ["../../secret", "coder"]}), encoding="utf-8")
    (none / "coder.md").write_text("did it", encoding="utf-8")
    rows = {r["id"]: r for r in call(base + "/api/shifts")[1]}
    assert rows["20261015-000002"]["title"] == "serve: 지난 실행에 변경 제목 표시"
    assert rows["20261015-000001"]["title"] == "tools: CP949 파일 보호"
    assert rows["20261015-000000"]["title"] == ""
    assert "function taskCell" in call(base + "/")[1]


def test_history_shows_the_current_state_of_open_prs(server, no_github):
    app, base = server
    for i, merged in enumerate([False, False, False, True]):
        d = app.shifts_dir / f"20261015-00000{i}"
        d.mkdir(parents=True)
        (d / "shift.json").write_text(json.dumps({"rig": "r", "task": "t", "ok": True, "prs": [
            {"url": f"https://github.com/o/r/pull/{i}", "number": i, "merged": merged}]}), encoding="utf-8")
    no_github.update({"https://github.com/o/r/pull/0": "merged", "https://github.com/o/r/pull/1": "closed"})

    def states():
        return {r["prs"][0]["number"]: r["prs"][0]["state"] for r in call(base + "/api/shifts")[1]}

    for _ in range(100):  # GitHub is asked in the background
        if states() == {0: "merged", 1: "closed", 2: "open", 3: "merged"}:
            break
        time.sleep(0.02)
    # Merged or closed since the shift; #2 still unknown (no gh, say); #3 was recorded merged: never asked.
    assert states() == {0: "merged", 1: "closed", 2: "open", 3: "merged"}
    assert "https://github.com/o/r/pull/3" not in app.pr_states.known


WRITER = "name: {name}\nworkspace: proj\nhands:\n  c: {{role: C, tools: [write_file]}}\n"


def test_each_rig_file_has_its_own_run(server):
    app, base = server
    app._work = lambda *args: None  # runs stay "running"
    for name in ("fix.rig.yaml", "fix2.rig.yaml"):
        (app.root / name).write_text(WRITER.format(name=name.split(".")[0]), encoding="utf-8")
    review = app.start("review.rig.yaml", "look", {"module": "m"}, dry=False, use_worktree=False)  # read-only
    fix = app.start("fix.rig.yaml", "change", {}, dry=False, use_worktree=False)  # writes, same folder: fine with a reader
    # A second writer straight into the same folder would overwrite the first one's changes.
    with pytest.raises(ValueError, match="fix.rig.yaml is running in the same project folder"):
        app.start("fix2.rig.yaml", "change too", {}, dry=False, use_worktree=False)
    app.start("fix2.rig.yaml", "change too", {}, dry=False, use_worktree=True)  # in its own worktree: fine
    with pytest.raises(ValueError, match="already running"):
        app.start("fix.rig.yaml", "again", {}, dry=True, use_worktree=False)

    state = call(base + "/api/run?file=review.rig.yaml&since=0")[1]
    assert state["task"] == "look" and state["status"] == "running"
    assert sorted(state["others"]) == ["fix.rig.yaml", "fix2.rig.yaml"]
    assert call(base + "/api/run?file=other.yaml")[1] == {
        "status": "idle", "file": "other.yaml", "others": ["review.rig.yaml", "fix.rig.yaml", "fix2.rig.yaml"]}
    assert call(base + "/api/run")[1]["file"] == "fix2.rig.yaml"  # no file: the last one started

    assert call(base + "/api/stop", {"file": "fix.rig.yaml"})[0] == 200
    assert fix.meter.stop_reason and not review.meter.stop_reason


def test_page_follows_the_selected_rig(server):
    page = call(server[1] + "/")[1]
    assert "function showRunOf" in page and "'/api/run?file='" in page and 'id="others"' in page


@pytest.mark.parametrize("kind, writes, pr, merge", [("review", False, False, False), ("edit", True, False, False),
                                                    ("merge", True, True, True)])
def test_new_setting_from_the_page(server, tmp_path, kind, writes, pr, merge):
    app, base = server
    status, data = call(base + "/api/rig", {"title": "주문 화면 작업", "kind": kind,
                                             "workspaces": [{"name": "", "path": str(tmp_path / "proj")}]})
    assert status == 200, data
    rig = {r["file"]: r for r in call(base + "/api/rigs")[1]}[data["file"]]
    assert rig["name"] == "주문 화면 작업" and rig["workspace_ok"] and rig["writes"] == writes
    assert rig["description"] == KIND_DESCRIPTIONS[kind]
    assert (rig["publish"]["pr"], rig["publish"]["auto_merge"]) == (pr, merge)
    # The same name again gets its own file.
    again = call(base + "/api/rig", {"title": "주문 화면 작업", "kind": kind,
                                     "workspaces": [{"name": "", "path": str(tmp_path / "proj")}]})[1]["file"]
    assert data["file"] == "rig-1.rig.yaml" and again == "rig-2.rig.yaml"


@pytest.mark.parametrize("body, error", [
    ({"title": " ", "kind": "edit"}, "enter a name"),
    ({"title": "x", "kind": "deploy"}, "unknown kind 'deploy'"),
    ({"title": "x", "kind": "edit", "workspaces": []}, "add at least one project folder"),
])
def test_new_setting_refused(server, body, error):
    app, base = server
    before = sorted(p.name for p in app.root.iterdir())
    status, data = call(base + "/api/rig", body)
    assert status == 400 and error in data["error"]
    assert sorted(p.name for p in app.root.iterdir()) == before  # nothing left behind


def test_publish_mode_of_a_setting(server, tmp_path):
    app, base = server
    file = app.create_rig("fix things", "edit", [{"name": "", "path": str(tmp_path / "proj")}])
    path = app.root / file
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))  # saved on Windows

    def publish():
        return {r["file"]: r for r in call(base + "/api/rigs")[1]}[file]["publish"]

    assert call(base + "/api/publish", {"file": file, "mode": "merge"})[0] == 200
    assert publish() == {"pr": True, "auto_merge": True, "can_merge": True}
    assert spec.load(path).publish.approver == "reviewer"  # the foreman's required reviewer
    assert b"\r\r" not in path.read_bytes() and b"publish:\r\n" in path.read_bytes()
    assert call(base + "/api/publish", {"file": file, "mode": "pr"})[0] == 200
    assert publish()["pr"] and not publish()["auto_merge"]
    assert call(base + "/api/publish", {"file": file, "mode": "branch"})[0] == 200
    assert not publish()["pr"] and not publish()["auto_merge"]
    # A read-only setting has nothing to publish.
    status, data = call(base + "/api/publish", {"file": "review.rig.yaml", "mode": "pr"})
    assert status == 400 and "only reads files" in data["error"]
    status, data = call(base + "/api/publish", {"file": file, "mode": "push"})
    assert status == 400 and "unknown mode" in data["error"]


def test_auto_merge_needs_a_reviewer(server, tmp_path):
    app, base = server
    (app.root / "solo.rig.yaml").write_text("name: solo\nworkspace: proj\nhands:\n  c: {role: C, tools: [write_file]}\n",
                                            encoding="utf-8")
    status, data = call(base + "/api/publish", {"file": "solo.rig.yaml", "mode": "merge"})
    assert status == 400 and "needs a hand that reviews" in data["error"]
    assert call(base + "/api/publish", {"file": "solo.rig.yaml", "mode": "pr"})[0] == 200


def test_page_can_add_settings(server):
    page = call(server[1] + "/")[1]
    assert 'id="new-box"' in page and "'/api/rig'" in page and 'id="publish-mode"' in page


def test_shift_killed_before_it_recorded_anything(server):
    # The page (or a terminal) was closed mid-run: running.json of a dead process, no shift.json.
    app, base = server
    d = app.shifts_dir / "20261015-180203"
    d.mkdir(parents=True)
    (d / "running.json").write_text(json.dumps({"pid": 999999999, "rig": "rig-self-dev", "task": "TODO 처리\nmore",
                                                "workspaces": [{"name": "self-dev", "dir": "D:/rig"}]}), encoding="utf-8")
    (row,) = call(base + "/api/shifts")[1]
    assert row["rig"] == "rig-self-dev" and row["task"] == "TODO 처리" and row["workspaces"]
    assert row["status"] == "error" and row["reason"].startswith("rig이 실행 도중 꺼져서 중단됨")
    assert not row["resumable"] and not row["running"]


def test_todo_list_of_the_selected_setting(server):
    app, base = server
    assert call(base + "/api/todo?file=review.rig.yaml")[1] == {"found": False}
    (app.root / "proj" / "TODO.md").write_text("## Backlog\n\n- [ ] **do x**: because\n", encoding="utf-8")
    t = call(base + "/api/todo?file=review.rig.yaml")[1]
    assert t["found"] and t["backlog"] == [{"title": "do x", "detail": "because"}]
    assert call(base + "/api/todo?file=../x.yaml")[0] == 404
    page = call(base + "/")[1]
    assert 'id="todo-box"' in page and "function useTodo" in page and "TODO 항목 처리: ${title}" in page


def test_page_has_board_tabs_and_skins(server):
    page = call(server[1] + "/")[1]
    assert 'id="vt-todo"' in page and 'id="vt-hist"' in page and "function showView" in page
    assert 'value="terminal"' in page and ":root[data-skin=terminal]" in page and "localStorage.getItem('rig.skin')" in page
