"""Built-in tools a hand can be given. All paths are confined to the workspace."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

# Directories skipped by `glob` and `search`: VCS data, dependencies, build output.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".rig",
    "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    "target", "build", "dist", "out", ".gradle", ".next", ".nuxt",
}
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


class ToolError(Exception):
    pass


Handler = Callable[[dict[str, Any]], Awaitable[str]]


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
    ):
        self.workspace = workspace.resolve()
        self.names = names
        # Run-time tools such as the foreman's `delegate`: name -> (definition, async handler).
        self.extra = extra or {}

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [DEFINITIONS[n] for n in self.names] + [d for d, _ in self.extra.values()]

    async def call(self, name: str, args: dict[str, Any]) -> str:
        if name in self.extra:
            return await self.extra[name][1](args)
        return await asyncio.to_thread(self.run, name, args)

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
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
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
        if name == "list_dir":
            p = self._path(args["path"])
            if not p.is_dir():
                raise ToolError(f"no such directory: {args['path']}")
            return "\n".join(sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())) or "(empty)"
        if name == "glob":
            return self._glob(args["pattern"], args.get("path") or ".")
        if name == "search":
            return self._search(args["pattern"], args.get("path") or ".", args.get("glob"), bool(args.get("ignore_case")))
        raise ToolError(f"unknown tool: {name}")

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
