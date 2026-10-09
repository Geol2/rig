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
    # Environment variables commands keep even though their names look secret, e.g. GITHUB_TOKEN.
    env_passthrough: list[str] = Field(default_factory=list)

    @field_validator("allow")
    @classmethod
    def _non_empty(cls, allow: list[str]) -> list[str]:
        if any(not a.strip() for a in allow):
            raise ValueError("run.allow entries must not be empty")
        return allow

    @field_validator("env_passthrough")
    @classmethod
    def _env_names(cls, names: list[str]) -> list[str]:
        if any(not n.strip() for n in names):
            raise ValueError("run.env_passthrough entries must not be empty")
        return names


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


class Publish(BaseModel):
    """After a --worktree shift: open a pull request for its branch, and optionally merge it."""

    model_config = ConfigDict(extra="forbid")

    pr: bool = False                 # push the branch and open a PR (turns on --worktree)
    base: str | None = None          # PR target; default: the branch checked out when the shift started
    remote: str = "origin"
    # Fetch the remote first and start from whichever of local and remote is newer, so a
    # shift after an auto-merged PR doesn't need a `git pull`.
    sync: bool = True
    # The hand whose last reply is posted on the PR and must approve before a merge.
    approver: str | None = None
    approve_word: str = "LGTM"
    auto_merge: bool = False         # merge once the shift is ok, the approver approved, and CI passed
    merge_method: Literal["squash", "merge", "rebase"] = "squash"
    require_checks: bool = True      # no CI checks on the PR means no merge
    ci_timeout: int = Field(default=1800, ge=30)
    ci_grace: int = Field(default=120, ge=0)   # how long to wait for checks to appear
    ci_poll: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _merge_needs_pr(self) -> Publish:
        if self.auto_merge and not self.pr:
            raise ValueError("publish.auto_merge needs publish.pr: true")
        if self.auto_merge and not self.approver:
            raise ValueError("publish.auto_merge needs publish.approver (the hand that must approve, e.g. reviewer)")
        return self


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "claude-opus-5-5"
    effort: Effort = "medium"
    max_tokens: int = Field(default=16000, ge=1)
    max_turns: int = Field(default=20, ge=1)
    # Server-side refusal fallback ("default" routes by refusal category). null disables it.
    fallbacks: Literal["default"] | None = "default"
    # Let the API clear old tool results once the prompt grows large (see hand.CLEAR_TOOL_RESULTS).
    clear_tool_results: bool = False


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
# Hand names become file names in the shift folder and `name#n` foreman keys:
# Unicode letters (Korean included), digits, `_` and `-` only.
HAND_NAME = re.compile(r"[\w-]+")
BOOLEANS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}
# `{{ inputs.name }}` in a role is replaced with the input's value.
INPUT_REF = re.compile(r"\{\{\s*inputs\.(\w+)\s*\}\}")


class InputError(ValueError):
    pass


class RigFileError(ValueError):
    """rig.yaml can't be read as written: not UTF-8, or a repeated key."""


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
    max_tokens: int | None = Field(default=None, ge=1)
    max_turns: int | None = Field(default=None, ge=1)
    # None uses defaults.clear_tool_results.
    clear_tool_results: bool | None = None
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
    # The foreman's conversation grows with every delegate result, so clearing is on unless set false.
    clear_tool_results: bool | None = True
    # Hands the foreman may delegate to; empty means all of them.
    crew: list[str] = Field(default_factory=list)
    # Hands that must run after the last delegation to any other hand before the
    # foreman may finish, e.g. [reviewer]. Enforced by the harness, not just the prompt.
    require: list[str] = Field(default_factory=list)
    max_delegations: int = Field(default=12, ge=1)


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
    # Stop the shift once its estimated cost reaches this many USD (see cost.Meter).
    max_cost_usd: float | None = Field(default=None, gt=0)
    publish: Publish = Field(default_factory=Publish)

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

    def models(self) -> dict[str, str]:
        """{hand: model} for the hands that can run: the foreman and its crew, or every hand on the lines."""
        names = ["foreman", *self.crew] if self.foreman else list(self.hands)
        return {n: self.resolve(n).model for n in names}

    @property
    def edges(self) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        for line in self.lines:
            names = [n.strip() for n in line.split("->")]
            edges.extend(zip(names, names[1:]))
        return edges

    def hand_warnings(self) -> list[str]:
        """Warnings about hands that can never run, or whose output no other hand gets."""
        if self.foreman:
            if not self.foreman.crew:
                return []
            off = [n for n in self.hands if n not in self.foreman.crew]
            return [f"⚠ hands not on foreman.crew never run: {', '.join(off)}; add them to the crew or remove them"] if off else []
        if not self.lines:
            return []
        on_lines = {n for edge in self.edges for n in edge}
        alone = [n for n in self.hands if n not in on_lines]
        if not alone:
            return []
        return [f"⚠ hands on no line run alone in stage 1 and no hand gets their output: {', '.join(alone)}; "
                "add them to a line or remove them"]

    @model_validator(mode="after")
    def _check_lines(self) -> Rig:
        if not self.hands:
            raise ValueError("a rig needs at least one hand")
        bad = [n for n in self.hands if not HAND_NAME.fullmatch(n)]
        if bad:
            raise ValueError(f"hand names must be plain names (letters, digits, - _): {bad}")
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
        if self.publish.approver and self.publish.approver not in self.hands:
            raise ValueError(f"publish.approver {self.publish.approver!r} isn't one of the hands")
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
            if self.publish.approver and self.publish.approver not in self.crew:
                raise ValueError(f"publish.approver {self.publish.approver!r} isn't on foreman.crew, so it never runs "
                                 "and can't approve; add it to the crew")
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
            # Not `or`: an explicit false must win over defaults true.
            clear_tool_results=h.clear_tool_results if h.clear_tool_results is not None else d.clear_tool_results,
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
    clear_tool_results: bool = False
    output_schema: dict[str, Any] | None = None


