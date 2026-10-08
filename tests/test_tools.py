import pytest

from rig.tools import READ_LIMIT, Toolbox, ToolError, glob_regex

ALL = ["write_file", "read_file", "list_dir", "glob", "search"]


@pytest.fixture
def repo(tmp_path):
    files = {
        "README.md": "# demo\n",
        "src/app.py": "import os\n\ndef main():\n    print('hello')\n",
        "src/util/strings.py": "def Hello():\n    return 'HELLO'\n",
        "tests/test_app.py": "from src.app import main\n",
        "node_modules/lib/index.js": "hello from deps\n",
        ".git/config": "hello git\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\0\0hello")
    return Toolbox(tmp_path, ALL)


def test_write_then_read(tmp_path):
    tb = Toolbox(tmp_path, ALL)
    tb.run("write_file", {"path": "src/x.txt", "content": "hi"})
    assert tb.run("read_file", {"path": "src/x.txt"}) == "1\thi"
    assert tb.run("list_dir", {"path": "."}) == "src/"


def test_read_range_and_continuation_hint(tmp_path):
    (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(1, 51)), encoding="utf-8")
    tb = Toolbox(tmp_path, ["read_file"])
    out = tb.run("read_file", {"path": "big.txt", "offset": 10, "limit": 3})
    assert out.splitlines() == ["10\tline 10", "11\tline 11", "12\tline 12", "[lines 10-12 of 50; continue with offset=13]"]
    assert tb.run("read_file", {"path": "big.txt", "offset": 49}).endswith("[lines 49-50 of 50]")
    with pytest.raises(ToolError, match="past the end"):
        tb.run("read_file", {"path": "big.txt", "offset": 51})


def test_read_caps_at_limit(tmp_path):
    (tmp_path / "huge.txt").write_text("x\n" * (READ_LIMIT + 5), encoding="utf-8")
    out = Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "huge.txt", "limit": 10**6})
    assert out.endswith(f"[lines 1-{READ_LIMIT} of {READ_LIMIT + 5}; continue with offset={READ_LIMIT + 1}]")


def test_read_non_utf8(tmp_path):
    (tmp_path / "kr.txt").write_bytes("안녕".encode("cp949"))
    assert Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "kr.txt"}).startswith("1\t")


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("**/*.py", ".", ["src/app.py", "src/util/strings.py", "tests/test_app.py"]),
        ("*.py", "src", ["src/app.py"]),
        ("src/**/*.py", ".", ["src/app.py", "src/util/strings.py"]),
        ("**/test_*.py", ".", ["tests/test_app.py"]),
        ("*.md", ".", ["README.md"]),
    ],
)
def test_glob(repo, pattern, path, expected):
    assert repo.run("glob", {"pattern": pattern, "path": path}).splitlines() == expected


def test_glob_skips_ignored_dirs(repo):
    assert "node_modules" not in repo.run("glob", {"pattern": "**/*"})
    assert repo.run("glob", {"pattern": "**/*.rs"}) == "(no matches)"


def test_search(repo):
    assert repo.run("search", {"pattern": "hello"}).splitlines() == ["src/app.py:4: print('hello')"]
    out = repo.run("search", {"pattern": "hello", "ignore_case": True}).splitlines()
    assert out == ["src/app.py:4: print('hello')", "src/util/strings.py:1: def Hello():", "src/util/strings.py:2: return 'HELLO'"]


def test_search_filters(repo):
    assert repo.run("search", {"pattern": "import", "glob": "tests/**"}) == "tests/test_app.py:1: from src.app import main"
    assert repo.run("search", {"pattern": "def", "path": "src/util"}).splitlines()[0] == "src/util/strings.py:1: def Hello():"
    assert repo.run("search", {"pattern": "def", "path": "src/app.py"}) == "src/app.py:3: def main():"


def test_search_caps_results(tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * 250, encoding="utf-8")
    out = Toolbox(tmp_path, ["search"]).run("search", {"pattern": "hit"}).splitlines()
    assert len(out) == 201 and out[-1].startswith("[50 more matches")


def test_search_bad_regex(repo):
    with pytest.raises(ToolError, match="bad regex"):
        repo.run("search", {"pattern": "("})


def test_glob_regex():
    assert glob_regex("**/*.py").match("a/b/c.py") and glob_regex("**/*.py").match("c.py")
    assert not glob_regex("*.py").match("a/c.py")


def test_path_escape_blocked(tmp_path):
    tb = Toolbox(tmp_path, ALL)
    for name, args in [("read_file", {"path": "../secret"}), ("glob", {"pattern": "*", "path": ".."}), ("search", {"pattern": "x", "path": "../"})]:
        with pytest.raises(ToolError):
            tb.run(name, args)


def test_tool_not_granted(tmp_path):
    tb = Toolbox(tmp_path, ["read_file"])
    with pytest.raises(ToolError):
        tb.run("write_file", {"path": "x", "content": ""})
