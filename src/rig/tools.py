"""Built-in tools a hand can be given. All paths are confined to the workspace."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

DEFINITIONS: dict[str, dict[str, Any]] = {
    "read_file": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the workspace."}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    "write_file": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file in the workspace.",
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
}


class ToolError(Exception):
    pass


Handler = Callable[[dict[str, Any]], Awaitable[str]]


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

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.names:
            raise ToolError(f"tool not available to this hand: {name}")
        if name == "read_file":
            p = self._path(args["path"])
            if not p.is_file():
                raise ToolError(f"no such file: {args['path']}")
            return p.read_text(encoding="utf-8")
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
        raise ToolError(f"unknown tool: {name}")
