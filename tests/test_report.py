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
    assert data["final"] == ["summary"]  # no "final" in shift.json: the last hand


def test_foreman_reply_is_the_summary(tmp_path):
    d = make_shift(tmp_path, {"reviewer#1": json.dumps({"findings": []}), "foreman": "Done."}, mode="foreman")
    data = report.collect(d)
    assert data["final"] == ["foreman"]
    assert {h["key"] for h in data["hands"]} == {"reviewer#1", "foreman"}


def test_every_final_hand_is_a_summary(tmp_path):
    d = make_shift(tmp_path, {
        "mapper": "## Map",
        "bugs": json.dumps({"findings": [finding("high", "b.py", 1)]}),
        "s1": "First **summary**.",
        "s2": "Second summary.",
    }, final=["bugs", "s1", "s2", "gone"])
    data = report.collect(d)
    assert data["final"] == ["bugs", "s1", "s2"]  # unknown keys dropped
    page = report.render(data)
    assert page.count('<section class="final">') == 2
    assert page.index("Summary <small>from s1</small>") < page.index("Summary <small>from s2</small>")
    assert page.count("First <strong>summary</strong>.") == 1 and page.count("Second summary.") == 1
    # Summaries aren't repeated in the Hands list; a final hand with findings still is.
    hands = page[page.index("<h2>Hands</h2>"):page.index("<script>")]
    assert "<b>mapper</b>" in hands and "<b>bugs</b>" in hands and "<b>s1</b>" not in hands and "<b>s2</b>" not in hands


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


def test_markdown_edge_cases():
    # An empty item, a nested marker, and a number without the dot's trailing space.
    assert report.markdown("- \n  * b\n3. c\n10.no") == "<ul><li></li><li>b</li><li>c</li></ul>\n<p>10.no</p>"


def test_markdown_is_linear_on_long_lines():
    import time

    start = time.monotonic()
    report.markdown(" " * 200_000 + "x\n" + "1" * 200_000)
    assert time.monotonic() - start < 1


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
