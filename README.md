# rig

Define a multi-agent harness in YAML, then run it.

```
rig init        # write a starter rig.yaml
rig check       # validate and show the stages
rig run "task"  # run a shift
rig logs        # list shifts; `rig logs last` shows the latest outputs
```

## Concepts

| Term | Meaning |
|---|---|
| **rig** | The harness, defined in `rig.yaml` |
| **hand** | One agent: a role (system prompt), a model, and tools |
| **line** | A handoff: `planner -> coder` feeds planner's output to coder |
| **foreman** | An orchestrator that delegates to hands at run time, instead of lines |
| **shift** | One run of the rig on a task, logged under `.rig/shifts/<id>/` |

Hands run in stages. A hand starts once every hand with a line into it has finished,
and hands with no path between them run in parallel.

## rig.yaml

```yaml
version: 1
name: feature-crew
workspace: .            # file tools are confined here

defaults:
  model: claude-opus-5-5
  effort: medium        # low | medium | high | xhigh | max
  max_tokens: 16000
  max_turns: 20
  fallbacks: default    # server-side refusal fallback; null disables

hands:
  planner:
    role: Write an implementation plan. Do not write code.
    tools: [list_dir, read_file]
  coder:
    role: Implement the plan.
    effort: high        # per-hand override
    tools: [list_dir, read_file, write_file]
  reviewer:
    role: Review the change.
    tools: [read_file]

lines:
  - planner -> coder -> reviewer
```

Each hand receives the task plus its upstream hands' outputs:

```
<task>…</task>
<handoff from="planner">…</handoff>
```

Built-in tools: `read_file`, `write_file`, `list_dir`.

## Foreman

Instead of fixed `lines`, a rig can have a **foreman**: an orchestrator that decides at
run time which hands to call, in what order, and how often (e.g. send the coder back
after the reviewer finds a bug). `rig init --template foreman` writes an example.

```yaml
foreman:
  effort: high
  tools: [list_dir, read_file]   # optional: look around before delegating
  crew: [coder, reviewer]        # optional: defaults to every hand
  max_delegations: 8
  # role: ...                    # optional: overrides the built-in foreman prompt

hands:
  coder:    { role: Implement what the instructions ask., tools: [read_file, write_file] }
  reviewer: { role: Review the changed files., tools: [read_file] }
```

The foreman gets a `delegate(hand, instructions)` tool and sees its crew's roles.
Each delegation starts the hand fresh with `<task>` + `<instructions from="foreman">`;
several delegations in one turn run in parallel. A rig uses either `foreman` or `lines`.
Delegated runs are logged as `<hand>-<n>.md` next to `foreman.md`.

## Setup

```bash
uv sync
uv run rig --version
```

Credentials come from `ANTHROPIC_API_KEY` or an `ant auth login` profile.
`rig run --dry` runs the whole shift without API calls.

## Roadmap

- **crews**: reusable groups of hands
- More tools (shell, web), MCP servers, per-hand budgets
