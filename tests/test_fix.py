"""Fixing a review finding: the report's fix button, the fix task, and fix.rig.yaml."""

import json
from pathlib import Path

import pytest
import yaml

from rig import report
from rig.cli import main
from rig.serve import App
from rig.spec import load

FINDING = {"severity": "high", "file": "backend/src/Order.java", "line": 142, "title": "NPE", "detail": "null customer",
           "suggestion": "return 404", "area": "bugs"}

REVIEW = """\
name: code-review
workspaces:
  backend: api
  frontend: web
hands:
  bugs: {role: B, tools: [read_file]}
"""


def make_shift(root: Path, findings: list[dict], rig: str = "code-review") -> str:
    d = root / ".rig" / "shifts" / "20261008-180000"
    d.mkdir(parents=True)
    (d / "bugs.md").write_text(json.dumps({"findings": findings}), encoding="utf-8")
    (d / "shift.json").write_text(json.dumps({"rig": rig, "task": "review", "hands": {"bugs": {}}}), encoding="utf-8")
    return d.name


@pytest.fixture
def app(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "review.rig.yaml").write_text(REVIEW, encoding="utf-8")
    return App(tmp_path)


def test_fix_task():
    assert report.fix_task(FINDING) == (
        "리뷰에서 발견된 문제를 고쳐줘.\n\n위치: backend/src/Order.java:142\n심각도: high (bugs)\n"
        "문제: NPE\n설명: null customer\n제안: return 404\n\n이 문제만 최소한으로 고치고, 무엇을 바꿨는지 요약해줘."
    )


def test_fix_buttons_only_when_served(app, tmp_path):
    shift = make_shift(tmp_path, [FINDING, {**FINDING, "severity": "critical", "line": 1}])
    static = report.render(report.collect(tmp_path / ".rig" / "shifts" / shift))
    assert "fix-btn" not in static.split("<style>")[1].split("</style>")[1]
    served = app.report_html(shift)
    # Rows are sorted most severe first, and the link index follows that order.
    assert served.count('class="fix-btn"') == 2 and f'href="/?fix={shift}&amp;n=0"' in served


def test_finding_carries_review_folders(app, tmp_path):
    shift = make_shift(tmp_path, [FINDING])
    f = app.finding(shift, 0)
    assert f["task"].startswith("리뷰에서 발견된 문제를 고쳐줘.")
    assert f["workspaces"] == [{"name": "backend", "path": "api"}, {"name": "frontend", "path": "web"}]
    with pytest.raises(ValueError, match="no finding 5"):
        app.finding(shift, 5)


def test_finding_from_unknown_rig_has_no_folders(app, tmp_path):
    shift = make_shift(tmp_path, [FINDING], rig="deleted-rig")
    assert app.finding(shift, 0)["workspaces"] is None


def test_init_fix_uses_review_folders(app, tmp_path):
    assert app.init_fix([{"name": "backend", "path": "api"}, {"name": "frontend", "path": "web"}]) == "fix.rig.yaml"
    rig = load(tmp_path / "fix.rig.yaml")
    assert rig.workspaces == {"backend": "api", "frontend": "web"}
    entry = next(r for r in app.rigs() if r["file"] == "fix.rig.yaml")
    assert entry["writes"] and entry["workspace_ok"]
    with pytest.raises(ValueError, match="already exists"):
        app.init_fix([{"name": "", "path": "api"}])


def test_init_fix_leaves_nothing_on_bad_folders(app, tmp_path):
    with pytest.raises(ValueError):
        app.init_fix([{"name": "a b", "path": "api"}, {"name": "c", "path": "web"}])
    assert not (tmp_path / "fix.rig.yaml").exists()


def test_fix_template(tmp_path, monkeypatch):
    rig = load(Path(__file__).parents[1] / "src" / "rig" / "template-fix.yaml")
    assert rig.edges == [("fixer", "checker")]
    assert "edit_file" in rig.hands["fixer"].tools and not {"edit_file", "write_file", "run"} & set(rig.hands["checker"].tools)
    monkeypatch.chdir(tmp_path)
    main(["-f", "fix.rig.yaml", "init", "--template", "fix"])
    assert yaml.safe_load((tmp_path / "fix.rig.yaml").read_text(encoding="utf-8"))["name"] == "fix"
