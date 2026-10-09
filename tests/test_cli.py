import json

import pytest

import rig
from rig.cli import main


def _exit_message(excinfo) -> str:
    """sys.exit("msg") stores the message in .code; the interpreter prints it to stderr with exit status 1."""
    code = excinfo.value.code
    assert isinstance(code, str), f"expected an error message, got exit code {code!r}"
    return code


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert out.strip() == f"rig {rig.__version__}"


def test_check_valid_rig(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(
        "name: ok\nhands:\n  a: {role: A}\n  b: {role: B}\nlines: ['a -> b']\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    main(["check"])
    out = capsys.readouterr().out
    assert "✓ ok: 2 hands, 1 lines" in out
    assert "⚠" not in out  # the default model is priced


def test_check_warns_about_unpriced_model(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(
        "name: ok\nmax_cost_usd: 5\nhands:\n  a: {role: A, model: claude-opus-9}\n  b: {role: B}\nlines: ['a -> b']\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    main(["check"])  # a warning, not an error
    out = capsys.readouterr().out
    assert "✓ ok: 2 hands, 1 lines" in out
    assert "⚠ cost limit $5.00 can't count hands a: no price for model 'claude-opus-9'" in out


@pytest.mark.parametrize(
    "content, detail",
    [
        ("name: bad\nhands:\n  a: {role: A}\nlines: ['a -> ghost']\n", "unknown hand 'ghost'"),
        ("name: bad\nhands: {}\n", "at least one hand"),
        ("name: bad\nhands:\n  a: {role: A, tools: [rm_rf]}\n", "unknown tools"),
        ("hands:\n  a: {role: A}\n", "name\n  Field required"),
        ("- just\n- a list\n", "valid dictionary"),
        ("name: bad\nhands:\n  a: {role: A, max_turns: 0}\n", "hands.a.max_turns\n  Input should be greater than or equal to 1"),
    ],
    ids=["unknown-hand", "no-hands", "unknown-tool", "missing-name", "not-a-mapping", "zero-max-turns"],
)
def test_check_schema_invalid_rig_exits_with_error(tmp_path, monkeypatch, capsys, content, detail):
    (tmp_path / "rig.yaml").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg.startswith("rig: rig.yaml is invalid")
    assert detail in msg
    assert "✓" not in capsys.readouterr().out


def test_check_uses_file_option(tmp_path):
    bad = tmp_path / "custom.yaml"
    bad.write_text("name: bad\nhands: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["-f", str(bad), "check"])
    assert f"{bad} is invalid" in _exit_message(excinfo)


def test_check_missing_file_exits_with_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert "rig.yaml not found" in msg
    assert "rig init" in msg


def _shift(tmp_path, shift_id, summary):
    d = tmp_path / ".rig" / "shifts" / shift_id
    d.mkdir(parents=True)
    (d / "a.md").write_text("out", encoding="utf-8")
    (d / "shift.json").write_text(json.dumps(summary), encoding="utf-8")


def test_logs_show_cost(tmp_path, monkeypatch, capsys):
    totals = {"input_tokens": 1234, "output_tokens": 56, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 1.5}
    _shift(tmp_path, "20250101-000000", {"mode": "lines", "ok": True, "task": "old task"})
    _shift(tmp_path, "20250102-000000", {"mode": "lines", "ok": True, "task": "priced", "totals": totals})
    _shift(tmp_path, "20250103-000000", {"mode": "foreman", "ok": False, "task": "unpriced", "totals": {**totals, "cost_usd": None}})
    monkeypatch.chdir(tmp_path)

    main(["logs"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith("old task") and "$" not in lines[0] and "n/a" not in lines[0]
    assert "$1.50  priced" in lines[1]
    assert "n/a  unpriced" in lines[2]
    assert len({line.index(t) for line, t in zip(lines, ["old task", "priced", "unpriced"])}) == 1

    main(["logs", "20250102-000000"])
    out = capsys.readouterr().out
    assert out.startswith("tokens: 1,234 in · 56 out · est. $1.50\n")
    assert "── a ──\nout" in out

    main(["logs", "last"])
    assert capsys.readouterr().out.startswith("tokens: 1,234 in · 56 out · est. n/a\n")

    main(["logs", "20250101-000000"])
    assert capsys.readouterr().out.startswith("── a ──")


def test_check_malformed_yaml_exits_with_error(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: bad\nhands: [unclosed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg.startswith("rig: rig.yaml is invalid")
    assert "unclosed" in msg or "line 2" in msg


def test_run_crash_exits_1_without_traceback(tmp_path, monkeypatch, capsys):
    async def crash(*args, **kwargs):
        raise RuntimeError("boom")

    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("rig.runner._run_lines", crash)
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--dry", "go"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "✗ shift failed: RuntimeError: boom" in captured.out
    assert "Traceback" not in captured.out + captured.err
    shift_dir = next((tmp_path / ".rig" / "shifts").iterdir())
    assert json.loads((shift_dir / "shift.json").read_text(encoding="utf-8"))["error"] == "RuntimeError: boom"


def test_run_prints_every_last_stage_hand(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(
        "name: t\nhands:\n  a: {role: A}\n  b: {role: B}\n  c: {role: C}\nlines: ['a -> b', 'a -> c']\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    main(["run", "--dry", "-q", "go"])
    out = capsys.readouterr().out
    assert "\n── b ──\n" in out and "\n── c ──\n" in out
    assert "── a ──" not in out
    assert out.index("── b ──") < out.index("── c ──")


def test_logs_shows_worktree_branch(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20261008-100000", {"mode": "lines", "ok": True, "task": "plain run", "branch": None})
    _shift(tmp_path, "20261008-110000", {"mode": "lines", "ok": True, "task": "worktree run", "branch": "rig/20261008-110000"})
    monkeypatch.chdir(tmp_path)
    main(["logs"])
    plain, worktree = capsys.readouterr().out.splitlines()
    assert "rig/20261008-110000  worktree run" in worktree
    assert "rig/" not in plain
    # Tasks line up whether or not a shift has a branch.
    assert plain.index("plain run") == worktree.index("worktree run")


def test_logs_without_branches_has_no_branch_column(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20261008-100000", {"mode": "lines", "ok": True, "task": "plain run", "branch": None})
    monkeypatch.chdir(tmp_path)
    main(["logs"])
    assert capsys.readouterr().out.rstrip("\n") == f"20261008-100000  {'lines':<8}  {'ok':<10}  {'':>9}  plain run"
