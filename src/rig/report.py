"""`rig report`: one self-contained HTML page for a shift, to open in a browser.

Hands whose output is JSON with a `findings` list (see `output.schema`) become rows in one
filterable table; every hand's full output follows, rendered from Markdown.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from rig.cost import usd

SEVERITIES = ["critical", "high", "medium", "low", "info"]


def findings_of(output: str) -> list[dict[str, Any]] | None:
    """The `findings` list of a JSON output, or None if the output isn't findings JSON."""
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return [f for f in data["findings"] if isinstance(f, dict)]
    return None


def collect(shift_dir: Path) -> dict[str, Any]:
    """Everything the page shows, read from a shift directory."""
    summary = json.loads((shift_dir / "shift.json").read_text(encoding="utf-8")) if (shift_dir / "shift.json").exists() else {}
    keys = list(summary.get("hands", {}))
    # Hands logged without a shift.json entry (a crashed shift) still get shown.
    for f in sorted(shift_dir.glob("*.md")):
        if not any(k.replace("#", "-") == f.stem for k in keys):
            keys.append(f.stem)
    hands = []
    rows = []
    for key in keys:
        path = shift_dir / f"{key.replace('#', '-')}.md"
        output = path.read_text(encoding="utf-8") if path.exists() else ""
        found = findings_of(output)
        meta = summary.get("hands", {}).get(key, {})
        hands.append({"key": key, "output": output, "findings": found, "meta": meta})
        for f in found or []:
            rows.append({**f, "area": key.split("#")[0]})
    rows.sort(key=lambda r: (_rank(r.get("severity")), str(r.get("file", "")), _line(r.get("line"))))
    # The final word: the foreman's reply, or the last hand to finish in lines mode.
    final = "foreman" if "foreman" in keys else (keys[-1] if keys else None)
    return {"id": shift_dir.name, "summary": summary, "hands": hands, "findings": rows, "final": final}


def _rank(severity: Any) -> int:
    s = str(severity or "").lower()
    return SEVERITIES.index(s) if s in SEVERITIES else len(SEVERITIES)


def _line(line: Any) -> int:
    try:
        return int(line)
    except (TypeError, ValueError):
        return 0


# --- Markdown ------------------------------------------------------------------------------------

