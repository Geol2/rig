import asyncio

import pytest

from rig.hand import HandResult
from rig.runner import run_shift
from rig.spec import Rig, RunPolicy
from rig.tools import Toolbox, ToolError, describe


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("read_file", {"path": "src/a.py"}, "read_file  src/a.py"),
        ("read_file", {"path": "a.py", "offset": 40, "limit": 20}, "read_file  a.py :40+20"),
        ("write_file", {"path": "a.py", "content": "x" * 12}, "write_file a.py (12 chars)"),
        ("search", {"pattern": "def main", "path": "src", "glob": "**/*.py"}, "search     'def main' in src (**/*.py)"),
        ("glob", {"pattern": "**/*.md"}, "glob       **/*.md"),
        ("run", {"command": "uv run pytest -q"}, "run        uv run pytest -q"),
    ],
)
def test_describe(name, args, expected):
    assert describe(name, args) == expected


def test_toolbox_reports_calls_results_and_errors(tmp_path):
    lines = []
    tb = Toolbox(tmp_path, ["write_file", "read_file", "run"], run_policy=RunPolicy(allow=["python -c"]), on_call=lines.append)

    async def go():
        await tb.call("write_file", {"path": "a.txt", "content": "hi"})
        await tb.call("run", {"command": "python -c \"import sys; sys.exit(2)\""})
        with pytest.raises(ToolError):
            await tb.call("read_file", {"path": "missing.txt"})

    asyncio.run(go())
    assert lines[0] == "write_file a.txt (2 chars)"
    assert lines[1].startswith("run ") and lines[2].startswith("  → exit code 2 (")
    assert lines[3].startswith("read_file") and lines[4] == "  ✗ no such file: missing.txt"


class ToolUsingWorker:
    async def run(self, hand, prompt, toolbox, check=None):
        await toolbox.call("list_dir", {"path": "."})
        return HandResult(name=hand.name, output="ok", stop_reason="end_turn", turns=1)


def _rig():
    return Rig.model_validate({"name": "t", "hands": {"a": {"role": "A", "tools": ["list_dir"]}}})


def test_shift_prints_tool_lines_labelled_by_hand(tmp_path):
    events = []
    asyncio.run(run_shift(_rig(), "t", ToolUsingWorker(), root=tmp_path, on_event=events.append))
    assert any(e.startswith("    · a") and "list_dir" in e for e in events)


def test_quiet_hides_tool_lines(tmp_path):
    events = []
    asyncio.run(run_shift(_rig(), "t", ToolUsingWorker(), root=tmp_path, on_event=events.append, verbose=False))
    assert not any("list_dir" in e for e in events)