MERGE_TAG = "tag:yaml.org,2002:merge"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that records where each mapping key was written, as node.key_marks parallel to node.value."""

    def compose_mapping_node(self, anchor: str | None) -> yaml.MappingNode:
        # Same as Composer.compose_mapping_node, plus key_marks: an aliased key composes to the anchored
        # node, whose start_mark is the anchor's line, so take the mark from the key's own event instead.
        start_event = self.get_event()
        tag = start_event.tag
        if tag is None or tag == "!":
            tag = self.resolve(yaml.MappingNode, None, start_event.implicit)
        node = yaml.MappingNode(tag, [], start_event.start_mark, None, flow_style=start_event.flow_style)
        node.key_marks = []
        if anchor is not None:
            self.anchors[anchor] = node
        while not self.check_event(yaml.MappingEndEvent):
            node.key_marks.append(self.peek_event().start_mark)
            item_key = self.compose_node(node, None)
            item_value = self.compose_node(node, item_key)
            node.value.append((item_key, item_value))
        node.end_mark = self.get_event().end_mark
        return node


def _check_duplicates(loader: yaml.SafeLoader, node: yaml.Node, where: str, seen: set[int]) -> None:
    """Raise RigFileError for the first mapping that repeats a key, before merges flatten anything."""
    if id(node) in seen:  # aliases point at nodes already checked
        return
    seen.add(id(node))
    if isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _check_duplicates(loader, item, f"{where}[{i}]", seen)
    elif isinstance(node, yaml.MappingNode):
        first: dict[Any, yaml.Mark] = {}
        marks = getattr(node, "key_marks", None) or [key_node.start_mark for key_node, _ in node.value]
        for (key_node, _), mark in zip(node.value, marks):
            if key_node.tag == MERGE_TAG or not isinstance(key_node, yaml.ScalarNode):
                continue
            key = loader.construct_object(key_node)
            try:
                repeated = key in first
            except TypeError:  # unhashable
                continue
            if repeated:  # by value, so an aliased key (the same node) counts too
                inside = f" in {where}" if where else ""
                raise RigFileError(
                    f'duplicate key "{key}"{inside} at line {mark.line + 1} '
                    f"(first at line {first[key].line + 1}); remove or rename one"
                )
            first[key] = mark
        for key_node, value_node in node.value:
            if key_node.tag == MERGE_TAG:
                _check_duplicates(loader, value_node, where, seen)
                continue
            name = key_node.value if isinstance(key_node, yaml.ScalarNode) else "?"
            _check_duplicates(loader, value_node, f"{where}.{name}" if where else str(name), seen)


def parse_yaml(text: str) -> Any:
    """yaml.safe_load, but a mapping that repeats a key raises RigFileError instead of keeping the last one."""
    loader = _StrictLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_duplicates(loader, node, "", set())
        return loader.construct_document(node)
    finally:
        loader.dispose()


def read_rig_text(path: str | Path) -> str:
    """The file as text; RigFileError if it isn't UTF-8 (we don't guess the encoding)."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        line = data.count(b"\n", 0, e.start) + 1
        column = e.start - data.rfind(b"\n", 0, e.start)
        raise RigFileError(
            f"not UTF-8 (byte 0x{data[e.start]:02x} at line {line}, column {column}); save the file as UTF-8"
        ) from None


def load(path: str | Path) -> Rig:
    data = parse_yaml(read_rig_text(path)) or {}
    return Rig.model_validate(data)