def markdown(text: str) -> str:
    """Enough Markdown for hand outputs: headings, lists, fenced code, paragraphs, inline code, bold."""
    out: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(body))}</code></pre>")
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:
            level = min(len(m.group(1)) + 2, 6)  # page headings own h1-h2
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"\s*([-*]|\d+\.)\s+", line):
            ordered = bool(re.match(r"\s*\d+\.", line))
            items = []
            while i < len(lines) and re.match(r"\s*([-*]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"\s*([-*]|\d+\.)\s+", "", lines[i], count=1))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(it)}</li>" for it in items) + f"</{tag}>")
            continue
        if not line.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(```|#{1,6}\s|\s*([-*]|\d+\.)\s+)", lines[i]):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def _inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    rendered = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            rendered.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            rendered.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(part)))
    return "".join(rendered)


# --- Page ----------------------------------------------------------------------------------------

def render(data: dict[str, Any]) -> str:
    s = data["summary"]
    totals = s.get("totals", {})
    rows = data["findings"]
    counts = {sev: sum(1 for r in rows if str(r.get("severity", "")).lower() == sev) for sev in SEVERITIES}
    areas = sorted({r["area"] for r in rows})
    task = s.get("task", "").strip()

    stats = "".join(
        f'<div class="stat sev-{sev}"><b>{counts[sev]}</b><span>{sev}</span></div>' for sev in SEVERITIES if counts[sev]
    ) or '<div class="stat"><b>0</b><span>findings</span></div>'
    meta = [f"shift {html.escape(data['id'])}", f"rig {html.escape(s.get('rig', '?'))}", html.escape(s.get("mode", ""))]
    if totals:
        meta.append(f"{totals.get('input_tokens', 0) + totals.get('cache_read_tokens', 0) + totals.get('cache_write_tokens', 0):,} in "
                    f"/ {totals.get('output_tokens', 0):,} out tok · est. {usd(totals.get('cost_usd'))}")
    if s.get("ok") is False:
        meta.append('<span class="bad">incomplete</span>')

    final = next((h for h in data["hands"] if h["key"] == data["final"]), None)
    final_html = ""
    if final and final["findings"] is None:
        final_html = f'<section class="final"><h2>Summary <small>from {html.escape(final["key"])}</small></h2>{markdown(final["output"])}</section>'

    table = ""
    if rows:
        chips = "".join(
            f'<label class="chip sev-{sev}"><input type="checkbox" data-sev="{sev}" checked> {sev} {counts[sev]}</label>'
            for sev in SEVERITIES if counts[sev]
        )
        area_chips = "".join(
            f'<label class="chip"><input type="checkbox" data-area="{html.escape(a)}" checked> {html.escape(a)}</label>' for a in areas
        )
        body = "".join(_row(r) for r in rows)
        table = f"""
<section>
  <h2>Findings <small id="shown">{len(rows)} of {len(rows)}</small></h2>
  <div class="filters">
    <div class="chips">{chips}</div>
    <div class="chips">{area_chips}</div>
    <input id="q" type="search" placeholder="Filter by file or text" aria-label="Filter findings">
  </div>
  <div class="table-wrap"><table>
    <thead><tr><th>Severity</th><th>Area</th><th>Location</th><th>Finding</th></tr></thead>
    <tbody>{body}</tbody>
  </table></div>
</section>"""

    hand_sections = "".join(_hand(h) for h in data["hands"] if h is not final or h["findings"] is not None)
    inputs = s.get("inputs") or {}
    inputs_html = "".join(f"<li><code>{html.escape(k)}</code> = {html.escape(str(v))}</li>" for k, v in inputs.items())

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rig report · {html.escape(s.get('rig', data['id']))} · {html.escape(data['id'])}</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <header>
    <p class="meta">{' · '.join(meta)}</p>
    <h1>{html.escape(task.splitlines()[0] if task else 'Shift ' + data['id'])}</h1>
    {f'<ul class="inputs">{inputs_html}</ul>' if inputs_html else ''}
    <div class="stats">{stats}</div>
  </header>
  {final_html}
  {table}
  <section>
    <h2>Hands</h2>
    {hand_sections}
  </section>
</main>
<script>{JS}</script>
</body>
</html>
"""


def _row(r: dict[str, Any]) -> str:
    sev = str(r.get("severity", "")).lower() or "?"
    loc = html.escape(str(r.get("file", "")))
    if r.get("line"):
        loc += f":{html.escape(str(r['line']))}"
    title = html.escape(str(r.get("title", "")))
    detail = html.escape(str(r.get("detail") or r.get("description") or ""))
    fix = r.get("suggestion") or r.get("fix")
    fix_html = f'<p class="fix"><b>Fix:</b> {html.escape(str(fix))}</p>' if fix else ""
    search = html.escape(" ".join(str(r.get(k, "")) for k in ("file", "title", "detail", "description", "suggestion")).lower())
    return (
        f'<tr data-sev="{html.escape(sev)}" data-area="{html.escape(r["area"])}" data-text="{search}">'
        f'<td><span class="sev sev-{html.escape(sev)}">{html.escape(sev)}</span></td>'
        f'<td>{html.escape(r["area"])}</td>'
        f'<td class="loc"><code>{loc}</code></td>'
        f'<td>{f"<b>{title}</b>" if title else ""}<p>{detail}</p>{fix_html}</td></tr>'
    )


def _hand(h: dict[str, Any]) -> str:
    m = h["meta"]
    bits = []
    if m.get("model"):
        bits.append(html.escape(m["model"]))
    if m.get("stop_reason"):
        cls = "" if m["stop_reason"] == "end_turn" else ' class="bad"'
        bits.append(f"<span{cls}>{html.escape(m['stop_reason'])}</span>")
    if "turns" in m:
        bits.append(f"{m['turns']} turns")
    if "cost_usd" in m:
        bits.append(usd(m["cost_usd"]))
    if h["findings"] is not None:
        body = f"<p>{len(h['findings'])} findings, listed in the table above.</p><details><summary>Raw JSON</summary><pre><code>{html.escape(h['output'])}</code></pre></details>"
    else:
        body = markdown(h["output"])
    return f'<details class="hand"><summary><b>{html.escape(h["key"])}</b> <small>{" · ".join(bits)}</small></summary><div class="hand-body">{body}</div></details>'


def write(shift_dir: Path) -> Path:
    path = shift_dir / "report.html"
    path.write_text(render(collect(shift_dir)), encoding="utf-8")
    return path


CSS = """
:root{--bg:#f6f7f5;--surface:#fff;--fg:#1c2420;--muted:#5f6a64;--line:#dde2dc;--accent:#2e6b5a;--code:#eef0ec;
--critical:#8f1d1d;--high:#b3401c;--medium:#a06b0b;--low:#2f6c8f;--info:#5f6a64;
--font:"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;--mono:Consolas,"SFMono-Regular",ui-monospace,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#131815;--surface:#1a201d;--fg:#e3e8e4;--muted:#9aa69f;--line:#2c3530;--accent:#7cc2ab;--code:#222924;
--critical:#ff8a8a;--high:#f29a6e;--medium:#e6c068;--low:#7fb8dc;--info:#9aa69f;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 var(--font);padding:32px 16px 64px}
main{max-width:1080px;margin:0 auto;display:grid;gap:36px}
h1{font-size:26px;line-height:1.3;margin:4px 0 0;text-wrap:balance}
h2{font-size:18px;margin:0 0 12px}h2 small,summary small{font-weight:400;color:var(--muted);font-size:13px}
h3,h4,h5,h6{font-size:15px;margin:16px 0 6px}
p{margin:0 0 8px}code{font-family:var(--mono);font-size:.9em;background:var(--code);padding:1px 4px;border-radius:3px}
pre{background:var(--code);padding:12px;border-radius:6px;overflow-x:auto;font:13px/1.55 var(--mono)}pre code{background:none;padding:0}
.meta{color:var(--muted);font-size:13px;margin:0}.bad{color:var(--high);font-weight:600}
.inputs{margin:8px 0 0;padding-left:18px;font-size:14px;color:var(--muted)}
.stats{display:flex;flex-wrap:wrap;gap:10px 28px;margin-top:16px;font-variant-numeric:tabular-nums}
.stat{display:grid}.stat b{font-size:24px;line-height:1.2}.stat span{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.stat.sev-critical b{color:var(--critical)}.stat.sev-high b{color:var(--high)}.stat.sev-medium b{color:var(--medium)}.stat.sev-low b{color:var(--low)}
.final{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:20px 22px}
.final ul,.final ol,.hand-body ul,.hand-body ol{margin:0 0 8px;padding-left:20px}
.filters{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:12px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:13px;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:2px 10px;cursor:pointer}
#q{flex:1;min-width:180px;font:inherit;font-size:14px;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg)}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:640px;font-size:14px}
th{text-align:left;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;padding:10px 12px;border-bottom:1px solid var(--line)}
td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
td p{margin:4px 0 0}.fix{color:var(--muted)}.loc code{white-space:nowrap}
.sev{display:inline-block;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:1px 8px;border-radius:999px;border:1px solid currentColor}
.sev-critical{color:var(--critical)}.sev-high{color:var(--high)}.sev-medium{color:var(--medium)}.sev-low{color:var(--low)}.sev-info{color:var(--info)}
.hand{background:var(--surface);border:1px solid var(--line);border-radius:8px;margin-bottom:10px}
.hand summary{padding:12px 16px;cursor:pointer}.hand-body{padding:0 18px 14px;min-width:0}
details details summary{cursor:pointer;color:var(--muted);font-size:13px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""

JS = """
const rows=[...document.querySelectorAll('tbody tr')];
const boxes=[...document.querySelectorAll('.filters input[type=checkbox]')];
const q=document.getElementById('q'),shown=document.getElementById('shown');
function apply(){
  const sev=new Set(boxes.filter(b=>b.dataset.sev&&b.checked).map(b=>b.dataset.sev));
  const area=new Set(boxes.filter(b=>b.dataset.area&&b.checked).map(b=>b.dataset.area));
  const text=(q&&q.value||'').trim().toLowerCase();let n=0;
  for(const r of rows){const ok=(sev.has(r.dataset.sev)||!boxes.some(b=>b.dataset.sev===r.dataset.sev))&&area.has(r.dataset.area)&&(!text||r.dataset.text.includes(text));r.hidden=!ok;if(ok)n++;}
  if(shown)shown.textContent=n+' of '+rows.length;
}
boxes.forEach(b=>b.addEventListener('change',apply));if(q)q.addEventListener('input',apply);
"""
