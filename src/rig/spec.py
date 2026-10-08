"""rig.yaml schema and loader."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

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
    # With several `workspaces`: the one commands run in (and that their path arguments must stay inside).
    workspace: str | None = None

    @field_validator("allow")
    @classmethod
    def _non_empty(cls, allow: list[str]) -> list[str]:
        if any(not a.strip() for a in allow):
            raise ValueError("run.allow entries must not be empty")
        return allow


class SearchPolicy(BaseModel):
    """Which directories `glob` and `search` skip, by name at any depth."""

    model_config = ConfigDict(extra="forbid")

    # Skipped in addition to the built-in list (or instead of it, with builtin_ignore: false).
    ignore: list[str] = Field(default_factory=list)
    # false: skip only `ignore`, e.g. for projects that keep sources in build/ or out/.
    # .git and .rig are always skipped.
    builtin_ignore: bool = True

    @field_validator("ignore")
    @classmethod
    def _dir_names(cls, ignore: list[str]) -> list[str]:
        bad = [d for d in ignore if not d.strip() or "/" in d or "\\" in d]
        if bad:
            raise ValueError(f"search.ignore takes directory names, not paths: {bad}")
        return ignore


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "claude-opus-5-5"
    effort: Effort = "medium"
    max_tokens: int = 16000
    max_turns: int = 20
    # Server-side refusal fallback ("default" routes by refusal category). null disables it.
    fallbacks: Literal["default"] | None = "default"


class InputSpec(BaseModel):
    """A value given at run time with `rig run -i name=value`."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = False
    default: str | int | float | bool | None = None

    @model_validator(mode="after")
    def _default_fits_type(self) -> InputSpec:
        if self.default is not None:
            self.check("default", self.default_text())
        return self

    def default_text(self) -> str:
        return str(self.default).lower() if isinstance(self.default, bool) else str(self.default)

    def check(self, name: str, value: str) -> str:
        """The value as hands will see it, or InputError if it isn't of this input's type."""
        try:
            if self.type == "integer":
                return str(int(value))
            if self.type == "number":
                return str(float(value))
        except ValueError:
            raise InputError(f"input {name!r} must be {'an integer' if self.type == 'integer' else 'a number'}, got {value!r}") from None
        if self.type == "boolean":
            v = value.strip().lower()
            if v not in BOOLEANS:
                raise InputError(f"input {name!r} must be true or false, got {value!r}")
            return str(BOOLEANS[v]).lower()
        return value


WORKSPACE_NAME = re.compile(r"[A-Za-z0-9_.-]+")
BOOLEANS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}
# `{{ inputs.name }}` in a role is replaced with the input's value.
INPUT_REF = re.compile(r"\{\{\s*inputs\.(\w+)\s*\}\}")


class InputError(ValueError):
    pass


