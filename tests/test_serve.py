"""rig serve against a real local server on a free port."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from rig.serve import App, make_handler

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
    assert call(base + "/api/workspace", {"file": "review.rig.yaml", "workspace": str(tmp_path / "other proj")})[0] == 200
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


def test_guards(server):
    _, base = server
    # Other Host names (DNS rebinding) and POSTs without the X-Rig header (other sites) are refused.
    assert call(base + "/api/rigs", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/run", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert call(base + "/shifts/..%2F..%2Fetc/report")[0] == 404
