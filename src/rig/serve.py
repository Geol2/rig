"""`rig serve`: a local web page to pick a rig, run it, watch progress and open reports.

Built on the standard library's HTTP server. It listens on 127.0.0.1 only, answers only
requests addressed to localhost, and requires an `X-Rig` header on every POST, so other
websites open in the same browser can't start a (paid) run.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import yaml
from pydantic import ValidationError

from rig import report
from rig.cost import usd
from rig.graph import layers
from rig.spec import InputError, load

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


@dataclass
class Run:
    file: str
    task: str
    dry: bool
    status: str = "running"  # running | done | incomplete | failed
    shift_id: str | None = None
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            m = re.match(r"shift (\S+) ·", line)
            if m and not self.shift_id:
                self.shift_id = m.group(1)


class App:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.run: Run | None = None

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
            workspace = (p.parent / rig.workspace).resolve()
            stages = [rig.crew] if rig.foreman else layers(list(rig.hands), rig.edges)
            writes = any({"write_file", "edit_file"} & set(h.tools) for h in [*rig.hands.values(), rig.foreman] if h)
            entry.update({
                "name": rig.name,
                "description": rig.description,
                "mode": "foreman" if rig.foreman else "lines",
                "stages": stages,
                "workspace": rig.workspace,
                "workspace_ok": workspace.is_dir(),
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

    def set_workspace(self, name: str, workspace: str) -> None:
        path = self._rig_path(name)
        workspace = workspace.strip().replace("\\", "/")
        if not workspace:
            raise ValueError("workspace is empty")
        text = path.read_text(encoding="utf-8")
        line = f"workspace: {json.dumps(workspace, ensure_ascii=False)}"
        new, n = re.subn(r"^workspace:.*$", line, text, count=1, flags=re.M)
        if not n:
            new = re.sub(r"^(name:.*)$", rf"\1\n{line}", text, count=1, flags=re.M)
        path.write_text(new, encoding="utf-8")

    # --- runs ------------------------------------------------------------------------------------

    def start(self, name: str, task: str, inputs: dict[str, str], dry: bool, use_worktree: bool) -> Run:
        if self.run and self.run.status == "running":
            raise ValueError("a run is already in progress")
        if not task.strip():
            raise ValueError("enter a task")
        path = self._rig_path(name)
        rig = load(path)
        if not (path.parent / rig.workspace).is_dir():
            raise ValueError(f"workspace {rig.workspace} not found; set it first")
        rig.resolve_inputs({k: v for k, v in inputs.items() if v != ""})  # raises InputError
        run = Run(file=name, task=task, dry=dry)
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
                inputs={k: v for k, v in inputs.items() if v != ""},
            ))
            run.shift_id = shift.id
            if shift.outcome and shift.outcome.changed and not shift.outcome.kept_at:
                run.log(f"changes are on branch {shift.worktree.branch}")
            run.status = "done" if shift.ok else "incomplete"
        except Exception as e:  # shown in the page; the server keeps running
            run.log(f"✗ {type(e).__name__}: {e}")
            run.status = "failed"

    def run_state(self, since: int) -> dict[str, Any]:
        run = self.run
        if not run:
            return {"status": "idle"}
        with run.lock:
            return {"status": run.status, "file": run.file, "task": run.task, "dry": run.dry,
                    "shift_id": run.shift_id, "lines": run.lines[since:], "total": len(run.lines)}

    # --- history ---------------------------------------------------------------------------------

    def shifts(self) -> list[dict[str, Any]]:
        if not self.shifts_dir.is_dir():
            return []
        out = []
        for d in sorted(self.shifts_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            s = json.loads((d / "shift.json").read_text(encoding="utf-8")) if (d / "shift.json").exists() else {}
            task = (s.get("task") or "").strip().splitlines()
            findings = sum(len(h["findings"] or []) for h in report.collect(d)["hands"]) if s else 0
            out.append({
                "id": d.name, "rig": s.get("rig", ""), "ok": s.get("ok"), "task": task[0] if task else "",
                "cost": usd(s["totals"].get("cost_usd")) if "totals" in s else "", "findings": findings,
                "running": bool(self.run and self.run.status == "running" and self.run.shift_id == d.name),
            })
        return out

    def report_html(self, shift_id: str) -> str:
        d = (self.shifts_dir / shift_id).resolve()
        if d.parent != self.shifts_dir.resolve() or not d.is_dir():
            raise ValueError(f"no shift {shift_id}")
        return report.render(report.collect(d))


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
            data = body.encode("utf-8") if isinstance(body, str) else body
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
                if self.path == "/api/workspace":
                    app.set_workspace(body["file"], body["workspace"])
                    return self._json(200, {"ok": True})
                if self.path == "/api/run":
                    run = app.start(body["file"], body.get("task", ""), body.get("inputs") or {},
                                    bool(body.get("dry")), bool(body.get("worktree")))
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
        server.serve_forever()
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
.ws{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.ws input{flex:1;min-width:200px}
.status-ok{color:var(--ok)}.status-bad{color:var(--bad)}
label.field{display:grid;gap:4px;font-size:13px;color:var(--muted)}
input[type=text],textarea{font:inherit;font-size:14px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 10px;width:100%}
textarea{min-height:96px;resize:vertical}
.opts{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px}
button.primary,button.secondary{font:inherit;font-size:14px;border-radius:6px;padding:7px 16px;cursor:pointer;border:1px solid var(--accent)}
button.primary{background:var(--accent);color:var(--accent-fg);font-weight:600}
button.secondary{background:transparent;color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
.error{color:var(--bad);font-size:14px;white-space:pre-wrap}
.log{font:12.5px/1.55 var(--mono);background:var(--code);border-radius:6px;padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}
.pill{font-size:12px;font-weight:600;padding:1px 9px;border-radius:999px;border:1px solid currentColor}
.pill.running{color:var(--warn)}.pill.done{color:var(--ok)}.pill.incomplete,.pill.failed{color:var(--bad)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
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
        <label class="field" for="ws">작업할 프로젝트 폴더 (workspace)
          <span class="ws"><input type="text" id="ws" spellcheck="false"><button class="secondary" id="ws-save">저장</button></span>
        </label>
        <p class="small" id="ws-status"></p>
      </div>
    </section>
    <section class="panel" aria-labelledby="run-h">
      <h2 id="run-h">2. 요구사항 입력 후 실행</h2>
      <label class="field" for="task">요구사항
        <textarea id="task" placeholder="예: 이 프로젝트를 리뷰해줘 / 주문 목록에 기간 검색 조건을 추가해줘"></textarea>
      </label>
      <div id="inputs"></div>
      <div class="opts">
        <label><input type="checkbox" id="dry"> API 호출 없이 테스트 (dry)</label>
        <label><input type="checkbox" id="worktree"> 수정 결과를 별도 브랜치에 (worktree)</label>
      </div>
      <div class="row"><button class="primary" id="go">실행</button><span class="error" id="run-error"></span></div>
      <div id="progress" hidden>
        <div class="row"><span class="pill" id="pill"></span><span class="small muted" id="run-title"></span><a id="report-link" target="_blank" hidden>결과 리포트 열기 →</a></div>
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
  if (!rigs.some(r => r.file === selected)) selected = (rigs.find(r => !r.error) || rigs[0]).file;
  for (const r of rigs) {
    const b = el('button', {class: 'rig', type: 'button', 'aria-pressed': String(r.file === selected), onclick: () => select(r.file)});
    if (r.error) { b.append(el('b', {}, r.file), el('span', {class: 'error small'}, '설정 오류: ' + r.error.split('\n').slice(0, 3).join(' '))); }
    else {
      const title = el('b', {}, r.name);
      title.append(el('span', {class: 'tag ' + (r.writes ? 'write' : 'read')}, r.writes ? '코드 수정' : '읽기 전용'));
      b.append(title, el('span', {class: 'small muted'}, r.file + (r.description ? ' · ' + r.description : '')),
        el('span', {class: 'stages'}, r.stages.map(s => s.join(' | ')).join('  →  ')),
        el('span', {class: 'small ' + (r.workspace_ok ? 'status-ok' : 'status-bad')}, (r.workspace_ok ? '✓ ' : '✗ 폴더 없음: ') + r.workspace));
    }
    box.append(b);
  }
  renderSelected();
}
function select(file) { selected = file; try { localStorage.setItem('rig.selected', file); } catch (e) {} loadRigs(); }
function renderSelected() {
  const r = rigs.find(x => x.file === selected);
  $('ws-box').hidden = !r || !!r.error;
  const box = $('inputs'); box.textContent = '';
  if (!r || r.error) return;
  $('ws').value = r.workspace;
  $('ws-status').textContent = r.workspace_ok ? '✓ 폴더를 찾았습니다.' : '✗ 폴더를 찾을 수 없습니다. 경로를 확인하고 저장하세요.';
  $('ws-status').className = 'small ' + (r.workspace_ok ? 'status-ok' : 'status-bad');
  for (const i of r.inputs) {
    const id = 'in-' + i.name;
    box.append(el('label', {class: 'field', for: id}, i.name + (i.required ? ' (필수)' : '') + (i.description ? ' · ' + i.description : ''),
      el('input', {type: 'text', id, 'data-name': i.name, placeholder: i.default ? '기본값: ' + i.default : ''})));
  }
}
$('ws-save').addEventListener('click', async () => {
  try { await api('/api/workspace', {file: selected, workspace: $('ws').value}); await loadRigs(); }
  catch (e) { $('ws-status').textContent = e.message; $('ws-status').className = 'small status-bad'; }
});

$('go').addEventListener('click', async () => {
  $('run-error').textContent = '';
  const inputs = {};
  for (const i of document.querySelectorAll('#inputs input')) inputs[i.dataset.name] = i.value;
  try {
    await api('/api/run', {file: selected, task: $('task').value, inputs, dry: $('dry').checked, worktree: $('worktree').checked});
    since = 0; $('log').textContent = ''; poll();
  } catch (e) { $('run-error').textContent = e.message; }
});

const LABEL = {running: '실행 중', done: '완료', incomplete: '미완료', failed: '실패'};
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
  if (s.status === 'running') polling = setTimeout(poll, 1000); else loadShifts();
}

async function loadShifts() {
  const rows = await api('/api/shifts');
  const body = $('shifts'); body.textContent = '';
  if (!rows.length) { body.append(el('tr', {}, el('td', {colspan: '7', class: 'muted small'}, '아직 실행 기록이 없습니다.'))); return; }
  for (const s of rows) {
    const state = s.running ? '실행 중' : s.ok === true ? '완료' : s.ok === false ? '미완료' : '-';
    body.append(el('tr', {},
      el('td', {class: 'id'}, s.id), el('td', {}, s.rig), el('td', {}, s.task),
      el('td', {}, state), el('td', {class: 'num'}, String(s.findings || '')), el('td', {class: 'num'}, s.cost),
      el('td', {}, el('a', {href: '/shifts/' + encodeURIComponent(s.id) + '/report', target: '_blank'}, '리포트'))));
  }
}

loadRigs().catch(e => { $('rigs').textContent = e.message; });
loadShifts();
poll();
</script>
</body>
</html>
"""
