"""`rig serve`: a local web page to pick a rig, run it, watch progress and open reports.

Built on the standard library's HTTP server. It listens on 127.0.0.1 only, answers only
requests addressed to localhost, and requires an `X-Rig` header on every POST, so other
websites open in the same browser can't start a (paid) run.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml
from pydantic import ValidationError

from rig import report
from rig.cost import Meter, usd
from rig.graph import layers
from rig.runner import PROGRESS_LOG, RUNNING, ShiftEvent
from rig.spec import InputError, load

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


@dataclass
class Run:
    file: str
    task: str
    dry: bool
    status: str = "running"  # running | done | stopped | incomplete | failed
    shift_id: str | None = None
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    meter: Meter = field(default_factory=Meter)

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def status_event(self, ev: ShiftEvent) -> None:
        with self.lock:
            if ev.kind == "started":
                self.shift_id = ev.shift_id


class App:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.run: Run | None = None
        # Held from the "already running" check until the new run is started, so two POSTs can't both start one.
        self._lock = threading.Lock()

    @property
    def shifts_dir(self) -> Path:
        return self.root / ".rig" / "shifts"

    # --- rigs ------------------------------------------------------------------------------------

    def rig_files(self) -> list[Path]:
        files = []
        for p in sorted([*self.root.glob("*.yaml"), *self.root.glob("*.yml")]):
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                continue
            if isinstance(data, dict) and "hands" in data:
                files.append(p)
        return files

    def rigs(self) -> list[dict[str, Any]]:
        out = []
        for p in self.rig_files():
            entry: dict[str, Any] = {"file": p.name}
            try:
                rig = load(p)
            except (ValidationError, yaml.YAMLError) as e:
                entry["error"] = str(e)
                out.append(entry)
                continue
            dirs = rig.workspace_dirs(p.parent)
            folders = [
                {"name": "" if n == "." else n, "path": rig.workspace if n == "." else rig.workspaces[n], "ok": d.is_dir()}
                for n, d in dirs.items()
            ]
            stages = [rig.crew] if rig.foreman else layers(list(rig.hands), rig.edges)
            writes = any({"write_file", "edit_file"} & set(h.tools) for h in [*rig.hands.values(), rig.foreman] if h)
            entry.update({
                "name": rig.name,
                "description": rig.description,
                "mode": "foreman" if rig.foreman else "lines",
                "stages": stages,
                "workspaces": folders,
                "max_cost_usd": rig.max_cost_usd,
                "publish": {"pr": rig.publish.pr, "auto_merge": rig.publish.auto_merge},
                "workspace_ok": all(f["ok"] for f in folders),
                "writes": writes,
                "inputs": [
                    {"name": n, "description": s.description, "required": s.required and s.default is None,
                     "default": s.default_text() if s.default is not None else ""}
                    for n, s in rig.inputs.items()
                ],
            })
            out.append(entry)
        return out

    def _rig_path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if path.parent != self.root or path not in self.rig_files():
            raise ValueError(f"no rig file {name!r}")
        return path

    def set_workspaces(self, name: str, entries: list[dict[str, str]]) -> None:
        """Rewrite the rig file's `workspace` / `workspaces`, keeping the rest of the file as written.

        One unnamed entry becomes `workspace: path`; otherwise every entry needs a name and they
        become a `workspaces:` block.
        """
        path = self._rig_path(name)
        rows = [((e.get("name") or "").strip(), (e.get("path") or "").strip().replace("\\", "/")) for e in entries]
        rows = [(n, p) for n, p in rows if p]
        if not rows:
            raise ValueError("add at least one project folder")
        if len(rows) == 1 and not rows[0][0]:
            block = f"workspace: {json.dumps(rows[0][1], ensure_ascii=False)}\n"
        else:
            names = [n for n, _ in rows]
            if not all(names):
                raise ValueError("with several projects, give each one a name (e.g. backend, frontend)")
            if len(set(names)) < len(names):
                raise ValueError("project names must be different")
            block = "workspaces:\n" + "".join(
                f"  {n}: {json.dumps(p, ensure_ascii=False)}\n" for n, p in rows
            )
        text = path.read_text(encoding="utf-8")
        # The current setting: a `workspace:` line and/or a `workspaces:` line with its indented entries.
        current = re.compile(r"^workspaces?:[^\n]*\n?(?:[ \t]+[^\n]*\n?)*", re.M)
        m = current.search(text)
        if m:
            new = text[: m.start()] + block + current.sub("", text[m.end():])
        else:
            new = re.sub(r"^(name:[^\n]*\n)", lambda mm: mm.group(1) + block, text, count=1, flags=re.M)
        load_check = yaml.safe_load(new)
        from rig.spec import Rig

        Rig.model_validate(load_check)  # never write a file rig can't load
        path.write_text(new, encoding="utf-8")

    # --- runs ------------------------------------------------------------------------------------

    def start(self, name: str, task: str, inputs: dict[str, str], dry: bool, use_worktree: bool,
              max_cost: float | None = None) -> Run:
        with self._lock:
            if self.run and self.run.status == "running":
                raise ValueError("a run is already in progress")
            if not task.strip():
                raise ValueError("enter a task")
            path = self._rig_path(name)
            rig = load(path)
            missing = [n for n, d in rig.workspace_dirs(path.parent).items() if not d.is_dir()]
            if missing:
                raise ValueError("project folder not found; fix it first" + ("" if missing == ["."] else f": {', '.join(missing)}"))
            rig.resolve_inputs({k: v for k, v in inputs.items() if v != ""})  # raises InputError
            if max_cost is not None and max_cost <= 0:
                raise ValueError("the cost limit must be more than 0")
            run = Run(file=name, task=task, dry=dry, meter=Meter(max_cost))
            self.run = run
            threading.Thread(target=self._work, args=(run, path, inputs, use_worktree), daemon=True).start()
            return run

    def _work(self, run: Run, path: Path, inputs: dict[str, str], use_worktree: bool) -> None:
        from rig.hand import ClaudeWorker, EchoWorker
        from rig.runner import run_shift

        try:
            rig = load(path)
            worker = EchoWorker() if run.dry else ClaudeWorker()
            shift = asyncio.run(run_shift(
                rig, run.task, worker, root=path.parent, on_event=run.log, use_worktree=use_worktree,
                inputs={k: v for k, v in inputs.items() if v != ""}, meter=run.meter, on_status=run.status_event,
            ))
            run.shift_id = shift.id
            for wt, _ in shift.committed():
                where = f" in {wt.repo}" if len(shift.worktrees) > 1 else ""
                run.log(f"changes are on branch {wt.branch}{where}")
            if shift.error:
                run.status = "failed"  # the "✗ shift failed" line is already in the log
            else:
                run.status = "done" if shift.ok else "stopped" if run.meter.stop_reason else "incomplete"
        except Exception as e:  # shown in the page; the server keeps running
            run.log(f"✗ {type(e).__name__}: {e}")
            run.status = "failed"

    def stop(self) -> None:
        run = self.run
        if not run or run.status != "running":
            raise ValueError("nothing is running")
        if not run.meter.stop_reason:
            run.meter.stop()
            run.log("■ stop requested; hands finish the request they're on, then stop")

    def run_state(self, since: int) -> dict[str, Any]:
        run = self.run
        if not run:
            return {"status": "idle"}
        m = run.meter
        with run.lock:
            return {"status": run.status, "file": run.file, "task": run.task, "dry": run.dry,
                    "shift_id": run.shift_id, "lines": run.lines[since:], "total": len(run.lines),
                    "cost": {"spent": usd(m.spent), "limit": usd(m.limit) if m.limit is not None else None,
                             "unpriced": m.unpriced, "stopping": m.stop_reason}}

    # --- history ---------------------------------------------------------------------------------

    def shifts(self) -> list[dict[str, Any]]:
        if not self.shifts_dir.is_dir():
            return []
        out = []
        for d in sorted(self.shifts_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            s = json.loads((d / "shift.json").read_text(encoding="utf-8")) if (d / "shift.json").exists() else {}
            live = _live(d)  # started here or by `rig run` in a terminal
            task = (s.get("task") or (live or {}).get("task") or "").strip().splitlines()
            findings = s.get("findings")
            if not isinstance(findings, int):  # older shift.json: count from the outputs
                findings = sum(len(h["findings"] or []) for h in report.collect(d)["hands"]) if s else 0
            out.append({
                "id": d.name, "rig": s.get("rig") or (live or {}).get("rig", ""), "ok": s.get("ok"),
                "task": task[0] if task else "",
                "cost": usd(s["totals"].get("cost_usd")) if "totals" in s else "", "findings": findings,
                "running": bool(live) or bool(self.run and self.run.status == "running" and self.run.shift_id == d.name),
                "log": (d / PROGRESS_LOG).exists(),
            })
        return out

    def _shift_dir(self, shift_id: str) -> Path:
        d = (self.shifts_dir / shift_id).resolve()
        if d.parent != self.shifts_dir.resolve() or not d.is_dir():
            raise ValueError(f"no shift {shift_id}")
        return d

    def shift_log(self, shift_id: str, since: int) -> dict[str, Any]:
        """A shift's progress lines from `since` on, and whether it's still running."""
        d = self._shift_dir(shift_id)
        p = d / PROGRESS_LOG
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        lines = text.splitlines()
        if text and not text.endswith("\n"):
            lines.pop()  # half-written; it comes with the next poll
        running = bool(_live(d))
        ok = None
        if not running and (d / "shift.json").exists():
            ok = json.loads((d / "shift.json").read_text(encoding="utf-8")).get("ok")
        return {"lines": lines[since:], "total": len(lines), "running": running, "ok": ok}

    def report_html(self, shift_id: str) -> str:
        return report.render(report.collect(self._shift_dir(shift_id)),
                             fix_link=lambda i: f"/?fix={quote(shift_id)}&n={i}")

    # --- fixing a finding ------------------------------------------------------------------------

    def finding(self, shift_id: str, n: int) -> dict[str, Any]:
        """A fix task for finding n of a shift, plus the project folders that review looked at."""
        data = report.collect(self._shift_dir(shift_id))
        if not 0 <= n < len(data["findings"]):
            raise ValueError(f"no finding {n} in shift {shift_id}")
        review = next((r for r in self.rigs() if r.get("name") == data["summary"].get("rig")), None)
        return {
            "task": report.fix_task(data["findings"][n]),
            "workspaces": [{"name": w["name"], "path": w["path"]} for w in review["workspaces"]] if review else None,
        }

    def init_fix(self, workspaces: list[dict[str, str]]) -> str:
        """Write fix.rig.yaml from the fix template, pointed at `workspaces`."""
        path = self.root / "fix.rig.yaml"
        if path.exists():
            raise ValueError("fix.rig.yaml already exists")
        path.write_text(resources.files("rig").joinpath("template-fix.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        try:
            self.set_workspaces(path.name, workspaces)
        except (ValueError, ValidationError):
            path.unlink()
            raise
        return path.name


def _live(shift_dir: Path) -> dict[str, Any] | None:
    """The shift's running.json while the process that runs it is alive, else None."""
    try:
        info = json.loads((shift_dir / RUNNING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = info.get("pid") if isinstance(info, dict) else None
    # A process killed outright leaves running.json behind; don't show that shift as running forever.
    return info if isinstance(pid, int) and _alive(pid) else None


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)  # signal 0: only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep the terminal for rig's own output
            pass

        def _local(self) -> bool:
            # Rejecting other Host names blocks DNS-rebinding pages from reaching the server.
            host = self.headers.get("Host") or ""
            if not host.endswith("]"):  # strip the port, but not from a bare "[::1]"
                host = host.rsplit(":", 1)[0]
            return host in LOCAL_HOSTS

        def _send(self, code: int, body: str | bytes, ctype: str = "application/json; charset=utf-8") -> None:
            # errors="replace": a lone surrogate in a log line or task becomes "?" instead of breaking the response.
            data = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def do_GET(self) -> None:
            if not self._local():
                return self._send(403, "forbidden", "text/plain")
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    return self._send(200, PAGE, "text/html; charset=utf-8")
                if url.path == "/api/rigs":
                    return self._json(200, app.rigs())
                if url.path == "/api/run":
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                    return self._json(200, app.run_state(since))
                if url.path == "/api/shifts":
                    return self._json(200, app.shifts())
                if url.path == "/api/finding":
                    q = parse_qs(url.query)
                    return self._json(200, app.finding(q.get("shift", [""])[0], int(q.get("n", ["-1"])[0])))
                m = re.fullmatch(r"/shifts/([^/]+)/log", url.path)
                if m:
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                    return self._json(200, app.shift_log(unquote(m.group(1)), since))
                m = re.fullmatch(r"/shifts/([^/]+)/report", url.path)
                if m:
                    return self._send(200, app.report_html(unquote(m.group(1))), "text/html; charset=utf-8")
            except ValueError as e:
                return self._json(404, {"error": str(e)})
            self._send(404, "not found", "text/plain")

        def do_POST(self) -> None:
            if not self._local() or self.headers.get("X-Rig") != "1":
                return self._send(403, "forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
                if self.path == "/api/workspaces":
                    app.set_workspaces(body["file"], body["workspaces"])
                    return self._json(200, {"ok": True})
                if self.path == "/api/stop":
                    app.stop()
                    return self._json(200, {"ok": True})
                if self.path == "/api/init-fix":
                    return self._json(200, {"file": app.init_fix(body["workspaces"])})
                if self.path == "/api/run":
                    limit = body.get("max_cost")
                    run = app.start(body["file"], body.get("task", ""), body.get("inputs") or {},
                                    bool(body.get("dry")), bool(body.get("worktree")),
                                    float(limit) if limit not in (None, "") else None)
                    return self._json(200, {"ok": True, "file": run.file})
            except (ValueError, KeyError, InputError, ValidationError, yaml.YAMLError) as e:
                return self._json(400, {"error": str(e)})
            self._send(404, "not found", "text/plain")

    return Handler


def serve(root: Path, port: int = 8000, open_browser: bool = True) -> None:
    app = App(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://localhost:{server.server_address[1]}/"
    print(f"rig serve → {url}  (folder {app.root}; Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        # Plain HTTP on purpose: the server only listens on loopback, where there's no network to
        # protect and no certificate to serve HTTPS with.
        server.serve_forever()  # NOSONAR
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rig</title>
<style>
:root{--bg:#f6f7f5;--surface:#fff;--fg:#1c2420;--muted:#5f6a64;--line:#dde2dc;--accent:#2e6b5a;--accent-fg:#fff;--soft:#e2eee9;
--ok:#2e6b5a;--warn:#a06b0b;--bad:#b3401c;--code:#eef0ec;
--font:"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;--mono:Consolas,"SFMono-Regular",ui-monospace,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#131815;--surface:#1a201d;--fg:#e3e8e4;--muted:#9aa69f;--line:#2c3530;--accent:#7cc2ab;--accent-fg:#10201a;
--soft:#1f2f29;--ok:#7cc2ab;--warn:#e6c068;--bad:#f29a6e;--code:#222924;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 var(--font);padding:28px 16px 64px}
main{max-width:1080px;margin:0 auto;display:grid;gap:28px}
h1{font-size:22px;margin:0}h2{font-size:16px;margin:0 0 10px}
.muted{color:var(--muted)}.small{font-size:13px}
a{color:var(--accent)}
code{font-family:var(--mono);font-size:.9em;background:var(--code);padding:1px 4px;border-radius:3px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:24px;align-items:start}
@media (max-width:820px){.grid{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px 20px;display:grid;gap:12px;min-width:0}
.rigs{display:grid;gap:8px}
.rig{border:1px solid var(--line);border-radius:8px;padding:12px 14px;cursor:pointer;display:grid;gap:4px;background:var(--surface);text-align:left;font:inherit;color:inherit}
.rig[aria-pressed=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.rig b{font-size:15px}.stages{font-family:var(--mono);font-size:12px;color:var(--muted)}
.tag{display:inline-block;font-size:11px;padding:0 7px;border-radius:999px;border:1px solid currentColor;margin-left:6px;vertical-align:1px}
.tag.read{color:var(--ok)}.tag.write{color:var(--warn)}
.ws-rows{display:grid;gap:6px}
.ws-row{display:grid;grid-template-columns:110px minmax(0,1fr) 22px 30px;gap:6px;align-items:center}
.ws-row .mark{text-align:center}
.icon-btn{font:inherit;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:6px;height:32px;cursor:pointer}
#ws-box{display:grid;gap:8px}#ws-box[hidden]{display:none}
p.field{margin:0}
.status-ok{color:var(--ok)}.status-bad{color:var(--bad)}
label.field{display:grid;gap:4px;font-size:13px;color:var(--muted)}
input[type=text],input[type=number],textarea{font:inherit;font-size:14px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 10px;width:100%}
textarea{min-height:96px;resize:vertical}
.opts{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px}
button.primary,button.secondary{font:inherit;font-size:14px;border-radius:6px;padding:7px 16px;cursor:pointer;border:1px solid var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);font-weight:600}
button.secondary{background:transparent;color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
.notice{border:1px solid var(--accent);background:var(--soft);border-radius:8px;padding:12px 14px;display:grid;gap:8px;font-size:14px}
.notice[hidden]{display:none}.notice p{margin:0}
.cost-field input{max-width:160px}
.cost{font-variant-numeric:tabular-nums;font-size:13px;font-weight:600}
button.danger{font:inherit;font-size:14px;border-radius:6px;padding:7px 16px;cursor:pointer;border:1px solid var(--bad);background:transparent;color:var(--bad)}
.error{color:var(--bad);font-size:14px;white-space:pre-wrap}
.log{font:12.5px/1.55 var(--mono);background:var(--code);border-radius:6px;padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}
.pill{font-size:12px;font-weight:600;padding:1px 9px;border-radius:999px;border:1px solid currentColor}
.pill.running{color:var(--warn)}.pill.done{color:var(--ok)}.pill.incomplete,.pill.failed,.pill.stopped{color:var(--bad)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#viewer[hidden]{display:none}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
td.id{font-family:var(--mono);font-size:12.5px;white-space:nowrap}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.table-wrap{overflow-x:auto}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
</head>
<body>
<main>
  <header><h1>rig</h1></header>
  <div class="grid">
    <section class="panel" aria-labelledby="rigs-h">
      <h2 id="rigs-h">1. 설정 선택</h2>
      <div class="rigs" id="rigs"><p class="muted small">불러오는 중…</p></div>
      <div id="ws-box" hidden>
        <p class="field" id="ws-label">작업할 프로젝트 폴더 (workspace)</p>
        <div class="ws-rows" id="ws-rows" role="group" aria-labelledby="ws-label"></div>
        <div class="row">
          <button class="secondary" id="ws-add" type="button">+ 프로젝트 추가</button>
          <button class="primary" id="ws-save" type="button">저장</button>
        </div>
        <p class="small muted" id="ws-hint">프로젝트가 2개 이상이면 각각 이름을 붙이세요. hand들은 <code>이름/경로</code>로 파일을 봅니다 (예: <code>backend/src/...</code>).</p>
        <p class="small" id="ws-status"></p>
      </div>
    </section>
    <section class="panel" aria-labelledby="run-h">
      <h2 id="run-h">2. 요구사항 입력 후 실행</h2>
      <div class="notice" id="fix-box" hidden>
        <p id="fix-msg"></p>
        <div class="row" id="fix-actions"></div>
      </div>
      <label class="field" for="task">요구사항
        <textarea id="task" placeholder="예: 이 프로젝트를 리뷰해줘 / 주문 목록에 기간 검색 조건을 추가해줘"></textarea>
      </label>
      <div id="inputs"></div>
      <label class="field cost-field" for="max-cost">비용 상한 (USD, 넘으면 자동으로 멈춤)
        <input type="number" id="max-cost" min="0.01" step="0.5" placeholder="없음">
      </label>
      <div class="opts">
        <label><input type="checkbox" id="dry"> API 호출 없이 테스트 (dry)</label>
        <label><input type="checkbox" id="worktree"> 수정 결과를 별도 브랜치에 (worktree)</label>
      </div>
      <div class="row"><button class="primary" id="go">실행</button><button class="danger" id="stop" type="button" hidden>중지</button><span class="error" id="run-error"></span></div>
      <div id="progress" hidden>
        <div class="row"><span class="pill" id="pill"></span><span class="small muted" id="run-title"></span><span class="cost" id="run-cost"></span><a id="report-link" target="_blank" hidden>결과 리포트 열기 →</a></div>
        <pre class="log" id="log" aria-live="polite"></pre>
      </div>
    </section>
  </div>
  <section class="panel" aria-labelledby="hist-h">
    <h2 id="hist-h">지난 실행</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>실행</th><th>설정</th><th>요구사항</th><th>상태</th><th class="num">발견</th><th class="num">비용</th><th></th></tr></thead>
      <tbody id="shifts"></tbody>
    </table></div>
    <div class="row"><p class="small muted" style="margin:0">터미널에서 <code>rig run</code>으로 시작한 실행도 새로고침 없이 여기 나타나고, 아래 로그 창에 자동으로 열립니다. 끝나면 탭 제목에 표시됩니다.</p>
      <button class="secondary" id="notify" type="button" hidden>끝나면 알림 받기</button></div>
  </section>
  <section class="panel" id="viewer" aria-labelledby="viewer-h" hidden>
    <div class="row"><h2 id="viewer-h">실행 로그</h2><span class="pill" id="v-pill"></span>
      <a id="v-report" target="_blank">결과 리포트 열기 →</a>
      <button class="secondary" id="v-close" type="button">닫기</button></div>
    <pre class="log" id="v-log" aria-live="polite"></pre>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let rigs = [], selected = null, since = 0, polling = null;
try { selected = localStorage.getItem('rig.selected'); } catch (e) {}

async function api(path, body) {
  const opts = body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Rig': '1'}, body: JSON.stringify(body)} : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v); }
  for (const k of kids) e.append(k);
  return e;
}

async function loadRigs() {
  rigs = await api('/api/rigs');
  const box = $('rigs'); box.textContent = '';
  if (!rigs.length) { box.append(el('p', {class: 'muted small'}, '이 폴더에 rig 설정 파일(*.yaml)이 없습니다. 터미널에서 rig init --template review 로 만들 수 있습니다.')); return; }
  if (fix && fix.pick) {
    // Coming from a report's fix button: prefer fix.rig.yaml, else any rig that can edit code.
    const writer = rigs.find(r => r.file === 'fix.rig.yaml' && !r.error) || rigs.find(r => r.writes && !r.error);
    if (writer) selected = writer.file;
    fix.pick = false;
  }
  if (!rigs.some(r => r.file === selected)) selected = (rigs.find(r => !r.error) || rigs[0]).file;
  for (const r of rigs) {
    const b = el('button', {class: 'rig', type: 'button', 'aria-pressed': String(r.file === selected), onclick: () => select(r.file)});
    if (r.error) { b.append(el('b', {}, r.file), el('span', {class: 'error small'}, '설정 오류: ' + r.error.split('\n').slice(0, 3).join(' '))); }
    else {
      const title = el('b', {}, r.name);
      title.append(el('span', {class: 'tag ' + (r.writes ? 'write' : 'read')}, r.writes ? '코드 수정' : '읽기 전용'));
      if (r.publish && r.publish.pr) title.append(el('span', {class: 'tag write'}, r.publish.auto_merge ? 'PR + 자동 병합' : 'PR'));
      b.append(title, el('span', {class: 'small muted'}, r.file + (r.description ? ' · ' + r.description : '')),
        el('span', {class: 'stages'}, r.stages.map(s => s.join(' | ')).join('  →  ')),
        ...r.workspaces.map(w => el('span', {class: 'small ' + (w.ok ? 'status-ok' : 'status-bad')},
          (w.ok ? '✓ ' : '✗ 폴더 없음: ') + (w.name ? w.name + ' · ' : '') + w.path)));
    }
    box.append(b);
  }
  renderSelected();
}
function select(file) { selected = file; try { localStorage.setItem('rig.selected', file); } catch (e) {} loadRigs(); }
function sameFolders(a, b) {
  const key = ws => JSON.stringify(ws.map(w => [w.name || '', w.path]).sort());
  return key(a) === key(b);
}
function renderFix(r) {
  const box = $('fix-box'), actions = $('fix-actions');
  if (!fix) { box.hidden = true; return; }
  box.hidden = false; actions.textContent = '';
  const writer = r && !r.error && r.writes;
  const button = (label, onclick) => actions.append(el('button', {type: 'button', class: 'secondary', onclick}, label));
  if (!rigs.some(x => x.writes && !x.error)) {
    $('fix-msg').textContent = '리뷰에서 수정 요청을 가져왔습니다. 코드를 고칠 수 있는 설정이 아직 없어서 먼저 만들어야 합니다.';
    if (fix.workspaces) button('수정용 설정 만들기 (리뷰와 같은 폴더)', async () => {
      try { await api('/api/init-fix', {workspaces: fix.workspaces}); fix.pick = true; await loadRigs(); }
      catch (e) { $('fix-msg').textContent = e.message; }
    });
    return;
  }
  if (!writer) {
    $('fix-msg').textContent = '선택한 설정은 읽기 전용이라 코드를 고칠 수 없습니다. 왼쪽에서 "코드 수정" 설정을 고르세요.';
    return;
  }
  if (fix.workspaces && !sameFolders(fix.workspaces, r.workspaces)) {
    $('fix-msg').textContent = '선택한 설정의 프로젝트 폴더가 리뷰한 폴더와 다릅니다.';
    button('리뷰와 같은 폴더로 맞추기', async () => {
      try { await api('/api/workspaces', {file: r.file, workspaces: fix.workspaces}); await loadRigs(); }
      catch (e) { $('fix-msg').textContent = e.message; }
    });
    return;
  }
  $('fix-msg').textContent = '리뷰에서 수정 요청을 가져왔습니다. 아래 내용을 확인하고 실행을 누르세요. 수정은 별도 브랜치(worktree)에 하는 것을 권합니다.';
  button('닫기', () => { fix = null; renderFix(r); });
}
function renderSelected() {
  const r = rigs.find(x => x.file === selected);
  renderFix(r);
  $('ws-box').hidden = !r || !!r.error;
  const box = $('inputs'); box.textContent = '';
  if (!r || r.error) return;
  $('max-cost').value = r.max_cost_usd ?? '';
  const rows = $('ws-rows'); rows.textContent = '';
  for (const w of r.workspaces) addRow(w);
  $('ws-status').textContent = r.workspace_ok ? '✓ 폴더를 모두 찾았습니다.' : '✗ 찾을 수 없는 폴더가 있습니다. 경로를 확인하고 저장하세요.';
  $('ws-status').className = 'small ' + (r.workspace_ok ? 'status-ok' : 'status-bad');
  for (const i of r.inputs) {
    const id = 'in-' + i.name;
    box.append(el('label', {class: 'field', for: id}, i.name + (i.required ? ' (필수)' : '') + (i.description ? ' · ' + i.description : ''),
      el('input', {type: 'text', id, 'data-name': i.name, placeholder: i.default ? '기본값: ' + i.default : ''})));
  }
}
let rowSeq = 0;
function addRow(w = {name: '', path: '', ok: null}) {
  const n = ++rowSeq;
  const mark = w.ok === null ? '' : w.ok ? '✓' : '✗';
  const row = el('div', {class: 'ws-row'},
    el('input', {type: 'text', class: 'ws-name', id: 'ws-name-' + n, 'aria-label': '프로젝트 이름', placeholder: '이름 (예: backend)', spellcheck: 'false'}),
    el('input', {type: 'text', class: 'ws-path', id: 'ws-path-' + n, 'aria-label': '폴더 경로', placeholder: 'D:/05_project/...', spellcheck: 'false'}),
    el('span', {class: 'mark ' + (w.ok ? 'status-ok' : 'status-bad')}, mark),
    el('button', {type: 'button', class: 'icon-btn', 'aria-label': '이 프로젝트 빼기', onclick: () => row.remove()}, '×'));
  row.querySelector('.ws-name').value = w.name; row.querySelector('.ws-path').value = w.path;
  $('ws-rows').append(row);
}
$('ws-add').addEventListener('click', () => addRow());
$('ws-save').addEventListener('click', async () => {
  const workspaces = [...document.querySelectorAll('.ws-row')].map(r => ({name: r.querySelector('.ws-name').value, path: r.querySelector('.ws-path').value}));
  try { await api('/api/workspaces', {file: selected, workspaces}); await loadRigs(); }
  catch (e) { $('ws-status').textContent = e.message; $('ws-status').className = 'small status-bad'; }
});

$('go').addEventListener('click', async () => {
  $('run-error').textContent = '';
  const inputs = {};
  for (const i of document.querySelectorAll('#inputs input')) inputs[i.dataset.name] = i.value;
  try {
    await api('/api/run', {file: selected, task: $('task').value, inputs, dry: $('dry').checked, worktree: $('worktree').checked,
                           max_cost: $('max-cost').value});
    since = 0; $('log').textContent = ''; poll();
  } catch (e) { $('run-error').textContent = e.message; }
});

$('stop').addEventListener('click', async () => {
  try { await api('/api/stop', {}); poll(); } catch (e) { $('run-error').textContent = e.message; }
});

const LABEL = {running: '실행 중', done: '완료', stopped: '중지됨', incomplete: '미완료', failed: '실패'};
async function poll() {
  clearTimeout(polling);
  const s = await api('/api/run?since=' + since).catch(() => null);
  if (!s || s.status === 'idle') return;
  $('progress').hidden = false;
  $('pill').className = 'pill ' + s.status; $('pill').textContent = LABEL[s.status] || s.status;
  $('run-title').textContent = s.file + ' · ' + s.task.split('\n')[0].slice(0, 80) + (s.dry ? ' (dry)' : '');
  if (s.lines.length) { const log = $('log'); log.textContent += s.lines.join('\n') + '\n'; log.scrollTop = log.scrollHeight; since = s.total; }
  const link = $('report-link');
  link.hidden = !(s.shift_id && s.status !== 'running');
  if (s.shift_id) link.href = '/shifts/' + encodeURIComponent(s.shift_id) + '/report';
  $('go').disabled = s.status === 'running';
  $('stop').hidden = s.status !== 'running';
  $('stop').disabled = !!(s.cost && s.cost.stopping);
  if (s.cost) {
    const c = s.cost;
    $('run-cost').textContent = '비용 ' + (c.unpriced ? '≥' : '') + c.spent + (c.limit ? ' / 상한 ' + c.limit : '')
      + (c.stopping === 'budget' ? ' · 상한 도달' : c.stopping ? (s.status === 'running' ? ' · 중지 중' : ' · 중지됨') : '');
  }
  if (s.shift_id) ownShift = s.shift_id;
  if (ownRunning && s.status !== 'running') finished(s.shift_id || '', s.status === 'done' ? 'done' : s.status);
  ownRunning = s.status === 'running';
  if (s.status === 'running') polling = setTimeout(poll, 1000); else loadShifts();
}
let ownShift = null, ownRunning = false;

async function loadShifts() {
  const rows = await api('/api/shifts');
  const body = $('shifts'); body.textContent = '';
  if (!rows.length) { body.append(el('tr', {}, el('td', {colspan: '7', class: 'muted small'}, '아직 실행 기록이 없습니다.'))); return; }
  for (const s of rows) {
    const state = s.running ? '실행 중' : s.ok === true ? '완료' : s.ok === false ? '미완료' : '-';
    const links = el('span', {class: 'row'});
    if (s.log) links.append(el('a', {href: '#viewer', onclick: e => { e.preventDefault(); openLog(s.id); }}, '로그'));
    if (!s.running) links.append(el('a', {href: '/shifts/' + encodeURIComponent(s.id) + '/report', target: '_blank'}, '리포트'));
    body.append(el('tr', {},
      el('td', {class: 'id'}, s.id), el('td', {}, s.rig), el('td', {}, s.task),
      el('td', {}, state), el('td', {class: 'num'}, String(s.findings || '')), el('td', {class: 'num'}, s.cost), el('td', {}, links)));
  }
  follow(rows);
}
// A run that starts elsewhere (rig run in a terminal) opens in the log panel by itself, unless
// another running one is already open there. Runs started from this page show above instead.
const followed = new Set();
function follow(rows) {
  for (const s of rows) {
    if (!s.running || !s.log || followed.has(s.id) || s.id === ownShift) continue;
    followed.add(s.id);
    if (!viewing || !vRunning) openLog(s.id, false);
  }
}
// Pick up runs started elsewhere (rig run in a terminal) and ones that finished.
setInterval(() => { if (!document.hidden) loadShifts().catch(() => {}); }, 5000);

let viewing = null, vSince = 0, vTimer = null, vRunning = false;
function openLog(id, scroll = true) {
  viewing = id; vSince = 0; vRunning = false; $('v-log').textContent = '';
  $('viewer-h').textContent = '실행 로그 · ' + id;
  $('v-report').href = '/shifts/' + encodeURIComponent(id) + '/report';
  $('viewer').hidden = false;
  if (scroll) $('viewer').scrollIntoView({behavior: 'smooth'});
  pollLog();
}
async function pollLog() {
  clearTimeout(vTimer);
  const id = viewing;
  if (!id) return;
  const s = await api('/shifts/' + encodeURIComponent(id) + '/log?since=' + vSince).catch(() => null);
  if (!s || id !== viewing) return;
  if (s.lines.length) { const log = $('v-log'); log.textContent += s.lines.join('\n') + '\n'; log.scrollTop = log.scrollHeight; vSince = s.total; }
  const state = s.running ? 'running' : s.ok === false ? 'incomplete' : 'done';
  $('v-pill').className = 'pill ' + state;
  $('v-pill').textContent = LABEL[state];
  $('v-report').hidden = s.running;
  if (vRunning && !s.running) finished(id, state);
  vRunning = s.running;
  if (s.running) vTimer = setTimeout(pollLog, 1000); else loadShifts();
}

// When a run ends: the tab title says so, and a desktop notification if allowed.
const TITLE = document.title;
function finished(id, state) {
  const what = (state === 'done' ? '✓ ' : '✗ ') + LABEL[state];
  document.title = what + ' · ' + TITLE;
  if (window.Notification && Notification.permission === 'granted') {
    try { new Notification('rig 실행 ' + LABEL[state], {body: id}); } catch (e) {}
  }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) document.title = TITLE; });
function notifyButton() {
  const b = $('notify');
  b.hidden = !(window.Notification && Notification.permission === 'default');
}
$('notify').addEventListener('click', async () => {
  try { await Notification.requestPermission(); } catch (e) {}
  notifyButton();
});
notifyButton();
$('v-close').addEventListener('click', () => { viewing = null; clearTimeout(vTimer); $('viewer').hidden = true; });

let fix = null;
async function start() {
  const params = new URLSearchParams(location.search);
  if (params.has('fix')) {
    try {
      const f = await api('/api/finding?shift=' + encodeURIComponent(params.get('fix')) + '&n=' + encodeURIComponent(params.get('n')));
      fix = {workspaces: f.workspaces, pick: true};
      $('task').value = f.task;
      $('worktree').checked = true;
    } catch (e) { $('run-error').textContent = e.message; }
    history.replaceState(null, '', '/');  // a reload shouldn't overwrite edits to the task
  }
  await loadRigs().catch(e => { $('rigs').textContent = e.message; });
}
start();
loadShifts();
poll();
</script>
</body>
</html>
"""
