import ast
import os

import pytest
from pydantic import ValidationError

from rig.spec import Rig, RunPolicy
from rig.tools import Toolbox, ToolError, run_env


def box(tmp_path, *allow, timeout=30, max_output=20000, env_passthrough=(), env=None):
    policy = RunPolicy(allow=list(allow), timeout=timeout, max_output=max_output, env_passthrough=list(env_passthrough))
    return Toolbox(tmp_path, ["run"], run_policy=policy, env=env)


def test_runs_allowed_command_in_workspace(tmp_path):
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    out = box(tmp_path, "python -c").run("run", {"command": "python -c \"import os; print(sorted(os.listdir()))\""})
    assert out == "[exit code 0]\n['data.txt']\n" or out.replace("\r", "") == "[exit code 0]\n['data.txt']\n"


def test_nonzero_exit_is_reported_not_raised(tmp_path):
    out = box(tmp_path, "python -c").run("run", {"command": "python -c \"import sys; print('boom'); sys.exit(3)\""})
    assert out.startswith("[exit code 3]") and "boom" in out


def test_stderr_is_included(tmp_path):
    out = box(tmp_path, "python -c").run("run", {"command": "python -c \"import sys; sys.stderr.write('err-out')\""})
    assert "err-out" in out


@pytest.mark.parametrize(
    "command",
    [
        "python -m pip install x",       # prefix not allowed
        "pythonx -c 1",                  # prefix must match whole tokens
        "git status",
    ],
)
def test_disallowed_commands(tmp_path, command):
    with pytest.raises(ToolError, match="not allowed"):
        box(tmp_path, "python -c").run("run", {"command": command})


@pytest.mark.parametrize("command", ["python -c 1 && echo hi", "python -c 1 | more", "python -c 1 > out.txt", "python -c 1; ls", "python -c $(whoami)"])
def test_shell_syntax_rejected(tmp_path, command):
    with pytest.raises(ToolError, match="shell syntax"):
        box(tmp_path, "python -c").run("run", {"command": command})


@pytest.mark.parametrize(
    "arg",
    [
        "../outside.py",
        "--rootdir=../..",
        "/etc/passwd",                 # was missed on Windows, where it isn't is_absolute()
        "--config=/etc/x",
        "C:/Windows/System32",         # was missed on Linux, where it's a relative path
        r"'\\server\share\x'",         # UNC path (quoted so shlex keeps the backslashes)
    ],
)
def test_paths_outside_workspace_rejected(tmp_path, arg):
    with pytest.raises(ToolError, match="outside the workspace"):
        box(tmp_path, "python").run("run", {"command": f"python {arg}"})


def test_paths_inside_workspace_allowed(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "ok.py").write_text("print('ok')", encoding="utf-8")
    assert "ok" in box(tmp_path, "python").run("run", {"command": "python sub/../ok.py"})
    assert "ok" in box(tmp_path, "python").run("run", {"command": f"python {(tmp_path / 'ok.py').as_posix()}"})


@pytest.mark.parametrize("arg", ["origin/main", "HEAD~1:src/a.py", "tests::test_x", "-k", "http://example.com/a"])
def test_non_path_arguments_pass(tmp_path, arg):
    assert not box(tmp_path, "python")._outside(arg)


def test_timeout(tmp_path):
    out = box(tmp_path, "python -c", timeout=1).run("run", {"command": "python -c \"import time; print('started', flush=True); time.sleep(10)\""})
    assert out.startswith("[timed out after 1s]")


def test_output_keeps_the_tail(tmp_path):
    out = box(tmp_path, "python -c", max_output=1000).run("run", {"command": "python -c \"print('a' * 5000 + 'END')\""})
    assert "earlier chars cut" in out and out.rstrip().endswith("END")


def test_missing_executable(tmp_path):
    with pytest.raises(ToolError, match="not found"):
        box(tmp_path, "no-such-tool-xyz").run("run", {"command": "no-such-tool-xyz --help"})


def test_definition_lists_allowed_commands(tmp_path):
    (d,) = box(tmp_path, "uv run pytest", "git diff").definitions
    assert d["name"] == "run" and "- uv run pytest\n- git diff" in d["description"]


SECRETS = {"ANTHROPIC_API_KEY": "sk-secret", "GITHUB_TOKEN": "gh-secret", "AWS_SECRET": "aws-secret", "DB_PASSWORD_X": "db-secret"}


def child_env(b, *names):
    """The variables a `run` child process sees, of `names`."""
    out = b.run("run", {"command": f"python -c \"import os; print(sorted(k for k in os.environ if k.upper() in {list(names)!r}))\""})
    assert out.startswith("[exit code 0]"), out
    return ast.literal_eval(out.splitlines()[1])


def test_run_hides_secret_env_vars(tmp_path, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RIG_TEST_PLAIN", "visible")
    assert child_env(box(tmp_path, "python -c"), *SECRETS, "RIG_TEST_PLAIN") == ["RIG_TEST_PLAIN"]


def test_env_passthrough_keeps_named_vars(tmp_path, monkeypatch):
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    b = box(tmp_path, "python -c", env_passthrough=["github_token"])
    assert child_env(b, *SECRETS) == ["GITHUB_TOKEN"]


def test_explicit_env_is_filtered_too(tmp_path):
    # The worktree case: rig passes its own env, with git config overrides.
    env = {**os.environ, **SECRETS, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "x"}
    names = [*SECRETS, "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]
    assert child_env(box(tmp_path, "python -c", env=env), *names) == ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]


def test_run_env_is_case_insensitive():
    base = {"anthropic_api_key": "1", "My_Token": "2", "Path": "3", "SystemRoot": "4", "db_password": "5", "Keyboard": "6"}
    assert run_env(base, []) == {"Path": "3", "SystemRoot": "4", "Keyboard": "6"}
    assert run_env(base, ["MY_TOKEN"]) == {"My_Token": "2", "Path": "3", "SystemRoot": "4", "Keyboard": "6"}


def test_env_passthrough_rejects_empty_names():
    with pytest.raises(ValidationError, match="env_passthrough"):
        RunPolicy(allow=["python"], env_passthrough=[" "])


def test_run_tool_requires_allow_list():
    with pytest.raises(ValidationError, match="run.allow is empty"):
        Rig.model_validate({"name": "t", "hands": {"tester": {"role": "Test.", "tools": ["run"]}}})
    Rig.model_validate({"name": "t", "run": {"allow": ["uv run pytest"]}, "hands": {"tester": {"role": "Test.", "tools": ["run"]}}})
