from pathlib import Path

import pytest
from pydantic import ValidationError

from rig.spec import Rig, SearchPolicy
from rig.tools import MAX_READ_CHARS, MAX_READ_LINE, READ_LIMIT, Toolbox, ToolError, crlf, glob_regex

ALL = ["write_file", "edit_file", "read_file", "list_dir", "glob", "search"]


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


def _windows_text_mode(monkeypatch):
    """Make Path.write_text translate \\n -> \\r\\n as on Windows, so a text-mode write shows up on any OS."""
    real = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is None:
            data = data.replace("\n", "\r\n")
        return real(self, data, encoding=encoding, errors=errors, newline="")

    monkeypatch.setattr(Path, "write_text", write_text)


def test_write_keeps_lf_in_new_file(tmp_path, monkeypatch):
    _windows_text_mode(monkeypatch)
    out = Toolbox(tmp_path, ALL).run("write_file", {"path": "n.txt", "content": "a\nb\n"})
    assert out == "wrote 4 chars to n.txt"
    assert (tmp_path / "n.txt").read_bytes() == b"a\nb\n"


def test_write_over_crlf_file_keeps_crlf(tmp_path, monkeypatch):
    _windows_text_mode(monkeypatch)
    (tmp_path / "w.txt").write_bytes(b"x\r\ny\r\n")
    out = Toolbox(tmp_path, ALL).run("write_file", {"path": "w.txt", "content": "a\nb\n"})
    assert out == "wrote 4 chars to w.txt"
    assert (tmp_path / "w.txt").read_bytes() == b"a\r\nb\r\n"


def test_write_crlf_content_not_doubled(tmp_path, monkeypatch):
    _windows_text_mode(monkeypatch)
    (tmp_path / "w.txt").write_bytes(b"x\r\ny\r\n")
    tb = Toolbox(tmp_path, ALL)
    for path in ("new.txt", "w.txt"):
        tb.run("write_file", {"path": path, "content": "a\r\nb\r\n"})
        data = (tmp_path / path).read_bytes()
        assert b"\r\r\n" not in data
        assert data == b"a\r\nb\r\n"


def test_write_over_lf_file_stays_lf(tmp_path, monkeypatch):
    _windows_text_mode(monkeypatch)
    (tmp_path / "l.txt").write_bytes(b"x\ny\n")
    Toolbox(tmp_path, ALL).run("write_file", {"path": "l.txt", "content": "a\nb\n"})
    assert (tmp_path / "l.txt").read_bytes() == b"a\nb\n"


def test_crlf_detection():
    assert crlf(b"a\r\nb\n")
    assert not crlf(b"a\nb\r\n")
    assert not crlf(b"no newline\r")
    assert not crlf(b"\nx")
    assert not crlf(b"")


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


def test_read_plain_file_unchanged(tmp_path):
    (tmp_path / "s.txt").write_text("a\nbb\n\nccc\n", encoding="utf-8")
    assert Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "s.txt"}) == "1\ta\n2\tbb\n3\t\n4\tccc"


def test_read_refuses_binary(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"abc\0def" + b"x" * 93)
    with pytest.raises(ToolError, match=r"binary file \(100 bytes\)"):
        Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "blob.bin"})


def test_read_cuts_long_line(tmp_path):
    (tmp_path / "min.js").write_text("y" * 100_000 + "\nshort\n", encoding="utf-8")
    out = Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "min.js"})
    first, second = out.splitlines()
    assert first == "1\t" + "y" * MAX_READ_LINE + " [… 98000 chars cut]"
    assert second == "2\tshort"
    assert len(out) < MAX_READ_LINE + 100


def test_read_caps_total_chars(tmp_path):
    (tmp_path / "wide.txt").write_text("".join(f"{i:04d}" + "z" * 1496 + "\n" for i in range(1, 201)), encoding="utf-8")
    tb = Toolbox(tmp_path, ["read_file"])
    out = tb.run("read_file", {"path": "wide.txt"})
    *rows, footer = out.splitlines()
    assert len(out) - len(footer) - 1 <= MAX_READ_CHARS
    last = int(rows[-1].split("\t")[0])
    assert last < 200 and len(rows) == last
    assert footer == f"[lines 1-{last} of 200; continue with offset={last + 1}]"
    nxt = tb.run("read_file", {"path": "wide.txt", "offset": last + 1})
    assert nxt.startswith(f"{last + 1:>3}\t{last + 1:04d}z")


