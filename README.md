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

At the end of a shift rig prints the tokens used and an estimated cost, and stores both
in `shift.json` (`usage`, plus `cost_usd` per hand); `rig logs` lists the cost per shift:

```
usage 48,210 in / 6,904 out tok · cache 312,800 read / 41,500 write · est. $0.6010
```

The estimate uses Claude API list prices from the table in `src/rig/pricing.py`, priced
by the model that served each request (so a refusal fallback is priced as the fallback model).
A model missing from the table makes the cost `n/a`.

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
    tools: [list_dir, read_file, write_file, edit_file]
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

Built-in tools (all confined to `workspace`):

| Tool | What it does |
|---|---|
| `list_dir` | List a directory |
| `glob` | Find files by pattern (`**/*.java`, `src/**/test_*.py`) |
| `search` | Regex search over file contents, like grep; optional `glob` filter and `ignore_case` |
| `read_file` | Read a file with line numbers; `offset`/`limit` for large files (max 2000 lines per call) |
| `write_file` | Create or overwrite a file |
| `edit_file` | Replace an exact string in a file (`old` → `new`); fails if `old` is missing or appears more than once, unless `replace_all` |
| `run` | Run an allowed command (tests, linters, `git diff`) and get exit code + output |

### run

`run` only executes commands that start with an entry in `run.allow`:

```yaml
run:
  allow: ["uv run pytest", "git status", "git diff", "git log"]
  timeout: 120        # seconds per command
  max_output: 20000   # chars kept (the tail, where test summaries are)

hands:
  tester:
    role: Run the tests and report failures.
    tools: [run, read_file]
```

Commands run in the workspace with no shell, so pipes, redirects, `&&`, `;` and `$()`
are rejected. Arguments pointing outside the workspace are rejected too. A failing command
isn't a tool error: the hand gets `[exit code N]` and the output and decides what to do.
Allow only what the hands need, since an allowed program can do anything it supports
(e.g. allowing `python` allows any script).

`glob` and `search` skip dependency and build directories (`.git`, `node_modules`,
`.venv`, `target`, `build`, `dist`, ...) and binary files. `search` in rig.yaml changes
which directories (matched by name at any depth) are skipped:

```yaml
search:
  ignore: [vendor, generated]   # skipped as well as the built-in list
  builtin_ignore: false         # optional: skip only `ignore`, e.g. when sources live in build/
```

`.git` and `.rig` are always skipped. When the list differs from the default, the `glob`
and `search` tool descriptions name the skipped directories so the hands know.

### Analyzing another project

Point `workspace` at it (absolute paths work) and leave out `write_file` and `edit_file` to keep it read-only:

```yaml
workspace: D:/05_project/some-service
hands:
  analyst:
    role: Map the architecture and report risks, citing file:line.
    tools: [list_dir, glob, search, read_file]
```

## Foreman

Instead of fixed `lines`, a rig can have a **foreman**: an orchestrator that decides at
run time which hands to call, in what order, and how often (e.g. send the coder back
after the reviewer finds a bug). `rig init --template foreman` writes an example.

```yaml
foreman:
  effort: high
  tools: [list_dir, read_file]   # optional: look around before delegating
  crew: [coder, reviewer]        # optional: defaults to every hand
  require: [reviewer]            # optional: must check the latest work before finishing
  max_delegations: 8
  # role: ...                    # optional: overrides the built-in foreman prompt

hands:
  coder:    { role: Implement what the instructions ask., tools: [read_file, write_file, edit_file] }
  reviewer: { role: Review the changed files., tools: [read_file] }
```

The foreman gets a `delegate(hand, instructions)` tool and sees its crew's roles.
Each delegation starts the hand fresh with `<task>` + `<instructions from="foreman">`;
several delegations in one turn run in parallel. A rig uses either `foreman` or `lines`.
Delegated runs are logged as `<hand>-<n>.md` next to `foreman.md`.

`require` is enforced by the harness, not just the prompt: each listed hand must
finish successfully after the last delegation to any other hand. If the foreman tries to
finish before that, rig tells it what's missing and the loop continues. If the delegation
limit runs out first, the shift ends but is marked incomplete.

## Setup

```bash
uv sync
uv run rig --version
```

Credentials come from `ANTHROPIC_API_KEY` or an `ant auth login` profile.
`rig run --dry` runs the whole shift without API calls.

## Worktree mode

`rig run --worktree "task"` keeps the hands' changes off your working tree:

1. Creates branch `rig/<shift-id>` from `HEAD` and a git worktree for it under `.rig/worktrees/`.
2. Runs the shift there (`workspace` is mapped to the same path inside the worktree).
3. Commits whatever the hands changed to that branch and removes the worktree.
   No changes → the branch is deleted. If the commit fails, the worktree is kept.

```
worktree: committed 4184be9 on rig/20261008-163910
   CHANGELOG.md | 3 +++
review:  git diff ecf0f25..rig/20261008-163910
merge:   git merge rig/20261008-163910    discard: git branch -D rig/20261008-163910
```

Uncommitted changes in your working tree are not carried into the worktree (rig warns).
rig never merges or pushes; that's left to you. `rig logs` lists each shift's branch,
so you can find it again later.

## Developing rig with rig

[`self.rig.yaml`](self.rig.yaml) is a foreman rig that works on rig itself: it takes an
item from [`TODO.md`](TODO.md), has the coder implement it with tests, requires the
reviewer to sign off, and ticks the item. Run it in worktree mode so the result is a
branch you review and merge:

```bash
uv run rig -f self.rig.yaml run --worktree "Do the next item in TODO.md"
```

The worktree starts from `HEAD`, so commit TODO.md changes before running.

## Roadmap

- **crews**: reusable groups of hands
- More tools (shell, web), MCP servers, per-hand budgets
