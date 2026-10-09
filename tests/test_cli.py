import json
import os
import subprocess
import sys

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


def test_check_warns_about_hands_off_crew(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(
        "name: ok\nhands:\n  a: {role: A}\n  b: {role: B}\nforeman: {crew: [a]}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    main(["check"])  # a warning, not an error
    out = capsys.readouterr().out
    assert "✓ ok: foreman + 1 hands" in out
    assert "⚠ hands not on foreman.crew never run: b; add them to the crew or remove them" in out


def test_check_hand_warning_before_price_warning(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(
        "name: ok\nhands:\n  a: {role: A, model: claude-opus-9}\n  b: {role: B}\n  c: {role: C}\nlines: ['a -> b']\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    main(["check"])
    out = capsys.readouterr().out
    assert "✓ ok: 3 hands, 1 lines" in out
    hand = out.index("⚠ hands on no line run alone in stage 1 and no hand gets their output: c;")
    assert hand < out.index("⚠ no price for model 'claude-opus-9'")


@pytest.mark.parametrize(
    "content, detail",
    [
        ("name: bad\nhands:\n  a: {role: A}\nlines: ['a -> ghost']\n", "unknown hand 'ghost'"),
        ("name: bad\nhands: {}\n", "at least one hand"),
        ("name: bad\nhands:\n  a: {role: A, tools: [rm_rf]}\n", "unknown tools"),
        ("hands:\n  a: {role: A}\n", "name\n  Field required"),
        ("- just\n- a list\n", "valid dictionary"),
        ("name: bad\nhands:\n  a: {role: A, max_turns: 0}\n", "hands.a.max_turns\n  Input should be greater than or equal to 1"),
        ("name: bad\nhands:\n  a: {role: A}\n  b: {role: B}\nforeman: {crew: [a]}\npublish: {approver: b}\n",
         "publish.approver 'b' isn't on foreman.crew, so it never runs and can't approve; add it to the crew"),
    ],
    ids=["unknown-hand", "no-hands", "unknown-tool", "missing-name", "not-a-mapping", "zero-max-turns",
         "approver-off-crew"],
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


def _shift(tmp_path, shift_id, summary, mds=("a",), running=None):
    """A shift folder; summary None leaves out shift.json, running is written as running.json."""
    d = tmp_path / ".rig" / "shifts" / shift_id
    d.mkdir(parents=True)
    for stem in mds:
        (d / f"{stem}.md").write_text("out" if stem == "a" else f"{stem} out", encoding="utf-8")
    if summary is not None:
        (d / "shift.json").write_text(json.dumps(summary), encoding="utf-8")
    if running is not None:
        (d / "running.json").write_text(json.dumps(running), encoding="utf-8")
    return d


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


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


def test_logs_status_column(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20250101-000000", None, running={"pid": os.getpid(), "task": "live task\nmore"})
    _shift(tmp_path, "20250102-000000", {"mode": "lines", "ok": False, "task": "crashed", "error": "RuntimeError: boom"})
    _shift(tmp_path, "20250103-000000", {"mode": "lines", "ok": False, "task": "both", "error": "interrupted",
                                         "stopped": "stopped"})
    _shift(tmp_path, "20250104-000000", {"mode": "lines", "ok": False, "task": "halted", "stopped": "budget"})
    _shift(tmp_path, "20250105-000000", {"mode": "lines", "ok": True, "task": "fine"})
    _shift(tmp_path, "20250106-000000", {"mode": "lines", "ok": False, "task": "partial"})
    _shift(tmp_path, "20250107-000000", None)
    _shift(tmp_path, "20250108-000000", None, running={"pid": _dead_pid(), "task": "killed"})
    (tmp_path / ".rig" / "shifts" / "stray.txt").write_text("not a shift", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    main(["logs"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"20250101-000000  {'':<8}  {'running':<10}  {'':>9}  live task"
    # error wins over stopped; a running.json whose process is gone is just incomplete
    assert [line[27:37].strip() for line in lines] == [
        "running", "error", "error", "stopped", "ok", "incomplete", "incomplete", "incomplete"]
    assert "killed" not in lines[7]  # running.json's task only stands in while the shift runs


def test_logs_shows_error_and_stop_first(tmp_path, monkeypatch, capsys):
    totals = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 5.12}
    _shift(tmp_path, "20250101-000000", {"ok": False, "error": "interrupted", "stopped": "budget", "max_cost_usd": 5,
                                         "totals": totals})
    _shift(tmp_path, "20250102-000000", {"ok": False, "stopped": "budget", "max_cost_usd": None, "totals": totals})
    _shift(tmp_path, "20250103-000000", {"ok": False, "stopped": "stopped", "totals": {**totals, "cost_usd": None}})
    monkeypatch.chdir(tmp_path)

    main(["logs", "20250101-000000"])
    assert capsys.readouterr().out.startswith(
        "✗ shift failed: interrupted\n✗ stopped: cost limit $5.00 reached ($5.12 spent)\n"
        "tokens: 10 in · 5 out · est. $5.12\n\n── a ──\nout\n")
    main(["logs", "20250102-000000"])
    assert capsys.readouterr().out.startswith("✗ stopped: cost limit reached ($5.12 spent)\ntokens: ")
    main(["logs", "20250103-000000"])
    assert capsys.readouterr().out.startswith("✗ stopped: stopped by user\ntokens: ")


def test_logs_detail_of_running_shift(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20250101-000000", None, running={"pid": os.getpid(), "task": "go"})
    monkeypatch.chdir(tmp_path)
    main(["logs", "last"])
    assert capsys.readouterr().out == f"still running (pid {os.getpid()}). Output so far:\n\n── a ──\nout\n\n"


def test_logs_hands_in_run_order(tmp_path, monkeypatch, capsys):
    hands = {"coder": {}, "coder#2": {}, "coder#10": {}, "gone": {}, "foreman": {}}  # gone has no .md
    _shift(tmp_path, "20250101-000000", {"ok": True, "hands": hands}, mds=("foreman", "coder-10", "coder", "coder-2"))
    monkeypatch.chdir(tmp_path)
    main(["logs", "last"])
    out = capsys.readouterr().out
    headings = [line for line in out.splitlines() if line.startswith("── ")]
    assert headings == ["── coder ──", "── coder#2 ──", "── coder#10 ──", "── foreman ──"]
    assert "── coder#10 ──\ncoder-10 out\n" in out
    assert out.endswith("── foreman ──\nforeman out\n\n")


def test_logs_leftover_outputs_in_natural_order(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20250101-000000", {"ok": True, "hands": {"b": {}}}, mds=("x-10", "b", "x-2", "a"))
    _shift(tmp_path, "20250102-000000", None, mds=("coder-10", "coder-2", "coder"))
    monkeypatch.chdir(tmp_path)
    main(["logs", "20250101-000000"])
    headings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("── ")]
    assert headings == ["── b ──", "── a ──", "── x-2 ──", "── x-10 ──"]
    main(["logs", "20250102-000000"])
    headings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("── ")]
    assert headings == ["── coder ──", "── coder-2 ──", "── coder-10 ──"]


def _broken_shift(tmp_path, shift_id, text='{"rig": "x", "ok": tr'):
    d = _shift(tmp_path, shift_id, None)
    (d / "shift.json").write_text(text, encoding="utf-8")
    return d


def test_logs_list_survives_broken_shift_json(tmp_path, monkeypatch, capsys):
    totals = {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 1.5}
    _shift(tmp_path, "20250101-000000", {"mode": "lines", "ok": True, "task": "fine", "totals": totals,
                                         "branch": "rig/20250101-000000"})
    _broken_shift(tmp_path, "20250102-000000")
    _broken_shift(tmp_path, "20250103-000000", "[]")
    _shift(tmp_path, "20250104-000000", None)
    monkeypatch.chdir(tmp_path)

    main(["logs"])
    lines = capsys.readouterr().out.splitlines()
    width = len("rig/20250101-000000")
    assert lines[0] == f"20250101-000000  {'lines':<8}  {'ok':<10}  {'$1.50':>9}  {'rig/20250101-000000'}  fine"
    assert lines[1] == f"20250102-000000  {'':<8}  {'unreadable':<10}  {'':>9}  {'':<{width}}  "
    assert [line[27:37].strip() for line in lines] == ["ok", "unreadable", "unreadable", "incomplete"]


def test_logs_detail_of_broken_shift_json(tmp_path, monkeypatch, capsys):
    _broken_shift(tmp_path, "20250101-000000")
    _broken_shift(tmp_path, "20250102-000000", "[]")
    monkeypatch.chdir(tmp_path)

    main(["logs", "20250101-000000"])
    out = capsys.readouterr().out
    assert out.startswith("✗ cannot read shift.json (")
    first, rest = out.split("\n", 1)
    assert first.endswith("; showing hand outputs only")
    assert rest == "\n── a ──\nout\n\n"  # no cost line
    main(["logs", "20250102-000000"])
    assert capsys.readouterr().out.startswith("✗ cannot read shift.json (not a JSON object); showing hand outputs only\n\n")


def test_logs_detail_of_broken_running_shift(tmp_path, monkeypatch, capsys):
    d = _broken_shift(tmp_path, "20250101-000000")
    (d / "running.json").write_text(json.dumps({"pid": os.getpid(), "task": "go"}), encoding="utf-8")
    (d / "a.md").unlink()
    monkeypatch.chdir(tmp_path)
    main(["logs", "last"])
    out = capsys.readouterr().out
    assert out.startswith("✗ cannot read shift.json (")
    assert out.endswith(f"; showing hand outputs only\nstill running (pid {os.getpid()}). Output so far:\n\n")


def test_report_of_broken_shift_json(tmp_path, monkeypatch, capsys):
    _broken_shift(tmp_path, "20250101-000000")
    monkeypatch.chdir(tmp_path)
    main(["report", "20250101-000000", "--no-open"])
    assert capsys.readouterr().out.startswith("report → ")


def test_check_malformed_yaml_exits_with_error(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: bad\nhands: [unclosed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg.startswith("rig: rig.yaml is invalid")
    assert "unclosed" in msg or "line 2" in msg


def test_check_duplicate_key_exits_with_error(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n  b: {role: B}\n  a: {role: C}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg.startswith("rig: rig.yaml is invalid")
    assert 'duplicate key "a" in hands at line 5 (first at line 3); remove or rename one' in msg


def test_check_non_utf8_file_exits_with_error(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_bytes("name: t\nhands:\n  a: {role: 역할}\n".encode("cp949"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["check"])
    msg = _exit_message(excinfo)
    assert msg == "rig: rig.yaml is invalid\nnot UTF-8 (byte 0xbf at line 3, column 13); save the file as UTF-8"


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


def test_run_resume_refuses_task_and_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no rig.yaml: the message comes before anything else
    for argv in (["run", "--resume", "last", "task"], ["run", "--resume", "last", "-i", "x=1"]):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert _exit_message(excinfo) == "rig: --resume reuses the old shift's task and inputs; don't pass a task or -i"


def test_run_resume_of_ok_shift(tmp_path, monkeypatch):
    (tmp_path / "rig.yaml").write_text("name: t\nhands:\n  a: {role: A}\n", encoding="utf-8")
    _shift(tmp_path, "20250101-000000", {"rig": "t", "mode": "lines", "ok": True, "task": "done"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--resume", "last"])
    assert _exit_message(excinfo) == "rig: shift 20250101-000000 finished ok; nothing to resume"
    assert [d.name for d in (tmp_path / ".rig" / "shifts").iterdir()] == ["20250101-000000"]


def test_logs_shows_resumed_from(tmp_path, monkeypatch, capsys):
    _shift(tmp_path, "20250102-000000", {"mode": "lines", "ok": True, "task": "go", "resumed_from": "20250101-000000"})
    monkeypatch.chdir(tmp_path)
    main(["logs", "20250102-000000"])
    assert capsys.readouterr().out.startswith("resumed from 20250101-000000\n")


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
