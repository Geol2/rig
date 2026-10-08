"""Built-in tools a hand can be given. All paths are confined to the workspace."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

from rig.spec import RunPolicy, SearchPolicy

# Directories skipped by `glob` and `search`: VCS data, dependencies, build output.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".rig",
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "target", "build", "dist", "out", ".gradle", ".next", ".nuxt",
}
# Skipped even with `search.builtin_ignore: false`: repo internals and rig's own shifts and worktrees.
ALWAYS_IGNORED = {".git", ".rig"}
READ_LIMIT = 2000        # lines per read_file call
GLOB_LIMIT = 500         # paths per glob call
SEARCH_LIMIT = 200       # matching lines per search call
MAX_LINE = 300           # chars kept per search result line
MAX_SEARCH_BYTES = 2_000_000

DEFINITIONS: dict[str, dict[str, Any]] = {
    "read_file": {
        "name": "read_file",
        "description": (
            f"Read a text file from the workspace. Lines come back numbered ('  12\\tcode'). "
            f"Returns at most {READ_LIMIT} lines; for large files, use offset/limit to read the part you need "
            "(find it first with `search`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "offset": {"type": "integer", "description": "1-based line to start from. Default 1."},
                "limit": {"type": "integer", "description": f"Number of lines to read. Default and max {READ_LIMIT}."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "write_file": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file in the workspace. Write plain content, without line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "edit_file": {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a UTF-8 text file in the workspace. `old` must occur exactly once "
            "unless replace_all is true; include enough surrounding lines to make it unique. "
            "Prefer this over write_file for changing existing files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "old": {
                    "type": "string",
                    "description": (
                        "Exact text to replace. Must match the file exactly, including whitespace and "
                        "indentation, without the line numbers read_file adds."
                    ),
                },
                "new": {"type": "string", "description": "Replacement text."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring exactly one. Default false.",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "list_dir": {
        "name": "list_dir",
        "description": "List entries of a directory in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory relative to the workspace; '.' for the root."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "glob": {
        "name": "glob",
        "description": (
            "Find files by path pattern, e.g. '**/*.java', 'src/**/test_*.py', '*.md'. "
            "'**' matches any number of directories. Skips dependency and build directories "
            f"(node_modules, .git, target, ...). Returns up to {GLOB_LIMIT} paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern relative to `path`."},
                "path": {"type": "string", "description": "Directory to search from. Default '.'."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "search": {
        "name": "search",
        "description": (
            "Search file contents with a regular expression (Python `re` syntax), like grep. "
            "Returns 'path:line: text' for each matching line, up to "
            f"{SEARCH_LIMIT} matches. Skips binary files and dependency/build directories. "
            "Use `glob` to narrow by file type, e.g. '**/*.ts'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to find."},
                "path": {"type": "string", "description": "File or directory to search. Default '.'."},
                "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '**/*.py'."},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive match. Default false."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


def run_definition(policy: RunPolicy) -> dict[str, Any]:
    allowed = "\n".join(f"- {a}" for a in policy.allow)
    return {
        "name": "run",
        "description": (
            "Run a command in the workspace root and get its exit code and combined stdout/stderr. "
            "There is no shell: pipes, redirects, `&&`, `;` and variables don't work, so run one command per call "
            "(several calls in one turn run in parallel). Use forward slashes in paths. "
            f"Times out after {policy.timeout}s. Only commands starting with one of these are allowed:\n{allowed}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command line, e.g. 'uv run pytest -q tests'."}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def ignored_dirs(policy: SearchPolicy) -> set[str]:
    return (IGNORED_DIRS if policy.builtin_ignore else set()) | ALWAYS_IGNORED | set(policy.ignore)


def search_definition(name: str, ignored: set[str]) -> dict[str, Any]:
    """`glob` or `search` with the directories it actually skips spelled out."""
    d = DEFINITIONS[name]
    if ignored == IGNORED_DIRS:
        return d
    return {**d, "description": f"{d['description']} Skipped directories in this workspace: {', '.join(sorted(ignored))}."}


# Unquoted shell operators `run` refuses: commands execute without a shell, so these
# would silently become literal arguments instead of doing what the model intended.
SHELL_CHARS = "();<>|&"


class ToolError(Exception):
    pass


Handler = Callable[[dict[str, Any]], Awaitable[str]]


def describe(name: str, args: dict[str, Any]) -> str:
    """A short one-line summary of a tool call for progress output."""
    path = args.get("path", "")
    if name == "read_file":
        offset, limit = args.get("offset"), args.get("limit")
        span = f" :{offset or 1}+{limit}" if limit else (f" :{offset}" if offset else "")
        detail = f"{path}{span}"
    elif name == "write_file":
        detail = f"{path} ({len(args.get('content', ''))} chars)"
    elif name == "glob":
        detail = args.get("pattern", "") + (f" in {path}" if path not in ("", ".") else "")
    elif name == "search":
        detail = repr(args.get("pattern", "")) + (f" in {path}" if path not in ("", ".") else "")
        if args.get("glob"):
            detail += f" ({args['glob']})"
    elif name == "run":
        detail = args.get("command", "")
    else:
        detail = path or ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{name:<10} {detail}"[:120]


def _tail(text: str, limit: int) -> str:
    # Keep the end: test summaries and errors usually come last.
    if len(text) <= limit:
        return text
    return f"[{len(text) - limit} earlier chars cut]\n" + text[-limit:]


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob with `**` support into a regex over '/'-separated relative paths."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Toolbox:
    def __init__(
        self,
        workspace: Path,
        names: list[str],
        extra: dict[str, tuple[dict[str, Any], Handler]] | None = None,
        run_policy: RunPolicy | None = None,
        env: dict[str, str] | None = None,
        on_call: Callable[[str], None] | None = None,
        search_policy: SearchPolicy | None = None,
    ):
        self.workspace = workspace.resolve()
        self.names = names
        # Run-time tools such as the foreman's `delegate`: name -> (definition, async handler).
        self.extra = extra or {}
        self.run_policy = run_policy or RunPolicy()
        # Directory names `glob` and `search` skip.
        self.ignored = ignored_dirs(search_policy or SearchPolicy())
        # Environment for `run` subprocesses; None inherits rig's own.
        self.env = env
        # Progress callback: one line per built-in tool call (run-time tools log themselves).
        self.on_call = on_call

    @property
    def definitions(self) -> list[dict[str, Any]]:
        builtins = [
            run_definition(self.run_policy) if n == "run"
            else search_definition(n, self.ignored) if n in ("glob", "search")
            else DEFINITIONS[n]
            for n in self.names
        ]
        return builtins + [d for d, _ in self.extra.values()]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        if name in self.extra:
            return await self.extra[name][1](args)
        if self.on_call:
            self.on_call(describe(name, args))
        start = time.monotonic()
        try:
            result = await asyncio.to_thread(self.run, name, args)
        except ToolError as e:
            if self.on_call:
                self.on_call(f"  ✗ {str(e).splitlines()[0][:100]}")
            raise
        if self.on_call and name == "run":
            # Commands can take a while; report how it ended, e.g. "[exit code 1]".
            self.on_call(f"  → {result.split(chr(10), 1)[0].strip('[]')} ({time.monotonic() - start:.0f}s)")
        return result

    def _path(self, rel: str) -> Path:
        p = (self.workspace / rel).resolve()
        if not p.is_relative_to(self.workspace):
            raise ToolError(f"path escapes workspace: {rel}")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.workspace).as_posix()

    def _files(self, root: Path) -> Iterator[Path]:
        """Files under root (or root itself), skipping ignored directories, in sorted order."""
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.ignored)
            for f in sorted(filenames):
                yield Path(dirpath) / f

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.names:
            raise ToolError(f"tool not available to this hand: {name}")
        if name == "read_file":
            return self._read(args["path"], args.get("offset") or 1, args.get("limit") or READ_LIMIT)
        if name == "write_file":
            p = self._path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"wrote {len(args['content'])} chars to {args['path']}"
        if name == "edit_file":
            return self._edit(args["path"], args["old"], args["new"], bool(args.get("replace_all")))
        if name == "list_dir":
            p = self._path(args["path"])
            if not p.is_dir():
                raise ToolError(f"no such directory: {args['path']}")
            return "\n".join(sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())) or "(empty)"
        if name == "glob":
            return self._glob(args["pattern"], args.get("path") or ".")
        if name == "search":
            return self._search(args["pattern"], args.get("path") or ".", args.get("glob"), bool(args.get("ignore_case")))
        if name == "run":
            return self._run(args["command"])
        raise ToolError(f"unknown tool: {name}")

    def _run(self, command: str) -> str:
        policy = self.run_policy
        try:
            # punctuation_chars splits unquoted operators into their own tokens; quoted text stays intact.
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            if any(set(tok) <= set(SHELL_CHARS) for tok in lexer) or "`" in command.replace("\\`", ""):
                raise ToolError("shell syntax isn't supported (no pipes, redirects, &&, ;, $()); run one command per call")
            argv = shlex.split(command)
        except ValueError as e:
            raise ToolError(f"can't parse command: {e}") from e
        if not argv:
            raise ToolError("empty command")
        if not any(argv[: len(p)] == p for p in (shlex.split(a) for a in policy.allow)):
            raise ToolError(f"command not allowed: {command!r}; allowed prefixes: {policy.allow}")
        for arg in argv[1:]:
            # Reject path-like arguments that reach outside the workspace.
            value = arg.split("=", 1)[-1] if arg.startswith("-") else arg
            if ".." in Path(value).parts or Path(value).is_absolute():
                if not (self.workspace / value).resolve().is_relative_to(self.workspace):
                    raise ToolError(f"argument points outside the workspace: {arg}")
        exe = shutil.which(argv[0])
        if exe is None:
            raise ToolError(f"executable not found: {argv[0]}")

        try:
            proc = subprocess.run(
                [exe, *argv[1:]],
                cwd=self.workspace,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=policy.timeout,
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.output or b"").decode("utf-8", errors="replace")
            return f"[timed out after {policy.timeout}s]\n{_tail(partial, policy.max_output)}"
        output = proc.stdout.decode("utf-8", errors="replace")
        return f"[exit code {proc.returncode}]\n{_tail(output, policy.max_output)}"

    def _read(self, rel: str, offset: int, limit: int) -> str:
        p = self._path(rel)
        if not p.is_file():
            raise ToolError(f"no such file: {rel}")
        if offset < 1 or limit < 1:
            raise ToolError("offset and limit must be >= 1")
        limit = min(limit, READ_LIMIT)
        # errors="replace": non-UTF-8 sources (e.g. CP949) still read instead of failing.
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return "(empty file)"
        if offset > len(lines):
            raise ToolError(f"offset {offset} is past the end ({len(lines)} lines)")
        end = min(offset - 1 + limit, len(lines))
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}\t{lines[i - 1]}" for i in range(offset, end + 1))
        if offset > 1 or end < len(lines):
            body += f"\n[lines {offset}-{end} of {len(lines)}" + (f"; continue with offset={end + 1}]" if end < len(lines) else "]")
        return body

    def _edit(self, rel: str, old: str, new: str, replace_all: bool) -> str:
        p = self._path(rel)
        if not p.is_file():
            raise ToolError(f"no such file: {rel}")
        if not old:
            raise ToolError("old must not be empty")
        try:
            # Strict decoding: read_file replaces bad bytes, so writing that text back would corrupt the file.
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            raise ToolError(f"{rel} isn't valid UTF-8; edit_file can't edit it safely") from e
        count = text.count(old)
        # read_file shows lines without \r, so if the literal text misses in a CRLF file, retry with CRLF line ends.
        # Literal first, so files with mixed line endings stay editable.
        if count == 0 and "\r\n" in text and "\n" in old and "\r" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            raise ToolError(f"old string not found in {rel}; re-read the file and match whitespace exactly")
        if count > 1 and not replace_all:
            raise ToolError(f"old string occurs {count} times in {rel}; add surrounding context to make it unique or set replace_all")
        n = count if replace_all else 1
        p.write_bytes(text.replace(old, new, n).encode("utf-8"))
        return f"replaced {n} occurrence(s) in {rel}"

    def _glob(self, pattern: str, rel: str) -> str:
        root = self._path(rel)
        if not root.is_dir():
            raise ToolError(f"no such directory: {rel}")
        rx = glob_regex(pattern)
        hits = [p for p in self._files(root) if rx.match(p.relative_to(root).as_posix())]
        if not hits:
            return "(no matches)"
        out = "\n".join(self._rel(p) for p in hits[:GLOB_LIMIT])
        if len(hits) > GLOB_LIMIT:
            out += f"\n[{len(hits) - GLOB_LIMIT} more not shown; use a narrower pattern]"
        return out

    def _search(self, pattern: str, rel: str, glob: str | None, ignore_case: bool) -> str:
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from e
        root = self._path(rel)
        if not root.exists():
            raise ToolError(f"no such path: {rel}")
        name_rx = glob_regex(glob) if glob else None

        out: list[str] = []
        more = 0
        for p in self._files(root):
            if name_rx and not name_rx.match(p.relative_to(root).as_posix() if root.is_dir() else p.name):
                continue
            try:
                if p.stat().st_size > MAX_SEARCH_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    if len(out) < SEARCH_LIMIT:
                        out.append(f"{self._rel(p)}:{n}: {line.strip()[:MAX_LINE]}")
                    else:
                        more += 1
        if not out:
            return "(no matches)"
        if more:
            out.append(f"[{more} more matches not shown; narrow the pattern, path, or glob]")
        return "\n".join(out)