def test_read_non_utf8(tmp_path):
    (tmp_path / "kr.txt").write_bytes("안녕".encode("cp949"))
    assert Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "kr.txt"}).startswith("1\t")


def test_read_non_utf8_adds_note(tmp_path):
    (tmp_path / "kr.txt").write_bytes("hi 안녕\n세상\n".encode("cp949"))
    out = Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "kr.txt"})
    *rows, note = out.splitlines()
    assert rows[0].startswith("1\thi �")
    assert len(rows) == 2
    assert note == (
        "[note: kr.txt isn't valid UTF-8 (first bad byte at offset 3); "
        "bad bytes are shown as �, and write_file/edit_file won't overwrite it]"
    )


def test_read_non_utf8_note_after_footer(tmp_path):
    (tmp_path / "kr.txt").write_bytes("a\nb\n안녕\nd\n".encode("cp949"))
    out = Toolbox(tmp_path, ["read_file"]).run("read_file", {"path": "kr.txt", "offset": 1, "limit": 2})
    assert out.splitlines()[:3] == ["1\ta", "2\tb", "[lines 1-2 of 4; continue with offset=3]"]
    assert out.splitlines()[3].startswith("[note: kr.txt isn't valid UTF-8 (first bad byte at offset 4);")


def test_read_utf8_korean_has_no_note(tmp_path):
    (tmp_path / "kr.txt").write_text("안녕\n세상\n", encoding="utf-8")
    (tmp_path / "bom.txt").write_bytes("\ufeff안녕\n".encode("utf-8"))
    tb = Toolbox(tmp_path, ["read_file"])
    assert tb.run("read_file", {"path": "kr.txt"}) == "1\t안녕\n2\t세상"
    assert "[note:" not in tb.run("read_file", {"path": "bom.txt"})


def test_write_non_utf8_refused(tmp_path):
    data = "안녕\n세상\n".encode("cp949")
    (tmp_path / "kr.txt").write_bytes(data)
    tb = Toolbox(tmp_path, ALL)
    with pytest.raises(ToolError, match="isn't valid UTF-8"):
        tb.run("write_file", {"path": "kr.txt", "content": "안녕\n세상\n"})
    assert (tmp_path / "kr.txt").read_bytes() == data
    assert tb.changed == []


def test_write_over_utf8_and_bom_files(tmp_path):
    (tmp_path / "u.txt").write_text("안녕\n", encoding="utf-8")
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbf" + "안녕\r\n".encode("utf-8"))
    tb = Toolbox(tmp_path, ALL)
    tb.run("write_file", {"path": "u.txt", "content": "세상\n"})
    tb.run("write_file", {"path": "bom.txt", "content": "\ufeff세상\n"})
    tb.run("write_file", {"path": "new.txt", "content": "새 파일\n"})
    assert (tmp_path / "u.txt").read_text(encoding="utf-8") == "세상\n"
    assert (tmp_path / "bom.txt").read_bytes() == b"\xef\xbb\xbf" + "세상\r\n".encode("utf-8")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "새 파일\n"
    assert tb.changed == ["u.txt", "bom.txt", "new.txt"]


def test_edit_single_replacement(repo):
    out = repo.run("edit_file", {"path": "src/app.py", "old": "    print('hello')", "new": "    print('bye')"})
    assert out == "replaced 1 occurrence(s) in src/app.py"
    assert (repo.workspace / "src/app.py").read_text(encoding="utf-8") == "import os\n\ndef main():\n    print('bye')\n"


def test_edit_missing_old(repo):
    with pytest.raises(ToolError, match="not found"):
        repo.run("edit_file", {"path": "src/app.py", "old": "print('nope')", "new": "x"})


def test_edit_ambiguous_old_leaves_file(tmp_path):
    (tmp_path / "a.txt").write_text("x = 1\nx = 1\n", encoding="utf-8")
    tb = Toolbox(tmp_path, ["edit_file"])
    with pytest.raises(ToolError, match="occurs 2 times"):
        tb.run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2"})
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_edit_replace_all(tmp_path):
    (tmp_path / "a.txt").write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")
    out = Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "a.txt", "old": "x = 1", "new": "x = 2", "replace_all": True})
    assert out == "replaced 2 occurrence(s) in a.txt"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x = 2\ny = 0\nx = 2\n"


