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
