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
    assert "✓ ok: 2 hands, 1 lines" in capsys.readouterr().out


@pytest.mark.parametrize(
    "content, detail",
    [
        ("name: bad\nhands:\n  a: {role: A}\nlines: ['a -> ghost']\n", "unknown hand 'ghost'"),
        ("name: bad\nhands: {}\n", "at least one hand"),
        ("name: bad\nhands:\n  a: {role: A, tools: [rm_rf]}\n", "unknown tools"),
        ("hands:\n  a: {role: A}\n", "name\n  Field required"),
        ("- just\n- a list\n", "valid dictionary"),
    ],
    ids=["unknown-hand", "no-hands", "unknown-tool", "missing-name", "not-a-mapping"],
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


def test_check_malformed_yaml_exits_with_error(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: bad\nhands: [unclosed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg.startswith("rig: rig.yaml is invalid")
    assert "unclosed" in msg or "line 2" in msg


def _log_shift(root, shift_id, **summary):
    d = root / ".rig" / "shifts" / shift_id
    d.mkdir(parents=True)
    (d / "shift.json").write_text(json.dumps({"mode": "lines", "ok": True, **summary}), encoding="utf-8")


def test_logs_shows_worktree_branch(tmp_path, monkeypatch, capsys):
    _log_shift(tmp_path, "20261008-100000", task="plain run", branch=None)
    _log_shift(tmp_path, "20261008-110000", task="worktree run", branch="rig/20261008-110000")
    monkeypatch.chdir(tmp_path)
    main(["logs"])
    plain, worktree = capsys.readouterr().out.splitlines()
    assert "rig/20261008-110000  worktree run" in worktree
    assert "rig/" not in plain
    # Tasks line up whether or not a shift has a branch.
    assert plain.index("plain run") == worktree.index("worktree run")


def test_logs_without_branches_has_no_branch_column(tmp_path, monkeypatch, capsys):
    _log_shift(tmp_path, "20261008-100000", task="plain run", branch=None)
    monkeypatch.chdir(tmp_path)
    main(["logs"])
    assert capsys.readouterr().out.rstrip("\n") == f"20261008-100000  {'lines':<8}  {'ok':<10}  {'':>9}  plain run"
