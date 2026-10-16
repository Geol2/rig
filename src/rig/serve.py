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
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from importlib import resources
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml
from pydantic import ValidationError

from rig import publish, report
from rig.cost import Meter, price_warnings, usd
from rig.graph import layers
from rig.runner import PROGRESS_LOG, Resume, ShiftEvent, live, load_resume
from rig.spec import InputError, RigFileError, check_yaml_syntax, load, parse_yaml, read_rig_text
from rig.tools import crlf
from rig.worktree import GitError, identity_problems

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
# How long a PR state read from GitHub is trusted before it's asked again.
PR_STATE_TTL = 120
PR_URL = re.compile(r"https://[\w.-]+/[\w.-]+/[\w.-]+/pull/\d+")


class PrStates:
    """Current state of PRs that shift.json recorded as open: they may have been merged or
    closed since (by hand, or by a later shift). Read with `gh pr view` in a background thread,
    so the history (polled every few seconds) never waits on GitHub; until then, and when gh
    isn't there, the recorded state is shown."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.known: dict[str, tuple[str, float]] = {}  # url -> (merged | closed | open, when read)
        self.lock = threading.Lock()
        self.pending: set[str] = set()
        self.busy = False

    def state(self, url: str) -> str | None:
        with self.lock:
            hit = self.known.get(url)
            return hit[0] if hit else None

    def refresh(self, urls: list[str]) -> None:
        """Queue the stale ones; one background thread asks GitHub until the queue is empty."""
        now = time.monotonic()
        with self.lock:
            for u in urls:
                hit = self.known.get(u)
                if hit is None or (hit[0] == "open" and now - hit[1] > PR_STATE_TTL):
                    self.pending.add(u)
            if not self.pending or self.busy:
                return
            self.busy = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            with self.lock:
                if not self.pending:
                    self.busy = False
                    return
                url = self.pending.pop()
            try:
                state = self.fetch(url)
            except Exception:
                state = None
            with self.lock:
                # Unknown (no gh, offline, ...): remembered as "open" too, so it's asked again only after the TTL.
                self.known[url] = (state or "open", time.monotonic())

    def fetch(self, url: str) -> str | None:
        code, out = publish.run_cmd(["gh", "pr", "view", url, "--json", "state", "--jq", ".state"], self.cwd, timeout=20)
        return {"MERGED": "merged", "CLOSED": "closed", "OPEN": "open"}.get(out.strip()) if code == 0 else None


# How hands talk to Claude: the API (ANTHROPIC_API_KEY, billed per use) or the locally installed
# Claude Code signed in with the user's subscription (rig.claude_code).
WORKERS = ("api", "claude-code")


@dataclass
class Run:
    file: str
    task: str
    dry: bool
    worker: str = "api"  # api | claude-code (WORKERS)
    status: str = "running"  # running | done | stopped | incomplete | failed
    shift_id: str | None = None
    lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    meter: Meter = field(default_factory=Meter)
    # The project folders it works in, and whether it leaves them alone (a worktree, read-only
    # hands or --dry): two runs that both change files in the same folder aren't started together.
    dirs: frozenset[str] = frozenset()
    isolated: bool = True

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
        # One run per rig file: the page shows the selected rig's. `run` is the last one started
        # (what a request without a file means).
        self.runs: dict[str, Run] = {}
        self._last: Run | None = None
        # Held from the "already running" check until the new run is started, so two POSTs can't both start one.
        self._lock = threading.Lock()
        self.pr_states = PrStates(self.root)

    @property
    def run(self) -> Run | None:
        return self._last

    @run.setter
    def run(self, run: Run | None) -> None:
        if run is not None:
            self.runs[run.file] = run
        self._last = run

    def _run_of(self, file: str | None) -> Run | None:
        return self.runs.get(file) if file else self._last

    def running(self) -> list[Run]:
        return [r for r in self.runs.values() if r.status == "running"]

    @property
    def shifts_dir(self) -> Path:
        return self.root / ".rig" / "shifts"

    # --- rigs ------------------------------------------------------------------------------------

    def rig_files(self) -> list[Path]:
        files = []
        for p in sorted([*self.root.glob("*.yaml"), *self.root.glob("*.yml")]):
            try:
                raw = p.read_bytes()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:  # still listed if it's a rig, so load() can say what's wrong
                    text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:  # still listed if it looks like a rig, so load() can say where the error is
                if p.name in ("rig.yaml", "rig.yml") or p.name.endswith((".rig.yaml", ".rig.yml")) \
                        or re.search(r"^hands:", text, re.M):
                    files.append(p)
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
            except (ValidationError, yaml.YAMLError, RigFileError) as e:
                entry["error"] = str(e)
                out.append(entry)
                continue
            dirs = rig.workspace_dirs(p.parent)
            folders = [
                {"name": "" if n == "." else n, "path": rig.workspace if n == "." else rig.workspaces[n],
                 "dir": d.as_posix(), "ok": d.is_dir()}
                for n, d in dirs.items()
            ]
            stages = [rig.crew] if rig.foreman else layers(list(rig.hands), rig.edges)
            writes = _writes(rig)
            entry.update({
                "name": rig.name,
                "description": rig.description,
                "mode": "foreman" if rig.foreman else "lines",
                "stages": stages,
                "workspaces": folders,
                "max_cost_usd": rig.max_cost_usd,
                "publish": {"pr": rig.publish.pr, "auto_merge": rig.publish.auto_merge,
                            "can_merge": bool(rig.publish.approver or _approver(rig))},
                "workspace_ok": all(f["ok"] for f in folders),
                "writes": writes,
                "warnings": [*rig.hand_warnings(), *price_warnings(rig.models(), rig.max_cost_usd)],
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
        text = read_rig_text(path)
        # A syntax error is reported at its line in the file, not in the rewritten text; a repeated
        # `workspace:` key is fine, the rewrite below replaces both.
        check_yaml_syntax(text)
        if crlf(text.encode("utf-8")):
            block = block.replace("\n", "\r\n")  # keep the file's line ends; the patterns below take \r as line content
        # The current setting: a `workspace:` line and/or a `workspaces:` line with its indented entries.
        current = re.compile(r"^workspaces?:[^\n]*\n?(?:[ \t]+[^\n]*\n?)*", re.M)
        m = current.search(text)
        if m:
            new = text[: m.start()] + block + current.sub("", text[m.end():])
        else:
            new = re.sub(r"^(name:[^\n]*\n)", lambda mm: mm.group(1) + block, text, count=1, flags=re.M)
        load_check = parse_yaml(new)
        from rig.spec import Rig

        Rig.model_validate(load_check)  # never write a file rig can't load
        path.write_bytes(new.encode("utf-8"))  # no newline translation: Windows would turn \r\n into \r\r\n

    # --- runs ------------------------------------------------------------------------------------

    def start(self, name: str, task: str, inputs: dict[str, str], dry: bool, use_worktree: bool,
              max_cost: float | None = None, resume: str | None = None, worker: str = "api") -> Run:
        """Start a run in the background; with `resume` (`last` or a shift id), continue that
        stopped shift like `rig run --resume`: its task and inputs, always in a worktree.
        `worker` is one of WORKERS."""
        from rig.claude_code import INSTALL_HELP, find_claude

        if worker not in WORKERS:
            raise ValueError(f"unknown worker {worker!r}; use one of: {', '.join(WORKERS)}")
        if worker == "claude-code" and not dry and not find_claude():
            raise ValueError(INSTALL_HELP)
        if resume and (task.strip() or any(v != "" for v in inputs.values())):
            raise ValueError("resume reuses the old shift's task and inputs; don't send a task or inputs")
        with self._lock:
            if name in self.runs and self.runs[name].status == "running":
                raise ValueError("this setting is already running; stop it or wait for it to finish")
            if not resume and not task.strip():
                raise ValueError("enter a task")
            path = self._rig_path(name)
            rig = load(path)
            missing = [n for n, d in rig.workspace_dirs(path.parent).items() if not d.is_dir()]
            if missing:
                raise ValueError("project folder not found; fix it first" + ("" if missing == ["."] else f": {', '.join(missing)}"))
            if max_cost is not None and max_cost <= 0:
                raise ValueError("the cost limit must be more than 0")
            if not dry:
                problems = publish.check_rig(rig, path.parent)  # before any hand spends money
                if problems:
                    raise ValueError("can't open PRs (publish.pr):\n" + "\n".join(f"- {p}" for p in problems))
            res = None
            if resume:
                try:
                    res = load_resume(path.parent, rig, resume)  # ResumeError is a ValueError: shown as 400
                except GitError as e:
                    raise ValueError(str(e)) from None
                task, inputs, use_worktree = res.task, res.inputs, True
            if not dry and (use_worktree or rig.publish.pr):
                # The worktree is committed only after the shift; make sure git can do it.
                problems = identity_problems(rig.workspace_dirs(path.parent).values())
                if problems:
                    raise ValueError("git can't commit the shift's changes:\n" + "\n".join(f"- {p}" for p in problems))
            rig.resolve_inputs({k: v for k, v in inputs.items() if v != ""})  # raises InputError
            dirs = frozenset(d.resolve().as_posix() for d in rig.workspace_dirs(path.parent).values())
            writes = any({"write_file", "edit_file"} & set(h.tools) for h in [*rig.hands.values(), rig.foreman] if h)
            isolated = dry or not writes or use_worktree or rig.publish.pr
            for other in self.running():
                if other.dirs & dirs and not other.isolated and not isolated:
                    raise ValueError(f"{other.file} is running in the same project folder and both change files there "
                                     "directly; wait for it, or turn on worktree")
            run = Run(file=name, task=task, dry=dry, worker=worker, meter=Meter(max_cost), dirs=dirs, isolated=isolated)
            self.run = run
            threading.Thread(target=self._work, args=(run, path, inputs, use_worktree, res), daemon=True).start()
            return run

    def _work(self, run: Run, path: Path, inputs: dict[str, str], use_worktree: bool,
              resume: Resume | None = None) -> None:
        from rig.claude_code import ClaudeCodeWorker
        from rig.hand import ClaudeWorker, EchoWorker
        from rig.runner import run_shift

        try:
            rig = load(path)
            worker = EchoWorker() if run.dry else ClaudeCodeWorker() if run.worker == "claude-code" else ClaudeWorker()
            shift = asyncio.run(run_shift(
                rig, run.task, worker, root=path.parent, on_event=run.log, use_worktree=use_worktree,
                inputs={k: v for k, v in inputs.items() if v != ""}, meter=run.meter, on_status=run.status_event,
                resume=resume,
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

    def stop(self, file: str | None = None) -> None:
        run = self._run_of(file)
        if not run or run.status != "running":
            raise ValueError("nothing is running")
        if not run.meter.stop_reason:
            run.meter.stop()
            run.log("■ stop requested; hands finish the request they're on, then stop")

    def _prs(self, summary: dict[str, Any]) -> list[dict[str, Any]]:
        """_prs, with PRs recorded as open shown as they are on GitHub now (PrStates)."""
        prs = _prs(summary)
        self.pr_states.refresh([p["url"] for p in prs if p["state"] == "open"])
        for p in prs:
            if p["state"] == "open":
                p["state"] = self.pr_states.state(p["url"]) or "open"
        return prs

    def run_state(self, since: int, file: str | None = None) -> dict[str, Any]:
        """The run of `file` (default: the last one started), and which other rig files are running."""
        run = self._run_of(file)
        others = [r.file for r in self.running() if r is not run]
        if not run:
            return {"status": "idle", "file": file, "others": others}
        m = run.meter
        with run.lock:
            return {"status": run.status, "file": run.file, "others": others, "task": run.task, "dry": run.dry, "worker": run.worker,
                    "shift_id": run.shift_id, "lines": run.lines[since:], "total": len(run.lines),
                    "cost": {"spent": usd(m.spent), "limit": usd(m.limit) if m.limit is not None else None,
                             "unpriced": m.unpriced, "stopping": m.stop_reason}}

    # --- history ---------------------------------------------------------------------------------

    def shifts(self) -> list[dict[str, Any]]:
        if not self.shifts_dir.is_dir():
            return []
        out = []
        to_resume: list[tuple[dict[str, Any], Any]] = []  # resumable rows and their shift.json `rig`
        for d in sorted(self.shifts_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            s, unreadable = report.read_summary(d)  # a broken shift.json reads as {}: ok None, not a success
            info = live(d)  # started here or by `rig run` in a terminal
            running = bool(info) or any(r.shift_id == d.name for r in self.running())
            status = report.status(s, running, unreadable is not None)
            task = (s.get("task") or (info or {}).get("task") or "").strip().splitlines()
            findings = s.get("findings")
            if not isinstance(findings, int):  # older shift.json: count from the outputs
                findings = sum(len(h["findings"] or []) for h in report.collect(d)["hands"]) if s else 0
            resumable = not running and status in ("error", "stopped", "incomplete") and _committed(s)
            out.append({
                "id": d.name, "rig": s.get("rig") or (info or {}).get("rig", ""), "ok": s.get("ok"),
                "task": task[0] if task else "",
                "title": report.title_of(d, s) or "",
                "cost": usd(s["totals"].get("cost_usd")) if "totals" in s else "", "findings": findings,
                "running": running, "status": status, "reason": report.status_reason(status, s, unreadable),
                "log": (d / PROGRESS_LOG).exists(),
                "prs": self._prs(s),
                "workspaces": _workspaces(s) or _workspaces(info or {}),
                "resumable": resumable, "resume_file": None, "resume_problem": None,
                "resume_task": (s.get("task") or "").strip() if resumable else "",
            })
            if resumable:
                to_resume.append((out[-1], s.get("rig")))
        # The rig file to resume with: the one with the shift's rig name. rigs() loads every rig
        # file and the page polls this every 5s, so only when some row needs it.
        if to_resume:
            names = [(r["file"], r.get("name")) for r in self.rigs() if "error" not in r]
            for row, rig_name in to_resume:
                files = [f for f, n in names if n == rig_name]
                if len(files) == 1:
                    row["resume_file"] = files[0]
                else:
                    row["resume_problem"] = "many_rigs" if files else "no_rig"
        return out

    # --- folder picker -----------------------------------------------------------------------

    def dirs(self, path: str) -> dict[str, Any]:
        """Subfolders of `path` (default: the home folder) for the page's folder picker.

        Only names are listed, and only to this machine's own browser (the server answers
        localhost only). Hidden folders (starting with ".") are left out.
        """
        start = Path(path).expanduser() if path.strip() else Path.home()
        if not start.is_absolute():
            start = self.root / start
        try:
            here = start.resolve()
            if not here.is_dir():
                raise ValueError(f"not a folder: {path}")
            names = sorted((c.name for c in here.iterdir() if not c.name.startswith(".") and _is_dir(c)),
                           key=str.lower)
        except PermissionError as e:
            raise ValueError(f"can't open {path}: permission denied") from e
        except OSError as e:
            raise ValueError(f"can't open {path}: {e.strerror or e}") from e
        parent = here.parent if here.parent != here else None
        return {"path": here.as_posix(), "parent": parent.as_posix() if parent else None,
                "dirs": names[:DIR_LIMIT], "more": len(names) > DIR_LIMIT, "roots": _roots()}

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
        running = bool(live(d))
        summary, unreadable = report.read_summary(d)
        ok = None if running else summary.get("ok")
        status = report.status(summary, running, unreadable is not None)
        return {"lines": lines[since:], "total": len(lines), "running": running, "ok": ok, "prs": self._prs(summary),
                "status": status, "reason": report.status_reason(status, summary, unreadable)}

    def report_html(self, shift_id: str) -> str:
        return report.render(report.collect(self._shift_dir(shift_id)),
                             fix_link=lambda i: f"/?fix={quote(shift_id)}&n={i}")

    # --- fixing a finding ------------------------------------------------------------------------

    def finding(self, shift_id: str, n: int) -> dict[str, Any]:
        """A fix task for finding n of a shift, plus the project folders that review looked at."""
        data = report.collect(self._shift_dir(shift_id))
        if not 0 <= n < len(data["findings"]):
            raise ValueError(f"no finding {n} in shift {shift_id}")
        task = report.fix_task(data["findings"][n])
        recorded = _workspaces(data["summary"])
        if recorded:
            return {"task": task, "workspaces": [{"name": w["name"], "path": w["dir"], "dir": w["dir"]} for w in recorded],
                    "ambiguous": False}
        # Older shift.json without `workspaces`: guess from the rig files with that name.
        matches = [r for r in self.rigs() if "error" not in r and r.get("name") == data["summary"].get("rig")]
        if not matches:
            return {"task": task, "workspaces": None, "ambiguous": False}
        folders = [{(w["name"], w["dir"]) for w in r["workspaces"]} for r in matches]
        if any(f != folders[0] for f in folders[1:]):
            return {"task": task, "workspaces": None, "ambiguous": True}
        return {"task": task, "workspaces": [{"name": w["name"], "path": w["path"], "dir": w["dir"]}
                                             for w in matches[0]["workspaces"]], "ambiguous": False}

    def create_rig(self, title: str, kind: str, workspaces: list[dict[str, str]]) -> str:
        """Write a new rig file from a bundled template (KINDS), named after `title` and pointed
        at `workspaces`; returns its file name. Nothing is left behind if any step fails."""
        title = " ".join(title.split())
        if not title:
            raise ValueError("enter a name for the setting")
        if len(title) > 60:
            raise ValueError("the name is too long (60 characters at most)")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}; use one of: {', '.join(KINDS)}")
        template, publish_mode = KINDS[kind]
        # Numbered, not named after the title: what the user typed never becomes a path. The
        # page shows the rig's name (the title) anyway.
        n = 1
        while (self.root / f"rig-{n}.rig.yaml").exists():
            n += 1
        path = self.root / f"rig-{n}.rig.yaml"
        text = resources.files("rig").joinpath(template).read_bytes().decode("utf-8")
        text = re.sub(r"^name:[^\n]*", lambda m: f"name: {json.dumps(title, ensure_ascii=False)}", text, count=1, flags=re.M)
        text = re.sub(r"^description:[^\n]*", lambda m: f"description: {json.dumps(KIND_DESCRIPTIONS[kind], ensure_ascii=False)}",
                      text, count=1, flags=re.M)
        path.write_bytes(text.encode("utf-8"))  # as shipped, no newline translation
        try:
            self.set_workspaces(path.name, workspaces)
            if publish_mode != "branch":
                self.set_publish(path.name, publish_mode)
        except (ValueError, ValidationError):
            path.unlink()
            raise
        return path.name

    def set_publish(self, name: str, mode: str) -> None:
        """What happens to a code-changing rig's result (PUBLISH_MODES): left on a branch, a PR,
        or a PR merged once the approver approves and CI passes. Other `publish` keys are kept."""
        if mode not in PUBLISH_MODES:
            raise ValueError(f"unknown mode {mode!r}; use one of: {', '.join(PUBLISH_MODES)}")
        path = self._rig_path(name)
        rig = load(path)
        if not _writes(rig):
            raise ValueError("this setting only reads files; there is nothing to publish")
        text = read_rig_text(path)
        check_yaml_syntax(text)
        current = (parse_yaml(text) or {}).get("publish") or {}
        settings = dict(current) if isinstance(current, dict) else {}
        settings["pr"] = mode != "branch"
        settings["auto_merge"] = mode == "merge"
        if mode == "merge" and not settings.get("approver"):
            approver = _approver(rig)
            if not approver:
                raise ValueError("automatic merging needs a hand that reviews the change (e.g. reviewer); "
                                 "this setting has none")
            settings["approver"] = approver
        if mode == "branch":
            settings = {k: v for k, v in settings.items() if k not in ("pr", "auto_merge")}
        block = ("publish:\n" + "".join(f"  {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in settings.items())
                 if settings else "")
        eol = "\r\n" if crlf(text.encode("utf-8")) else "\n"
        block = block.replace("\n", eol)
        current_block = re.compile(r"^publish:[^\n]*\n?(?:[ \t]+[^\n]*\n?|[ \t]*\r?\n)*", re.M)
        m = current_block.search(text)
        if m:
            new = text[: m.start()] + block + text[m.end():]
        else:
            new = text + ("" if text.endswith("\n") or not block else eol) + block
        from rig.spec import Rig

        Rig.model_validate(parse_yaml(new))  # never write a file rig can't load
        path.write_bytes(new.encode("utf-8"))

    def init_fix(self, workspaces: list[dict[str, str]]) -> str:
        """Write fix.rig.yaml from the fix template, pointed at `workspaces`."""
        path = self.root / "fix.rig.yaml"
        if path.exists():
            raise ValueError("fix.rig.yaml already exists")
        path.write_bytes(resources.files("rig").joinpath("template-fix.yaml").read_bytes())  # as shipped, no newline translation
        try:
            self.set_workspaces(path.name, workspaces)
        except (ValueError, ValidationError):
            path.unlink()
            raise
        return path.name


# New settings from the page: kind -> (bundled template, what happens to the result).
KINDS = {
    "review": ("template-review.yaml", "branch"),   # read-only: reviews and reports, changes nothing
    "edit": ("template-foreman.yaml", "branch"),    # changes code; the result stays on a branch / in place
    "merge": ("template-foreman.yaml", "merge"),    # changes code, opens a PR and merges it when approved
}
KIND_DESCRIPTIONS = {
    "review": "프로젝트를 읽고 버그·보안·DB 문제를 리뷰합니다. 파일은 바꾸지 않습니다.",
    "edit": "foreman이 계획하고 coder가 고치고 reviewer가 검토합니다.",
    "merge": "foreman이 계획하고 coder가 고치고 reviewer가 검토한 뒤, PR을 열어 승인과 CI 통과 시 병합합니다.",
}
PUBLISH_MODES = ("branch", "pr", "merge")


def _writes(rig: Any) -> bool:
    return any({"write_file", "edit_file"} & set(h.tools) for h in [*rig.hands.values(), rig.foreman] if h)


def _approver(rig: Any) -> str | None:
    """The hand that should approve a PR before it's merged: one the foreman must call, else a
    read-only hand called like a reviewer."""
    if rig.foreman and rig.foreman.require:
        return rig.foreman.require[0]
    for name in ("reviewer", "checker", "review"):
        if name in rig.hands:
            return name
    return None


# The page's icon: a foreman joined to two hands (src/rig/favicon.svg, and .ico for older browsers).
ICONS = {"/favicon.ico": ("favicon.ico", "image/x-icon"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}


def _workspaces(summary: dict[str, Any]) -> list[dict[str, str]]:
    """The project folders a shift worked on (shift.json / running.json `workspaces`); [] for older shifts."""
    recorded = summary.get("workspaces")
    if not isinstance(recorded, list):
        return []
    return [{"name": w.get("name") if isinstance(w.get("name"), str) else "", "dir": w["dir"]}
            for w in recorded if isinstance(w, dict) and isinstance(w.get("dir"), str)]


def _committed(summary: dict[str, Any]) -> bool:
    """Whether shift.json records a committed worktree branch (what `load_resume` continues from)."""
    entries = summary.get("worktrees")
    return isinstance(entries, list) and any(
        isinstance(w, dict) and w.get("changed") and w.get("commit") for w in entries)


DIR_LIMIT = 500


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _roots() -> list[str]:
    """Drive letters on Windows ("C:/", "D:/", ...); "/" elsewhere."""
    if os.name != "nt":
        return ["/"]
    import string

    return [f"{c}:/" for c in string.ascii_uppercase if os.path.isdir(f"{c}:/")]


def _prs(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """The shift's pull requests from shift.json: number, url and merged / closed / open."""
    out = []
    for pr in summary.get("prs") or []:
        # Only a PR link: it's shown as a link and passed to `gh pr view`.
        if isinstance(pr, dict) and isinstance(pr.get("url"), str) and PR_URL.fullmatch(pr["url"]):
            state = "merged" if pr.get("merged") else "closed" if pr.get("closed") else "open"
            out.append({"number": pr.get("number"), "url": pr["url"], "state": state})
    return out


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
                if url.path in ICONS:
                    name, ctype = ICONS[url.path]
                    return self._send(200, resources.files("rig").joinpath(name).read_bytes(), ctype)
                if url.path == "/api/dirs":
                    return self._json(200, app.dirs(parse_qs(url.query).get("path", [""])[0]))
                if url.path == "/api/workers":
                    from rig.claude_code import find_claude

                    return self._json(200, {"claude_code": find_claude() is not None})
                if url.path == "/api/rigs":
                    return self._json(200, app.rigs())
                if url.path == "/api/run":
                    q = parse_qs(url.query)
                    return self._json(200, app.run_state(int(q.get("since", ["0"])[0]), q.get("file", [""])[0] or None))
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
                    app.stop(body.get("file") or None)
                    return self._json(200, {"ok": True})
                if self.path == "/api/rig":
                    return self._json(200, {"file": app.create_rig(body.get("title") or "", body.get("kind") or "",
                                                                   body.get("workspaces") or [])})
                if self.path == "/api/publish":
                    app.set_publish(body["file"], body.get("mode") or "")
                    return self._json(200, {"ok": True})
                if self.path == "/api/init-fix":
                    return self._json(200, {"file": app.init_fix(body["workspaces"])})
                if self.path == "/api/run":
                    limit = body.get("max_cost")
                    run = app.start(body["file"], body.get("task", ""), body.get("inputs") or {},
                                    bool(body.get("dry")), bool(body.get("worktree")),
                                    float(limit) if limit not in (None, "") else None, body.get("resume") or None,
                                    worker=body.get("worker") or "api")
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
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="any">
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
.rig-warn{color:var(--warn);overflow-wrap:anywhere}
.tag{display:inline-block;font-size:11px;padding:0 7px;border-radius:999px;border:1px solid currentColor;margin-left:6px;vertical-align:1px}
.tag.read{color:var(--ok)}.tag.write{color:var(--warn)}
.ws-rows{display:grid;gap:6px}
.ws-row{display:grid;grid-template-columns:110px minmax(0,1fr) auto 22px 30px;gap:6px;align-items:center}
.pick-btn{font:inherit;font-size:13px;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:6px;height:32px;padding:0 10px;cursor:pointer;white-space:nowrap}
dialog#picker{border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--fg);padding:18px 20px;width:min(560px,calc(100vw - 32px))}
dialog#picker::backdrop{background:rgba(0,0,0,.35)}
#pick-path{font-family:var(--mono);font-size:13px;word-break:break-all;background:var(--code);border-radius:6px;padding:6px 8px;margin:0}
#pick-list{list-style:none;margin:0;padding:0;max-height:320px;overflow:auto;border:1px solid var(--line);border-radius:6px}
#pick-list button{all:unset;box-sizing:border-box;display:block;width:100%;padding:7px 10px;cursor:pointer;font-size:14px}
#pick-list button:hover,#pick-list button:focus-visible{background:var(--soft)}
#pick-list li+li{border-top:1px solid var(--line)}
.picker-body{display:grid;gap:10px}
#pick-error:empty{display:none}
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
#others[hidden]{display:none}#new-box[hidden],#publish-box[hidden]{display:none}#new-open{margin-top:10px}
.kind{border:0;padding:0;margin:0;display:grid;gap:4px}.kind legend{font-size:14px;padding:0;margin-bottom:4px}
#publish-mode{max-width:100%;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:inherit;font:inherit}#others{margin:0 0 10px}
.notice{border:1px solid var(--accent);background:var(--soft);border-radius:8px;padding:12px 14px;display:grid;gap:8px;font-size:14px}
.notice[hidden]{display:none}.notice p{margin:0}
.cost-field input{max-width:160px}.cost-field[hidden]{display:none}
.worker{border:0;padding:0;margin:0 0 10px}
.worker legend{font-size:14px;padding:0;margin-bottom:4px}
.cost{font-variant-numeric:tabular-nums;font-size:13px;font-weight:600}
button.danger{font:inherit;font-size:14px;border-radius:6px;padding:7px 16px;cursor:pointer;border:1px solid var(--bad);background:transparent;color:var(--bad)}
.error{color:var(--bad);font-size:14px;white-space:pre-wrap}
.log{font:12.5px/1.55 var(--mono);background:var(--code);border-radius:6px;padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}
.pill{font-size:12px;font-weight:600;padding:1px 9px;border-radius:999px;border:1px solid currentColor}
.pill.running{color:var(--warn)}
a.pr.merged{color:var(--ok)}a.pr.closed{color:var(--muted)}a.pr.open{color:var(--warn)}.pill.done,.pill.ok{color:var(--ok)}.pill.incomplete,.pill.failed,.pill.stopped,.pill.error{color:var(--bad)}.pill.unreadable{color:var(--muted)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#viewer[hidden]{display:none}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
.change{font-weight:600}
td.id{font-family:var(--mono);font-size:12.5px;white-space:nowrap}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.table-wrap{overflow-x:auto}
header.row h1{margin:0}
.tabs{display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:8px}
.tab{font:inherit;font-size:13px;display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:999px;padding:4px 12px 4px 6px;cursor:pointer;max-width:100%}
.tab:first-child{padding-left:12px}
.tab[aria-selected=true]{border-color:var(--accent);background:var(--soft);font-weight:600}
.tab .count{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.tab .dot{width:7px;height:7px;border-radius:50%;background:var(--warn)}
.badge{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:6px;color:#fff;font-size:11px;font-weight:700;flex:none}
button.resume{padding:2px 10px;font-size:13px;white-space:nowrap}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
</head>
<body>
<main>
  <header class="row"><img src="/favicon.svg" alt="" width="28" height="28"><h1>rig</h1></header>
  <div class="grid">
    <section class="panel" aria-labelledby="rigs-h">
      <h2 id="rigs-h">1. 설정 선택</h2>
      <div class="rigs" id="rigs"><p class="muted small">불러오는 중…</p></div>
      <button class="secondary" id="new-open" type="button">+ 설정 추가</button>
      <div class="notice" id="new-box" hidden>
        <label class="field" for="new-title">설정 이름
          <input type="text" id="new-title" maxlength="60" placeholder="예: 주문 화면 리뷰, dworks 기능 개발">
        </label>
        <fieldset class="opts kind" id="new-kind">
          <legend>종류</legend>
          <label><input type="radio" name="new-kind" value="review" checked> 읽기 전용 · 코드를 읽고 리뷰만 함 (파일을 바꾸지 않음)</label>
          <label><input type="radio" name="new-kind" value="edit"> 코드 수정 · 계획 → 수정 → 리뷰, 결과는 폴더(또는 별도 브랜치)에 남김</label>
          <label><input type="radio" name="new-kind" value="merge"> 코드 수정 + PR 자동 병합 · 리뷰어가 승인하고 CI가 통과하면 병합</label>
        </fieldset>
        <div class="ws-row">
          <input type="text" id="new-path" aria-label="프로젝트 폴더" placeholder="프로젝트 폴더 (예: D:/05_project/...)" spellcheck="false">
          <button type="button" class="pick-btn" id="new-pick">폴더 선택</button>
        </div>
        <div class="row">
          <button class="primary" id="new-create" type="button">만들기</button>
          <button class="secondary" id="new-cancel" type="button">취소</button>
          <span class="error" id="new-error"></span>
        </div>
      </div>
      <div id="ws-box" hidden>
        <label class="field" for="publish-mode" id="publish-box" hidden>작업 결과 처리
          <select id="publish-mode">
            <option value="branch">폴더(또는 별도 브랜치)에만 남김</option>
            <option value="pr">PR만 열기 (병합은 직접)</option>
            <option value="merge">PR 열고 리뷰 승인 + CI 통과 시 자동 병합</option>
          </select>
          <span class="small muted" id="publish-status"></span>
        </label>
        <p class="field" id="ws-label">작업할 프로젝트 폴더 (workspace)</p>
        <div class="ws-rows" id="ws-rows" role="group" aria-labelledby="ws-label"></div>
        <div class="row">
          <button class="secondary" id="ws-add" type="button">+ 프로젝트 추가</button>
          <button class="primary" id="ws-save" type="button">저장</button>
        </div>
        <p class="small muted" id="ws-hint"><b>폴더 선택</b>으로 경로를 고른 뒤 저장하세요. 프로젝트가 2개 이상이면 각각 이름을 붙이세요. hand들은 <code>이름/경로</code>로 파일을 봅니다 (예: <code>backend/src/...</code>).</p>
        <p class="small" id="ws-status"></p>
      </div>
    </section>
    <section class="panel" aria-labelledby="run-h">
      <h2 id="run-h">2. 요구사항 입력 후 실행</h2>
      <p class="small muted" id="others" hidden></p>
      <div class="notice" id="fix-box" tabindex="-1" hidden>
        <p id="fix-msg"></p>
        <div class="row" id="fix-actions"></div>
      </div>
      <label class="field" for="task">요구사항
        <textarea id="task" placeholder="예: 이 프로젝트를 리뷰해줘 / 주문 목록에 기간 검색 조건을 추가해줘"></textarea>
      </label>
      <div id="inputs"></div>
      <fieldset class="opts worker" id="worker-box">
        <legend>실행 방식</legend>
        <label><input type="radio" name="worker" value="api" checked> API 키 (쓴 만큼 API 크레딧 차감)</label>
        <label><input type="radio" name="worker" value="claude-code" id="worker-cc"> Claude Code 구독 (설치된 claude 사용, API 크레딧 안 씀)</label>
        <span class="small muted" id="worker-note"></span>
      </fieldset>
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
    <div class="tabs" role="tablist" aria-label="프로젝트별 실행" id="hist-tabs"></div>
    <div class="table-wrap"><table>
      <thead><tr><th>실행</th><th>설정</th><th>요구사항</th><th>상태</th><th>PR</th><th class="num">발견</th><th class="num">비용</th><th></th></tr></thead>
      <tbody id="shifts"></tbody>
    </table></div>
    <div class="row"><p class="small muted" style="margin:0">터미널에서 <code>rig run</code>으로 시작한 실행도 새로고침 없이 여기 나타나고, 아래 로그 창에 자동으로 열립니다. 끝나면 탭 제목에 표시됩니다.</p>
      <button class="secondary" id="notify" type="button" hidden>끝나면 알림 받기</button></div>
  </section>
  <section class="panel" id="viewer" aria-labelledby="viewer-h" hidden>
    <div class="row"><h2 id="viewer-h">실행 로그</h2><span class="pill" id="v-pill" role="status"></span><span class="small muted" id="v-reason"></span>
      <span id="v-pr"></span>
      <a id="v-report" target="_blank">결과 리포트 열기 →</a>
      <button class="secondary" id="v-close" type="button">닫기</button></div>
    <pre class="log" id="v-log" aria-live="polite"></pre>
  </section>
</main>
<dialog id="picker" aria-labelledby="pick-h">
  <div class="picker-body">
    <h2 id="pick-h">프로젝트 폴더 선택</h2>
    <p id="pick-path"></p>
    <div class="row" id="pick-nav"></div>
    <ul id="pick-list" aria-label="하위 폴더"></ul>
    <p class="error" id="pick-error"></p>
    <div class="row">
      <button class="primary" id="pick-ok" type="button">이 폴더 선택</button>
      <button class="secondary" id="pick-cancel" type="button">취소</button>
    </div>
  </div>
</dialog>
<script>
const $ = id => document.getElementById(id);
let rigs = [], selected = null, since = 0, polling = null;
try { selected = localStorage.getItem('rig.selected'); } catch (e) {}
let histTab = 'all';  // 'all', or a project key (its sorted folder list)
try { histTab = localStorage.getItem('rig.histTab') || 'all'; } catch (e) {}

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
  if (!rigs.length) { box.append(el('p', {class: 'muted small'}, '이 폴더에 rig 설정 파일(*.yaml)이 없습니다. 아래 + 설정 추가로 만드세요.')); renderShifts(); return; }
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
          (w.ok ? '✓ ' : '✗ 폴더 없음: ') + (w.name ? w.name + ' · ' : '') + w.path)),
        ...(r.warnings || []).map(w => el('span', {class: 'small rig-warn'}, w)));
    }
    box.append(b);
  }
  renderSelected();
  renderShifts();  // the history filter follows the selected rig
}
function select(file) {
  if (resume && file !== resume.file) endResume();
  const changed = file !== selected;
  selected = file; try { localStorage.setItem('rig.selected', file); } catch (e) {}
  if (changed) showRunOf(file);
  return loadRigs();
}
// Section 2 shows the selected rig's run: start over with its log.
function showRunOf(file) {
  since = 0; ownShift = null; ownRunning = false;
  $('log').textContent = ''; $('progress').hidden = true; $('go').disabled = false; $('stop').hidden = true;
  $('run-error').textContent = '';
  poll();
}
// Other rig files running now, as links that select them.
function renderOthers(others) {
  const box = $('others'); box.textContent = '';
  box.hidden = !others || !others.length;
  if (box.hidden) return;
  box.append('다른 설정 실행 중: ');
  others.forEach((f, i) => {
    if (i) box.append(', ');
    box.append(el('a', {href: '#', onclick: e => { e.preventDefault(); select(f); }}, f));
  });
}
function sameFolders(a, b) {
  const key = ws => JSON.stringify(ws.map(w => [w.name || '', w.dir || w.path]).sort());
  return key(a) === key(b);
}
function renderFix(r) {
  const box = $('fix-box'), actions = $('fix-actions');
  if (resume) { renderResume(); return; }
  if (!fix) { box.hidden = true; return; }
  box.hidden = false; actions.textContent = '';
  const writer = r && !r.error && r.writes;
  const button = (label, onclick) => actions.append(el('button', {type: 'button', class: 'secondary', onclick}, label));
  if (!rigs.some(x => x.writes && !x.error)) {
    $('fix-msg').textContent = '리뷰에서 수정 요청을 가져왔습니다. 코드를 고칠 수 있는 설정이 아직 없어서 먼저 만들어야 합니다.'
      + (fix.ambiguous ? ' 리뷰한 프로젝트 폴더를 알 수 없어서 자동으로 만들 수는 없습니다.' : '');
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
  if (fix.ambiguous) {
    $('fix-msg').textContent = '리뷰에서 수정 요청을 가져왔지만, 리뷰한 프로젝트 폴더를 알 수 없습니다 (같은 이름의 설정 파일이 여러 개이고 폴더가 서로 다릅니다). 왼쪽에서 선택한 설정의 프로젝트 폴더가 리뷰한 프로젝트와 같은지 확인한 뒤 실행하세요.';
    button('닫기', () => { fix = null; renderFix(r); });
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
// Resume mode: continue a stopped run from the history table (like rig run --resume). The old
// task and inputs are used as they were, and the worktree is always on.
function renderResume() {
  const box = $('fix-box'), actions = $('fix-actions');
  box.hidden = false; actions.textContent = '';
  $('fix-msg').textContent = `중단된 실행 ${resume.id}(${LABEL[resume.status] || resume.status}${resume.why ? ' · ' + resume.why : ''})에 이어서 실행합니다. `
    + '그 실행의 브랜치 끝에서 새 실행으로 이어 가며, 요구사항과 입력값은 그때 것을 그대로 쓰고 별도 브랜치(worktree)는 항상 켜집니다.'
    + (resume.reason === 'budget' ? ' 비용 상한 때문에 멈췄으니 아래에서 상한을 올린 뒤 실행하세요.' : '');
  actions.append(el('button', {type: 'button', class: 'secondary', onclick: () => { endResume(); $('task').focus(); }}, '이어 하기 취소'));
  $('task').value = resume.task; $('task').readOnly = true;
  $('inputs').hidden = true;
  $('worktree').checked = true; $('worktree').disabled = true;
  $('go').textContent = '이어서 실행';
}
async function startResume(s) {
  // Clicking another run's button while in resume mode keeps what was typed before the first one.
  const saved = resume ? resume.saved : {task: $('task').value, worktree: $('worktree').checked};
  resume = {id: s.id, file: s.resume_file, task: s.resume_task, status: s.status, why: reasonText(s), reason: s.reason, saved};
  fix = null;
  await select(resume.file).catch(e => { $('run-error').textContent = e.message; });
  $('run-h').scrollIntoView({behavior: 'smooth'});
  $('fix-box').focus({preventScroll: true});
}
function endResume() {
  if (!resume) return;
  const saved = resume.saved;
  resume = null;
  $('task').value = saved.task; $('task').readOnly = false;
  $('worktree').checked = saved.worktree; $('worktree').disabled = false;
  $('inputs').hidden = false;
  $('go').textContent = '실행';
  renderFix(rigs.find(x => x.file === selected));
}
function renderSelected() {
  const r = rigs.find(x => x.file === selected);
  renderFix(r);
  $('ws-box').hidden = !r || !!r.error;
  const box = $('inputs'); box.textContent = '';
  if (!r || r.error) return;
  $('max-cost').value = r.max_cost_usd ?? '';
  // Code-changing rigs: what happens to the result (publish.pr / auto_merge).
  $('publish-box').hidden = !r.writes;
  if (r.writes) {
    const pub = r.publish || {};
    $('publish-mode').value = pub.auto_merge ? 'merge' : pub.pr ? 'pr' : 'branch';
    $('publish-mode').querySelector('option[value=merge]').disabled = !pub.can_merge && !pub.auto_merge;
    $('publish-status').textContent = pub.can_merge ? '' : '자동 병합은 리뷰하는 hand(예: reviewer)가 있어야 합니다.';
  }
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
    el('button', {type: 'button', class: 'pick-btn', onclick: () => pickFolder(row.querySelector('.ws-path'))}, '폴더 선택'),
    el('span', {class: 'mark ' + (w.ok ? 'status-ok' : 'status-bad')}, mark),
    el('button', {type: 'button', class: 'icon-btn', 'aria-label': '이 프로젝트 빼기', onclick: () => row.remove()}, '×'));
  row.querySelector('.ws-name').value = w.name; row.querySelector('.ws-path').value = w.path;
  $('ws-rows').append(row);
}
$('ws-add').addEventListener('click', () => addRow());

// Folder picker: browse this computer's folders through the server (it runs here) and fill the path.
let pickTarget = null, pickAt = '';
async function pickFolder(input) {
  pickTarget = input;
  $('picker').showModal();
  await pickShow(input.value.trim()).catch(() => pickShow(''));
}
async function pickShow(path) {
  $('pick-error').textContent = '';
  let d;
  try { d = await api('/api/dirs?path=' + encodeURIComponent(path)); }
  catch (e) { $('pick-error').textContent = e.message; throw e; }
  pickAt = d.path;
  $('pick-path').textContent = d.path;
  const nav = $('pick-nav'); nav.textContent = '';
  if (d.parent) nav.append(el('button', {type: 'button', class: 'secondary', onclick: () => pickShow(d.parent)}, '↑ 상위 폴더'));
  for (const r of d.roots) nav.append(el('button', {type: 'button', class: 'secondary', onclick: () => pickShow(r)}, r));
  const list = $('pick-list'); list.textContent = '';
  if (!d.dirs.length) list.append(el('li', {class: 'muted small', style: 'padding:7px 10px'}, '하위 폴더가 없습니다.'));
  for (const name of d.dirs) {
    const sep = d.path.endsWith('/') ? '' : '/';
    list.append(el('li', {}, el('button', {type: 'button', onclick: () => pickShow(d.path + sep + name)}, '📁 ' + name)));
  }
  if (d.more) list.append(el('li', {class: 'muted small', style: 'padding:7px 10px'}, '폴더가 많아 일부만 보입니다.'));
}
$('pick-ok').addEventListener('click', () => { if (pickTarget && pickAt) pickTarget.value = pickAt; $('picker').close(); });
$('pick-cancel').addEventListener('click', () => $('picker').close());
$('publish-mode').addEventListener('change', async () => {
  try { await api('/api/publish', {file: selected, mode: $('publish-mode').value}); $('publish-status').textContent = '✓ 저장했습니다.'; await loadRigs(); }
  catch (e) { $('publish-status').textContent = e.message; await loadRigs(); }
});
// New setting: a bundled template with a name, a kind and a project folder.
$('new-open').addEventListener('click', () => { $('new-box').hidden = false; $('new-open').hidden = true; $('new-title').focus(); });
$('new-cancel').addEventListener('click', () => { $('new-box').hidden = true; $('new-open').hidden = false; $('new-error').textContent = ''; });
$('new-pick').addEventListener('click', () => pickFolder($('new-path')));
$('new-create').addEventListener('click', async () => {
  $('new-error').textContent = '';
  const kind = document.querySelector('input[name=new-kind]:checked').value;
  try {
    const r = await api('/api/rig', {title: $('new-title').value, kind, workspaces: [{name: '', path: $('new-path').value}]});
    $('new-box').hidden = true; $('new-open').hidden = false; $('new-title').value = ''; $('new-path').value = '';
    await select(r.file);
  } catch (e) { $('new-error').textContent = e.message; }
});
$('ws-save').addEventListener('click', async () => {
  const workspaces = [...document.querySelectorAll('.ws-row')].map(r => ({name: r.querySelector('.ws-name').value, path: r.querySelector('.ws-path').value}));
  try { await api('/api/workspaces', {file: selected, workspaces}); await loadRigs(); }
  catch (e) { $('ws-status').textContent = e.message; $('ws-status').className = 'small status-bad'; }
});

$('go').addEventListener('click', async () => {
  $('run-error').textContent = '';
  const inputs = {};
  for (const i of document.querySelectorAll('#inputs input')) inputs[i.dataset.name] = i.value;
  // Resuming sends only the old run's id: the server takes its task and inputs and turns the worktree on.
  const worker = workerChoice();
  const max_cost = worker === 'claude-code' ? '' : $('max-cost').value;  // no per-call billing to limit
  const body = resume ? {file: selected, resume: resume.id, dry: $('dry').checked, max_cost, worker}
    : {file: selected, task: $('task').value, inputs, dry: $('dry').checked, worktree: $('worktree').checked,
       max_cost, worker};
  try {
    await api('/api/run', body);
    endResume();
    since = 0; $('log').textContent = ''; poll();
  } catch (e) { $('run-error').textContent = e.message; }
});

$('stop').addEventListener('click', async () => {
  try { await api('/api/stop', {file: selected}); poll(); } catch (e) { $('run-error').textContent = e.message; }
});

const LABEL = {running: '실행 중', done: '완료', stopped: '중지됨', incomplete: '미완료', failed: '실패',
  ok: '완료', error: '오류', unreadable: '결과 불명'};
// A shift's short why (from the server's status reason): the error, the kind of stop, or why shift.json can't be read.
const STOP = {budget: '비용 상한 도달', user: '사용자가 중지'};
const reasonText = s => s.status === 'stopped' ? (STOP[s.reason] || '') : (s.reason || '');
async function poll() {
  clearTimeout(polling);
  const file = selected;
  const s = await api('/api/run?file=' + encodeURIComponent(file || '') + '&since=' + since).catch(() => null);
  if (!s || file !== selected) return;  // the selection changed meanwhile: showRunOf polls again
  renderOthers(s.others);
  if (s.status === 'idle') {
    // Nothing of this rig yet; look again while others run (they may finish and free the folder).
    if (s.others && s.others.length) polling = setTimeout(poll, 3000);
    return;
  }
  $('progress').hidden = false;
  $('pill').className = 'pill ' + s.status; $('pill').textContent = LABEL[s.status] || s.status;
  $('run-title').textContent = s.file + ' · ' + s.task.split('\n')[0].slice(0, 80) + (s.dry ? ' (dry)' : '')
    + (s.worker === 'claude-code' && !s.dry ? ' · Claude Code 구독' : '');
  if (s.lines.length) { const log = $('log'); log.textContent += s.lines.join('\n') + '\n'; log.scrollTop = log.scrollHeight; since = s.total; }
  const link = $('report-link');
  link.hidden = !(s.shift_id && s.status !== 'running');
  if (s.shift_id) link.href = '/shifts/' + encodeURIComponent(s.shift_id) + '/report';
  $('go').disabled = s.status === 'running';
  $('stop').hidden = s.status !== 'running';
  $('stop').disabled = !!(s.cost && s.cost.stopping);
  if (s.worker === 'claude-code' && !s.dry) {
    $('run-cost').textContent = 'API 크레딧 안 씀' + (s.cost && s.cost.stopping ? (s.status === 'running' ? ' · 중지 중' : ' · 중지됨') : '');
  } else if (s.cost) {
    const c = s.cost;
    $('run-cost').textContent = '비용 ' + (c.unpriced ? '≥' : '') + c.spent + (c.limit ? ' / 상한 ' + c.limit : '')
      + (c.stopping === 'budget' ? ' · 상한 도달' : c.stopping ? (s.status === 'running' ? ' · 중지 중' : ' · 중지됨') : '');
  }
  if (s.shift_id) ownShift = s.shift_id;
  if (ownRunning && s.status !== 'running') finished(s.shift_id || '', s.status === 'done' ? 'done' : s.status);
  if (ownRunning !== (s.status === 'running')) { ownRunning = s.status === 'running'; renderShifts(); }  // the resume buttons
  if (s.status === 'running') polling = setTimeout(poll, 1000); else loadShifts();
}
let ownShift = null, ownRunning = false;

let shiftRows = null;  // the last /api/shifts result; the filter re-renders from it
async function loadShifts() {
  shiftRows = await api('/api/shifts');
  renderShifts();
  follow(shiftRows);
}
// A folder's last path segment, for a project without a name.
const baseName = dir => dir.split(/[\\/]/).filter(Boolean).pop() || dir;
const dirSet = ws => JSON.stringify([...new Set(ws.map(w => w.dir))].sort());
const RESUME_PROBLEM = {no_rig: '이어 하기 불가 · 같은 이름의 설정 파일이 없음', many_rigs: '이어 하기 불가 · 같은 이름의 설정 파일이 여러 개'};
function renderShifts() {
  if (!shiftRows) return;
  const body = $('shifts'); body.textContent = '';
  const empty = text => body.append(el('tr', {}, el('td', {colspan: '8', class: 'muted small'}, text)));
  renderTabs();
  if (!shiftRows.length) { empty('아직 실행 기록이 없습니다.'); return; }
  const rows = histTab === 'all' ? shiftRows : shiftRows.filter(s => projectKey(s) === histTab);
  if (!rows.length) { empty('이 프로젝트의 실행 기록이 없습니다.'); return; }
  for (const s of rows) {
    const state = el('td', {}, LABEL[s.status] || s.status);
    const why = reasonText(s);
    if (why) state.append(el('div', {class: 'small muted', title: why}, why));
    const links = el('span', {class: 'row'});
    if (s.log) links.append(el('a', {href: '#viewer', onclick: e => { e.preventDefault(); openLog(s.id); }}, '로그'));
    if (!s.running) links.append(el('a', {href: '/shifts/' + encodeURIComponent(s.id) + '/report', target: '_blank'}, '리포트'));
    if (s.resumable && s.resume_file) {
      const b = el('button', {type: 'button', class: 'secondary resume', 'aria-label': `실행 ${s.id} 이어 하기`, onclick: () => startResume(s)}, '이어 하기');
      if (ownRunning) { b.disabled = true; b.title = '실행 중에는 이어 할 수 없습니다'; }
      links.append(b);
    } else if (s.resume_problem) {
      links.append(el('span', {class: 'small muted'}, RESUME_PROBLEM[s.resume_problem] || ''));
    }
    const setting = el('td', {}, s.rig);
    if (s.workspaces.length) {
      setting.append(el('div', {class: 'small muted', title: s.workspaces.map(w => w.dir).join('\n')},
        s.workspaces.map(w => w.name || baseName(w.dir)).join(' · ')));
    }
    body.append(el('tr', {},
      el('td', {class: 'id'}, s.id), setting, taskCell(s),
      state, el('td', {}, prLinks(s.prs)), el('td', {class: 'num'}, String(s.findings || '')), el('td', {class: 'num'}, s.cost), el('td', {}, links)));
  }
}
// What a run changed (its TITLE, the PR's title) in bold, with the request under it; the
// request alone while it runs or when the run named no change.
function taskCell(s) {
  if (!s.title) return el('td', {}, s.task);
  return el('td', {}, el('div', {class: 'change'}, s.title), el('div', {class: 'small muted'}, s.task));
}
// One tab per project (the set of folders a run worked on), newest first, plus 전체.
// Runs from before folders were recorded go under 기타.
const projectKey = s => s.workspaces.length ? dirSet(s.workspaces) : 'other';
const projectLabel = s => s.workspaces.length ? s.workspaces.map(w => w.name || baseName(w.dir)).join(' · ') : '기타';
// A small colored badge with the project's first letter, the same color every time.
function badge(label) {
  let h = 0;
  for (const ch of label) h = (h * 31 + ch.codePointAt(0)) % 360;
  return el('span', {class: 'badge', style: `background:hsl(${h} 45% 42%)`, 'aria-hidden': 'true'}, [...label][0].toUpperCase());
}
function renderTabs() {
  const tabs = new Map([['all', {label: '전체', count: 0, running: false}]]);
  for (const s of shiftRows || []) {
    const key = projectKey(s);
    if (!tabs.has(key)) tabs.set(key, {label: projectLabel(s), count: 0, running: false, title: s.workspaces.map(w => w.dir).join('\n')});
    for (const k of ['all', key]) { tabs.get(k).count++; tabs.get(k).running ||= s.running; }
  }
  if (!tabs.has(histTab)) histTab = 'all';
  const box = $('hist-tabs'); box.textContent = '';
  for (const [key, t] of tabs) {
    const b = el('button', {type: 'button', role: 'tab', class: 'tab', 'aria-selected': String(key === histTab),
                            tabindex: key === histTab ? '0' : '-1', title: t.title || '',
                            onclick: () => { histTab = key; try { localStorage.setItem('rig.histTab', key); } catch (e) {} renderShifts(); }});
    if (key !== 'all') b.append(badge(t.label));
    b.append(t.label, el('span', {class: 'count'}, String(t.count)));
    if (t.running) b.append(el('span', {class: 'dot', title: '실행 중'}));
    box.append(b);
  }
  return tabs;
}
// Arrow keys move between tabs.
$('hist-tabs').addEventListener('keydown', e => {
  if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
  const all = [...$('hist-tabs').querySelectorAll('.tab')];
  const i = all.indexOf(document.activeElement);
  const next = all[(i + (e.key === 'ArrowRight' ? 1 : -1) + all.length) % all.length];
  if (next) { next.click(); $('hist-tabs').querySelectorAll('.tab')[all.indexOf(next)].focus(); }
});
// "#56 병합됨" links to each of a shift's PRs.
const PR_STATE = {merged: '병합됨', closed: '닫힘', open: '열림'};
function prLinks(prs) {
  const box = el('span', {class: 'row'});
  for (const p of prs || []) {
    box.append(el('a', {href: p.url, target: '_blank', rel: 'noopener', class: 'pr ' + p.state},
      (p.number ? '#' + p.number : 'PR') + ' ' + (PR_STATE[p.state] || '')));
  }
  return box;
}
// While a shift runs, shift.json has no PR yet; take it from the "PR opened/updated: <url>" log line.
function prsFromLog(text) {
  const out = [];
  for (const m of text.matchAll(/PR (?:opened|updated): (\S+\/pull\/(\d+))/g)) out.push({url: m[1], number: Number(m[2]), state: 'open'});
  return out;
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
  viewing = id; vSince = 0; vRunning = false; $('v-log').textContent = ''; $('v-pr').textContent = '';
  const row = (shiftRows || []).find(r => r.id === id);
  $('viewer-h').textContent = '실행 로그 · ' + id + (row && (row.title || row.task) ? ' · ' + (row.title || row.task) : '');
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
  const state = s.status;
  $('v-pill').className = 'pill ' + state; $('v-pill').textContent = LABEL[state] || state;
  $('v-reason').textContent = reasonText(s);
  $('v-report').hidden = s.running;
  const prs = s.prs && s.prs.length ? s.prs : prsFromLog($('v-log').textContent);
  $('v-pr').replaceChildren(prLinks(prs));
  if (vRunning && !s.running) finished(id, state, reasonText(s));
  vRunning = s.running;
  if (s.running) vTimer = setTimeout(pollLog, 1000); else loadShifts();
}

// When a run ends: the tab title says so, and a desktop notification if allowed.
const TITLE = document.title;
function finished(id, state, reason = '') {
  const what = (state === 'ok' || state === 'done' ? '✓ ' : '✗ ') + LABEL[state];
  document.title = what + ' · ' + TITLE;
  if (window.Notification && Notification.permission === 'granted') {
    try { new Notification('rig 실행 ' + LABEL[state], {body: id + (reason ? ' · ' + reason : '')}); } catch (e) {}
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

// Worker: the API, or the local Claude Code on the user's subscription. Remembered per browser.
const workerChoice = () => document.querySelector('input[name=worker]:checked').value;
function workerChanged() {
  const cc = workerChoice() === 'claude-code';
  // The subscription isn't billed per call, so the cost limit doesn't apply there.
  document.querySelector('.cost-field').hidden = cc;
  try { localStorage.setItem('rig.worker', workerChoice()); } catch (e) {}
}
for (const r of document.querySelectorAll('input[name=worker]')) r.addEventListener('change', workerChanged);
async function loadWorkers() {
  let saved = null;
  try { saved = localStorage.getItem('rig.worker'); } catch (e) {}
  const w = await api('/api/workers').catch(() => ({claude_code: false}));
  $('worker-cc').disabled = !w.claude_code;
  $('worker-note').textContent = w.claude_code ? ''
    : 'Claude Code(claude)가 설치돼 있지 않아 구독 방식을 쓸 수 없습니다. 설치 후 터미널에서 claude → /login 으로 로그인하세요.';
  const pick = document.querySelector(`input[name=worker][value="${saved === 'claude-code' && w.claude_code ? 'claude-code' : 'api'}"]`);
  pick.checked = true;
  workerChanged();
}

let fix = null, resume = null;  // resume: the stopped run being continued (startResume), or null
async function start() {
  const params = new URLSearchParams(location.search);
  if (params.has('fix')) {
    try {
      const f = await api('/api/finding?shift=' + encodeURIComponent(params.get('fix')) + '&n=' + encodeURIComponent(params.get('n')));
      fix = {workspaces: f.workspaces, ambiguous: !!f.ambiguous, pick: true};
      $('task').value = f.task;
      $('worktree').checked = true;
    } catch (e) { $('run-error').textContent = e.message; }
    history.replaceState(null, '', '/');  // a reload shouldn't overwrite edits to the task
  }
  await loadRigs().catch(e => { $('rigs').textContent = e.message; });
}
start();
loadWorkers();
loadShifts();
poll();
</script>
</body>
</html>
"""
