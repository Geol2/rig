"""rig.yaml schema and loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Effort = Literal["low", "medium", "high", "xhigh", "max"]

BUILTIN_TOOLS = {"read_file", "write_file", "edit_file", "list_dir", "glob", "search", "run"}


class RunPolicy(BaseModel):
    """What the `run` tool may execute. Commands run in the workspace without a shell."""

    model_config = ConfigDict(extra="forbid")

    # Command prefixes, e.g. "uv run pytest" allows "uv run pytest -q tests/x.py".
    allow: list[str] = Field(default_factory=list)
    timeout: int = Field(default=120, ge=1, le=3600)
    max_output: int = Field(default=20000, ge=1000)

    @field_validator("allow")
    @classmethod
    def _non_empty(cls, allow: list[str]) -> list[str]:
        if any(not a.strip() for a in allow):
            raise ValueError("run.allow entries must not be empty")
        return allow


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "claude-opus-5-5"
    effort: Effort = "medium"
    max_tokens: int = 16000
    max_turns: int = 20
    # Server-side refusal fallback ("default" routes by refusal category). null disables it.
    fallbacks: Literal["default"] | None = "default"


class Hand(BaseModel):
    """A single agent on the rig."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="System prompt describing this hand's job.")
    model: str | None = None
    effort: Effort | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    tools: list[str] = Field(default_factory=list)

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, tools: list[str]) -> list[str]:
        unknown = set(tools) - BUILTIN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}; available: {sorted(BUILTIN_TOOLS)}")
        return tools


DEFAULT_FOREMAN_ROLE = """\
You are the foreman of a crew of agents ("hands"). You do not do the work yourself:
break the task into pieces, hand each piece to the right hand with the `delegate` tool,
check what comes back, and send follow-up work when something is missing or wrong.
Each delegation starts the hand fresh, so give it everything it needs in the instructions,
including relevant results from other hands. Delegate independent pieces in parallel.
When the task is done, reply with the final result for the user."""


class Foreman(Hand):
    """An orchestrator that delegates to hands at run time instead of following fixed lines."""

    role: str = DEFAULT_FOREMAN_ROLE
    # Hands the foreman may delegate to; empty means all of them.
    crew: list[str] = Field(default_factory=list)
    # Hands that must run after the last delegation to any other hand before the
    # foreman may finish, e.g. [reviewer]. Enforced by the harness, not just the prompt.
    require: list[str] = Field(default_factory=list)
    max_delegations: int = 12


class Rig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    name: str
    description: str = ""
    workspace: str = "."
    defaults: Defaults = Field(default_factory=Defaults)
    hands: dict[str, Hand]
    # Edges as chains: "planner -> coder -> reviewer".
    lines: list[str] = Field(default_factory=list)
    foreman: Foreman | None = None
    run: RunPolicy = Field(default_factory=RunPolicy)

    @property
    def crew(self) -> list[str]:
        if self.foreman and self.foreman.crew:
            return self.foreman.crew
        return list(self.hands)

    @property
    def edges(self) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        for line in self.lines:
            names = [n.strip() for n in line.split("->")]
            edges.extend(zip(names, names[1:]))
        return edges

    @model_validator(mode="after")
    def _check_lines(self) -> Rig:
        if not self.hands:
            raise ValueError("a rig needs at least one hand")
        runners = [n for n, h in [*self.hands.items(), ("foreman", self.foreman)] if h and "run" in h.tools]
        if runners and not self.run.allow:
            raise ValueError(f"{', '.join(runners)} can use `run`, but run.allow is empty; list the allowed commands")
        if self.foreman:
            if self.lines:
                raise ValueError("use either `foreman` or `lines`, not both")
            if "foreman" in self.hands:
                raise ValueError("'foreman' is reserved; rename that hand")
            unknown = set(self.foreman.crew) - set(self.hands)
            if unknown:
                raise ValueError(f"foreman.crew references unknown hands {sorted(unknown)}")
            missing = set(self.foreman.require) - set(self.crew)
            if missing:
                raise ValueError(f"foreman.require lists hands not on the crew: {sorted(missing)}")
        for line in self.lines:
            names = [n.strip() for n in line.split("->")]
            if len(names) < 2 or any(not n for n in names):
                raise ValueError(f"bad line {line!r}; expected 'a -> b [-> c ...]'")
            for n in names:
                if n not in self.hands:
                    raise ValueError(f"line {line!r} references unknown hand {n!r}")
        # Imported here to avoid a cycle; raises on cycles.
        from rig.graph import layers

        layers(list(self.hands), self.edges)
        return self

    def resolve(self, name: str) -> ResolvedHand:
        h = self.foreman if name == "foreman" and self.foreman else self.hands[name]
        d = self.defaults
        return ResolvedHand(
            name=name,
            role=h.role,
            model=h.model or d.model,
            effort=h.effort or d.effort,
            max_tokens=h.max_tokens or d.max_tokens,
            max_turns=h.max_turns or d.max_turns,
            tools=h.tools,
            fallbacks=d.fallbacks,
        )


class ResolvedHand(BaseModel):
    name: str
    role: str
    model: str
    effort: Effort
    max_tokens: int
    max_turns: int
    tools: list[str]
    fallbacks: Literal["default"] | None


def load(path: str | Path) -> Rig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Rig.model_validate(data)