def test_edit_replace_all_missing_old(tmp_path):
    (tmp_path / "a.txt").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="not found"):
        Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "a.txt", "old": "y = 1", "new": "y = 2", "replace_all": True})
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "x = 1\n"


def test_edit_keeps_crlf(tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "w.txt", "old": "one\ntwo\n", "new": "one\n2a\n2b\n"})
    assert (tmp_path / "w.txt").read_bytes() == b"one\r\n2a\r\n2b\r\nthree\r\n"


def test_edit_crlf_old_not_translated_twice(tmp_path):
    (tmp_path / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "w.txt", "old": "one\r\ntwo\r\n", "new": "one\r\n2\r\n"})
    assert (tmp_path / "w.txt").read_bytes() == b"one\r\n2\r\nthree\r\n"


def test_edit_mixed_line_endings(tmp_path):
    (tmp_path / "m.txt").write_bytes(b"a\r\nb\nc\r\n")
    Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "m.txt", "old": "b\nc", "new": "B\nC"})
    assert (tmp_path / "m.txt").read_bytes() == b"a\r\nB\nC\r\n"


def test_edit_non_utf8_refused(tmp_path):
    data = "안녕\n".encode("cp949")
    (tmp_path / "kr.txt").write_bytes(data)
    with pytest.raises(ToolError, match="UTF-8"):
        Toolbox(tmp_path, ["edit_file"]).run("edit_file", {"path": "kr.txt", "old": "\n", "new": "\r\n"})
    assert (tmp_path / "kr.txt").read_bytes() == data


def test_edit_empty_old(repo):
    with pytest.raises(ToolError, match="must not be empty"):
        repo.run("edit_file", {"path": "src/app.py", "old": "", "new": "x"})


def test_edit_missing_file(repo):
    with pytest.raises(ToolError, match="no such file"):
        repo.run("edit_file", {"path": "nope.py", "old": "a", "new": "b"})


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


def _with_extra_dirs(tmp_path, policy):
    for rel in ["build/gen/Main.java", "vendor/lib.py", ".rig/shifts/x/a.md", ".git/config"]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("hello\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("hello\n", encoding="utf-8")
    return Toolbox(tmp_path, ALL, search_policy=policy)


def test_search_ignore_adds_to_builtin(tmp_path):
    tb = _with_extra_dirs(tmp_path, SearchPolicy(ignore=["vendor"]))
    assert tb.run("search", {"pattern": "hello"}).splitlines() == ["src.py:1: hello"]
    assert "vendor" in next(d for d in tb.definitions if d["name"] == "glob")["description"]


def test_search_ignore_replaces_builtin(tmp_path):
    # build/ is searched now; .git and .rig stay skipped.
    tb = _with_extra_dirs(tmp_path, SearchPolicy(ignore=["vendor"], builtin_ignore=False))
    assert tb.run("glob", {"pattern": "**/*"}).splitlines() == ["src.py", "build/gen/Main.java"]
    search = next(d for d in tb.definitions if d["name"] == "search")["description"]
    assert search.endswith("Skipped directories in this workspace: .git, .rig, vendor.")


def test_default_definitions_unchanged(tmp_path):
    from rig.tools import DEFINITIONS

    tb = Toolbox(tmp_path, ["glob", "search"])
    assert tb.definitions == [DEFINITIONS["glob"], DEFINITIONS["search"]]


@pytest.mark.parametrize("bad", ["", "src/build", "a\\b"])
def test_search_ignore_rejects_paths(bad):
    with pytest.raises(ValidationError, match="directory names"):
        Rig.model_validate({"name": "t", "hands": {"a": {"role": "A"}}, "search": {"ignore": [bad]}})


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
    for name, args in [
        ("read_file", {"path": "../secret"}),
        ("edit_file", {"path": "../secret", "old": "a", "new": "b"}),
        ("glob", {"pattern": "*", "path": ".."}),
        ("search", {"pattern": "x", "path": "../"}),
    ]:
        with pytest.raises(ToolError):
            tb.run(name, args)


MANAGED = [".git/hooks/x", "sub/.git/config", ".RIG/shifts/a.md", ".rig/stop", ".git"]


@pytest.mark.parametrize("path", MANAGED)
def test_write_refuses_git_and_rig(tmp_path, path):
    tb = Toolbox(tmp_path, ALL)
    with pytest.raises(ToolError, match="which rig and git manage"):
        tb.run("write_file", {"path": path, "content": "evil"})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path", MANAGED)
def test_edit_refuses_git_and_rig(tmp_path, path):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("safe", encoding="utf-8")
    with pytest.raises(ToolError, match="which rig and git manage"):
        Toolbox(tmp_path, ALL).run("edit_file", {"path": path, "old": "safe", "new": "evil"})
    assert target.read_text(encoding="utf-8") == "safe"


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".gitignore", ".rigrc", "src/git/x.py"])
def test_write_and_edit_near_git_still_allowed(tmp_path, path):
    tb = Toolbox(tmp_path, ALL)
    tb.run("write_file", {"path": path, "content": "a = 1\n"})
    tb.run("edit_file", {"path": path, "old": "1", "new": "2"})
    assert (tmp_path / path).read_text(encoding="utf-8") == "a = 2\n"


