# rig

Define a multi-agent harness in YAML, then run it.

```
rig init        # write a starter rig.yaml
rig check       # validate and show the stages
rig run "task"  # run a shift
rig logs        # list shifts; `rig logs last` shows the latest outputs
rig report      # open the latest shift as a web page
rig serve       # do all of the above from a local web page
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

At the end of `rig run`, each shift prints its total tokens and an estimated USD cost.
Both are stored in `.rig/shifts/<id>/shift.json` and shown by `rig logs`. Prices live in
`src/rig/cost.py`; models not listed there show "n/a".

To cap a shift, set `max_cost_usd: 5` in rig.yaml or pass `rig run --max-cost 5`. Each
hand's done line shows the shift's running total (`· shift $1.20 of $5.00`); once the total
reaches the limit, hands stop before their next request and later stages don't start.
Requests already in flight still finish, so a shift can end slightly over the limit.

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

### Several projects at once

To work across projects in one shift (say a backend and its frontend), name them under
`workspaces` instead of setting `workspace`:

```yaml
workspaces:
  backend:  D:/work/orders-api
  frontend: D:/work/orders-web
```

The hands then see one tree whose top-level folders are those names: `list_dir .` shows
`backend/` and `frontend/`, every path starts with a name (`backend/src/...`), and `glob`
and `search` from `.` cover both projects. The tool descriptions tell the hands this.

- A hand with `run` needs `run.workspace` (one of the names) when there's more than one
  project; commands run there and their path arguments must stay inside it.
- `--worktree` makes a worktree in each git repository the projects live in (projects in
  the same repository share one) and commits each repository's changes to a branch of the
  same name, `rig/<shift-id>`. Every project must be in a git repository; rig checks that
  before creating anything.
- `rig serve` edits the list: add a row per project, name each one, and save.

### Analyzing another project

Point `workspace` at it (absolute paths work) and leave out `write_file` and `edit_file` to keep it read-only:

```yaml
workspace: D:/05_project/some-service
hands:
  analyst:
    role: Map the architecture and report risks, citing file:line.
    tools: [list_dir, glob, search, read_file]
```

For a full review there's a ready-made rig: a mapper reads the project, three reviewers
(bugs, security, db) work in parallel and return findings as JSON, and a summary hand
says what to fix first.

```bash
uv run rig -f review.rig.yaml init --template review   # then set `workspace` in the file
uv run rig -f review.rig.yaml check
uv run rig -f review.rig.yaml run "Review this project"
uv run rig -f review.rig.yaml report
```

### Reports

`rig report [shift]` (default `last`) writes `report.html` into the shift's folder and
opens it in the browser (`--no-open` to only write it). It's one self-contained file:

- the task, token use and cost, and the number of findings per severity
- the final hand's reply (or the foreman's) as the summary
- every finding from hands whose output is `{"findings": [...]}` in one table, most severe
  first, filterable by severity, hand and text; each row shows `file:line`, the problem
  and the suggested fix (fields `severity`, `file`, `line`, `title`, `detail`, `suggestion`)
- each hand's full output

### Web page

`rig serve` starts a local web page (http://localhost:8000, `--port` to change) for the rig
files in the current folder: pick one, set its `workspace`, type the task, fill in its
inputs, run it (optionally dry or in a worktree), watch progress live, and open the report
of any past shift. It runs one shift at a time. While a shift runs, the page shows its cost
so far against the limit (taken from `max_cost_usd`, editable before each run) and a
**Stop** button that ends it the same way the limit does.

Reports opened from this page have a **Fix** button on each finding. It fills the task
with that finding (location, problem, suggested fix) and picks `fix.rig.yaml`, a rig that
makes the smallest change and has a checker review it (`rig init --template fix`). If
there's none yet, the page offers to create it pointed at the same project folders as the
review. Nothing runs until you press Run; the worktree option is preselected, so the change lands on a branch you review first.

The server listens on 127.0.0.1 only, answers only requests addressed to localhost, and
requires an `X-Rig` header on every POST, so other websites can't start a run.

### inputs

Values that change from run to run go under `inputs` and are passed with `-i`:

```yaml
inputs:
  dataset:  { description: CSV to analyze, required: true }
  audience: { default: executives }
  weeks:    { type: integer, default: 4 }   # string (default) | integer | number | boolean

hands:
  writer:
    role: Write a one-page report for {{ inputs.audience }}.
```

```bash
rig run -i dataset=data/sales.csv -i weeks=8 "Weekly KPI report"
```

Every hand (and the foreman) gets them after the task, and `{{ inputs.name }}` in a
`role` is replaced with the value (empty for an optional input left unset):

```
<task>…</task>
<inputs>
<input name="dataset">data/sales.csv</input>
…
</inputs>
```

Unknown or missing required inputs, and values of the wrong type, stop `rig run` before
the shift starts; a role that references an undeclared input fails `rig check`. The values
used are stored in `shift.json`.

### output.schema

A hand with `output.schema` must end with JSON matching that JSON Schema (Claude
structured outputs). Tool calls work as usual before the final reply, and the JSON is what
downstream hands (or the foreman) receive:

```yaml
hands:
  analyst:
    role: Compute the weekly KPIs from the dataset.
    tools: [read_file, search]
    output:
      schema:
        type: object
        properties:
          kpis:
            type: array
            items:
              type: object
              properties: { name: { type: string }, value: { type: number }, change_pct: { type: number } }
              required: [name, value, change_pct]
          anomalies: { type: array, items: { type: string } }
        required: [kpis, anomalies]
```

rig adds `additionalProperties: false` to every object, which structured outputs require;
setting it to anything else is an error. Numeric and string-length constraints
(`minimum`, `maxLength`, ...) and recursive schemas aren't supported by the API.

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
