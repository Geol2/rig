import pytest

from rig.tools import Toolbox, ToolError


def test_write_then_read(tmp_path):
    tb = Toolbox(tmp_path, ["write_file", "read_file", "list_dir"])
    tb.run("write_file", {"path": "src/x.txt", "content": "hi"})
    assert tb.run("read_file", {"path": "src/x.txt"}) == "hi"
    assert tb.run("list_dir", {"path": "."}) == "src/"


def test_path_escape_blocked(tmp_path):
    tb = Toolbox(tmp_path, ["read_file"])
    with pytest.raises(ToolError):
        tb.run("read_file", {"path": "../secret"})


def test_tool_not_granted(tmp_path):
    tb = Toolbox(tmp_path, ["read_file"])
    with pytest.raises(ToolError):
        tb.run("write_file", {"path": "x", "content": ""})