class Output(BaseModel):
    """Constrains a hand's final reply to JSON matching `schema` (Claude structured outputs)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: dict[str, Any] = Field(alias="schema")

    @field_validator("schema_")
    @classmethod
    def _closed_objects(cls, schema: dict[str, Any]) -> dict[str, Any]:
        return close_objects(schema)


def close_objects(node: Any, where: str = "schema") -> Any:
    """Add `additionalProperties: false` to every object schema, which structured outputs require."""
    if isinstance(node, list):
        return [close_objects(n, f"{where}[{i}]") for i, n in enumerate(node)]
    if not isinstance(node, dict):
        return node
    out = {k: close_objects(v, f"{where}.{k}") for k, v in node.items()}
    if out.get("type") == "object":
        extra = out.setdefault("additionalProperties", False)
        if extra is not False:
            raise ValueError(f"{where}: additionalProperties must be false (structured outputs only allow closed objects)")
    return out


class Hand(BaseModel):
    """A single agent on the rig."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="System prompt describing this hand's job.")
    model: str | None = None
    effort: Effort | None = None
    max_tokens: int | None = None
    max_turns: int | None = None
    tools: list[str] = Field(default_factory=list)
    output: Output | None = None

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
    # Several projects in one shift, by name: {backend: D:/work/api, frontend: D:/work/web}.
    # Hands then see one tree whose top-level folders are these names.
    workspaces: dict[str, str] = Field(default_factory=dict)
    defaults: Defaults = Field(default_factory=Defaults)
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    hands: dict[str, Hand]
    # Edges as chains: "planner -> coder -> reviewer".
    lines: list[str] = Field(default_factory=list)
    foreman: Foreman | None = None
    run: RunPolicy = Field(default_factory=RunPolicy)
    search: SearchPolicy = Field(default_factory=SearchPolicy)

    def workspace_dirs(self, base: Path) -> dict[str, Path]:
        """Project folders by name, resolved against `base` (the rig file's folder); "." for a single workspace."""
        if self.workspaces:
            return {n: (base / p).resolve() for n, p in self.workspaces.items()}
        return {".": (base / self.workspace).resolve()}

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
        for n, h in [*self.hands.items(), ("foreman", self.foreman)]:
            unknown = {m for m in INPUT_REF.findall(h.role) if m not in self.inputs} if h else set()
            if unknown:
                raise ValueError(f"{n}'s role uses undeclared inputs {sorted(unknown)}; add them under `inputs`")
        runners = [n for n, h in [*self.hands.items(), ("foreman", self.foreman)] if h and "run" in h.tools]
        if runners and not self.run.allow:
            raise ValueError(f"{', '.join(runners)} can use `run`, but run.allow is empty; list the allowed commands")
        if self.workspaces:
            if self.workspace != ".":
                raise ValueError("use either `workspace` or `workspaces`, not both")
            bad = [n for n in self.workspaces if not WORKSPACE_NAME.fullmatch(n) or n in (".", "..")]
            if bad:
                raise ValueError(f"workspace names must be plain folder-like names (letters, digits, - _ .): {bad}")
            if runners and not self.run.workspace and len(self.workspaces) > 1:
                raise ValueError(f"{', '.join(runners)} can use `run`; set run.workspace to the project commands run in "
                                 f"({', '.join(self.workspaces)})")
        if self.run.workspace and self.run.workspace not in self.workspaces:
            raise ValueError(f"run.workspace {self.run.workspace!r} isn't one of `workspaces`")
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

    def resolve_inputs(self, given: dict[str, str]) -> dict[str, str]:
        """Checked input values with defaults filled in; raises InputError."""
        unknown = set(given) - set(self.inputs)
        if unknown:
            declared = ", ".join(self.inputs) or "none"
            raise InputError(f"unknown inputs {sorted(unknown)}; declared: {declared}")
        values: dict[str, str] = {}
        missing = []
        for name, spec in self.inputs.items():
            if name in given:
                values[name] = spec.check(name, given[name])
            elif spec.default is not None:
                values[name] = spec.check(name, spec.default_text())
            elif spec.required:
                missing.append(name)
        if missing:
            raise InputError(f"missing required inputs: {', '.join(missing)} (pass -i name=value)")
        return values

    def resolve(self, name: str, inputs: dict[str, str] | None = None) -> ResolvedHand:
        h = self.foreman if name == "foreman" and self.foreman else self.hands[name]
        d = self.defaults
        values = inputs or {}
        return ResolvedHand(
            name=name,
            # An optional input left unset reads as empty.
            role=INPUT_REF.sub(lambda m: values.get(m.group(1), ""), h.role),
            model=h.model or d.model,
            effort=h.effort or d.effort,
            max_tokens=h.max_tokens or d.max_tokens,
            max_turns=h.max_turns or d.max_turns,
            tools=h.tools,
            fallbacks=d.fallbacks,
            output_schema=h.output.schema_ if h.output else None,
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
    output_schema: dict[str, Any] | None = None


def load(path: str | Path) -> Rig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Rig.model_validate(data)
