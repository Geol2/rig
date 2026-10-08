import json
from pathlib import Path

import pytest

from rig import report
from rig.cli import main
from rig.spec import load


def finding(severity, file, line, title="t"):
    return {"severity": severity, "file": file, "line": line, "title": title, "detail": "d", "suggestion": "s"}


def make_shift(root: Path, outputs: dict[str, str], shift_id="20261008-180000", **summary) -> Path:
    d = root / ".rig" / "shifts" / shift_id
    d.mkdir(parents=True)
    for key, text in outputs.items():
        (d / f"{key.replace('#', '-')}.md").write_text(text, encoding="utf-8")
    data = {"rig": "review", "mode": "lines", "task": "Review it", "ok": True,
            "hands": {k: {"stop_reason": "end_turn", "turns": 3, "model": "claude-opus-5-5", "cost_usd": 0.1} for k in outputs},
            "totals": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.3}, **summary}
    (d / "shift.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def test_findings_of():
    assert report.findings_of(json.dumps({"findings": [finding("high", "a.py", 1), "junk"]})) == [finding("high", "a.py", 1)]
    assert report.findings_of("## plain markdown") is None
    assert report.findings_of(json.dumps({"other": 1})) is None


def test_collect_merges_and_sorts_findings(tmp_path):
    d = make_shift(tmp_path, {
        "mapper": "## Map",
        "bugs": json.dumps({"findings": [finding("low", "b.py", 9), finding("high", "b.py", 20)]}),
        "security": json.dumps({"findings": [finding("critical", "conf.properties", 3), finding("high", "a.py", 5)]}),
        "summary": "Fix the password first.",
    })
    data = report.collect(d)
    assert [(r["severity"], r["file"], r["area"]) for r in data["findings"]] == [
        ("critical", "conf.properties", "security"), ("high", "a.py", "security"), ("high", "b.py", "bugs"), ("low", "b.py", "bugs"),
    ]
    assert data["final"] == "summary"


def test_foreman_reply_is_the_summary(tmp_path):
    d = make_shift(tmp_path, {"reviewer#1": json.dumps({"findings": []}), "foreman": "Done."}, mode="foreman")
    data = report.collect(d)
    assert data["final"] == "foreman"
    assert {h["key"] for h in data["hands"]} == {"reviewer#1", "foreman"}


def test_render(tmp_path):
    d = make_shift(tmp_path, {
        "bugs": json.dumps({"findings": [finding("high", "src/<A>.java", 12, "NPE")]}),
        "summary": "## 종합\n\n1. **먼저** `A.java:12`\n\n```\ncode <x>\n```",
    })
    page = report.render(report.collect(d))
    assert '<tr data-sev="high" data-area="bugs"' in page
    assert "src/&lt;A&gt;.java:12" in page            # escaped
    assert '<input type="checkbox" data-sev="high" checked> high 1' in page
    assert "<ol><li><strong>먼저</strong> <code>A.java:12</code></li></ol>" in page
    assert "<pre><code>code &lt;x&gt;</code></pre>" in page
    assert "est. $0.30" in page


def test_markdown_lists_and_headings():
    assert report.markdown("# Title\n- a\n- b\n\ntext `x`") == "<h3>Title</h3>\n<ul><li>a</li><li>b</li></ul>\n<p>text <code>x</code></p>"


def test_cli_report_writes_latest(tmp_path, monkeypatch, capsys):
    make_shift(tmp_path, {"a": "old"}, shift_id="20261008-100000")
    newest = make_shift(tmp_path, {"a": "new"}, shift_id="20261008-110000")
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    monkeypatch.chdir(tmp_path)
    main(["report"])
    assert (newest / "report.html").exists() and opened == [(newest / "report.html").as_uri()]
    main(["report", "20261008-100000", "--no-open"])
    assert (tmp_path / ".rig/shifts/20261008-100000/report.html").exists() and len(opened) == 1
    assert "report →" in capsys.readouterr().out


def test_cli_report_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no shifts yet"):
        main(["report"])
    make_shift(tmp_path, {"a": "x"})
    with pytest.raises(SystemExit, match="no shift nope"):
        main(["report", "nope"])


def test_review_template(tmp_path, monkeypatch):
    rig = load(Path(__file__).parents[1] / "src" / "rig" / "template-review.yaml")
    assert all(not {"write_file", "edit_file", "run"} & set(h.tools) for h in rig.hands.values())
    # The YAML anchor gives the three reviewers the same closed findings schema.
    schemas = [rig.resolve(n).output_schema for n in ("bugs", "security", "db")]
    assert schemas[0] == schemas[1] == schemas[2]
    assert schemas[0]["properties"]["findings"]["items"]["additionalProperties"] is False
    assert rig.resolve("summary").output_schema is None

    monkeypatch.chdir(tmp_path)
    main(["-f", "review.rig.yaml", "init", "--template", "review"])
    with pytest.raises(SystemExit, match="workspace D:/path/to/project not found"):
        main(["-f", "review.rig.yaml", "check"])