def test_changed_records_successful_writes_and_edits_once(tmp_path):
    tb = Toolbox(tmp_path, ALL)
    tb.run("write_file", {"path": "./a.txt", "content": "a = 1\n"})
    tb.run("edit_file", {"path": "a.txt", "old": "1", "new": "2"})
    tb.run("write_file", {"path": "src/b.py", "content": "x"})
    tb.run("read_file", {"path": "src/b.py"})
    with pytest.raises(ToolError):
        tb.run("edit_file", {"path": "src/b.py", "old": "missing", "new": "y"})
    with pytest.raises(ToolError):
        tb.run("edit_file", {"path": "c.txt", "old": "a", "new": "b"})
    with pytest.raises(ToolError):
        tb.run("write_file", {"path": ".git/config", "content": "evil"})
    assert tb.changed == ["a.txt", "src/b.py"]


def test_changed_ignores_reads_and_failures(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    tb = Toolbox(tmp_path, ALL)
    tb.run("read_file", {"path": "a.txt"})
    with pytest.raises(ToolError):
        tb.run("edit_file", {"path": "a.txt", "old": "zzz", "new": "b"})
    with pytest.raises(ToolError):
        tb.run("write_file", {"path": ".git/x", "content": "evil"})
    assert tb.changed == []


def test_write_in_workspace_under_rig_worktrees(tmp_path):
    # --worktree mode: the workspace itself lives in .rig/worktrees/<id>.
    ws = tmp_path / ".rig" / "worktrees" / "abc"
    ws.mkdir(parents=True)
    tb = Toolbox(ws, ALL)
    tb.run("write_file", {"path": "src/x.txt", "content": "hi"})
    tb.run("edit_file", {"path": "src/x.txt", "old": "hi", "new": "ho"})
    assert (ws / "src/x.txt").read_text(encoding="utf-8") == "ho"
    with pytest.raises(ToolError, match="which rig and git manage"):
        tb.run("write_file", {"path": ".git", "content": "gitdir: elsewhere"})


def test_write_refuses_symlink_into_git(tmp_path):
    (tmp_path / ".git").mkdir()
    try:
        (tmp_path / "link").symlink_to(tmp_path / ".git", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("can't create symlinks here")
    with pytest.raises(ToolError, match="which rig and git manage"):
        Toolbox(tmp_path, ALL).run("write_file", {"path": "link/config", "content": "evil"})
    assert list((tmp_path / ".git").iterdir()) == []


def test_read_git_still_allowed(repo):
    assert repo.run("read_file", {"path": ".git/config"}) == "1\thello git"


def test_tool_not_granted(tmp_path):
    tb = Toolbox(tmp_path, ["read_file"])
    with pytest.raises(ToolError):
        tb.run("write_file", {"path": "x", "content": ""})
    (tmp_path / "x").write_text("a", encoding="utf-8")
    with pytest.raises(ToolError, match="not available"):
        tb.run("edit_file", {"path": "x", "old": "a", "new": "b"})
    assert (tmp_path / "x").read_text(encoding="utf-8") == "a"
