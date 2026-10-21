"""Reading a project's TODO.md for the `rig serve` page: its open backlog items, in order.

The format is the one this repository's TODO.md uses (and `scripts/next-task.sh` reads):
`## Backlog` / `## Done` / other `##` sections, items as `- [ ] **title**: details` with
indented continuation lines, `- [x]` once done.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ITEM = re.compile(r"^- (?:\[( |x|X)\] )?(.*)$")
TITLE = re.compile(r"^\*\*(.+?)\*\*[:：]?\s*(.*)$", re.S)
MAX_BYTES = 2_000_000


def parse(text: str) -> dict[str, Any]:
    """{"backlog": [{"title", "detail"}] (open items under ## Backlog, in order), "done": n
    (checked items anywhere), "later": [titles] (items under any other section, unchecked)}."""
    section, items, current = "", [], None
    for line in text.splitlines():
        if line.startswith("## "):
            section, current = line[3:].strip().lower(), None
            continue
        m = ITEM.match(line)
        if m:
            current = {"section": section, "done": (m.group(1) or " ").lower() == "x",
                       "checkbox": m.group(1) is not None, "lines": [m.group(2)]}
            items.append(current)
        elif current is not None and line[:1] in (" ", "\t") and line.strip():
            current["lines"].append(line.strip())
        elif not line.strip():
            current = None
    backlog, later, done = [], [], 0
    for it in items:
        if it["done"]:
            done += 1
            continue
        text_ = " ".join(it["lines"]).strip()
        m = TITLE.match(text_)
        title, detail = (m.group(1).strip(), m.group(2).strip()) if m else (text_, "")
        if it["section"] == "backlog" and it["checkbox"]:
            backlog.append({"title": title, "detail": detail})
        elif it["section"] not in ("done", "backlog"):
            later.append(title)
    return {"backlog": backlog, "done": done, "later": later}


def find(dirs: dict[str, Path]) -> tuple[str, Path] | None:
    """(shown name, path) of the first TODO.md directly in one of the rig's project folders."""
    for name, d in dirs.items():
        path = d / "TODO.md"
        if path.is_file():
            return ("TODO.md" if name == "." else f"{name}/TODO.md"), path
    return None


def newer_upstream(path: Path) -> tuple[str, str] | None:
    """(upstream branch, TODO.md as committed there) when the folder's branch is behind its
    upstream: shifts merge their PRs on the remote and fetch it, but don't update this checkout,
    so its TODO.md still lists items that are already done. None otherwise (or without git)."""
    from rig.worktree import GitError, git

    cwd = path.parent
    try:
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=cwd).strip()
        if git("rev-parse", "HEAD", cwd=cwd).strip() == git("rev-parse", "@{u}", cwd=cwd).strip():
            return None
        git("merge-base", "--is-ancestor", "HEAD", "@{u}", cwd=cwd)  # fails unless HEAD is behind
        return upstream, git("show", f"@{{u}}:./{path.name}", cwd=cwd)
    except (GitError, OSError):
        return None


def read(dirs: dict[str, Path]) -> dict[str, Any]:
    hit = find(dirs)
    if hit is None:
        return {"found": False}
    shown, path = hit
    newer = newer_upstream(path)
    if newer:
        upstream, text = newer
        return {"found": True, "file": shown, "from": upstream, **parse(text[:MAX_BYTES])}
    try:
        raw = path.read_bytes()[:MAX_BYTES]
    except OSError:
        return {"found": False}
    return {"found": True, "file": shown, "from": None, **parse(raw.decode("utf-8", errors="replace"))}
