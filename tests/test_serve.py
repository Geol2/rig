"""rig serve against a real local server on a free port."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from rig.runner import ShiftEvent
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


def test_history_reads_findings_count_from_shift_json(server):
    app, base = server
    d = app.shifts_dir / "20261009-120000"
    d.mkdir(parents=True)
    (d / "shift.json").write_text(json.dumps({"rig": "review", "ok": True, "hands": {"a": {}}, "findings": 3}), encoding="utf-8")
    (d / "a.md").write_text("no findings here", encoding="utf-8")
    assert call(base + "/api/shifts")[1][0]["findings"] == 3  # only shift.json is read


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
