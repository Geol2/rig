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

`rig.yaml` must be saved as UTF-8, and a key repeated in the same mapping (say, two `coder:`
hands) is rejected with both line numbers instead of the last one silently winning.

Hands run in stages. A hand starts once every hand with a line into it has finished,
and hands with no path between them run in parallel.

At the end of `rig run`, each hand of the last stage (or the foreman) has its output printed
under its name; when the last stage runs in parallel, every one of them is printed, and a
`publish.pr` body gets each under a `## <name>` heading. Each shift also prints its total
tokens and an estimated USD cost.
Both are stored in `.rig/shifts/<id>/shift.json` and shown by `rig logs`. Prices live in
`src/rig/cost.py`; models not listed there show "n/a". A shift that crashes still writes
`shift.json`, with the exception in an `error` field. `shift.json` also keeps the total
number of findings (see [Reports](#reports)) as `findings`, which `rig serve`'s history shows.

Each hand's output is saved as `<hand>.md` in the shift directory, and its full
conversation (tool calls and results) as `<hand>.transcript.json` next to it, also for
hands that errored or were stopped, so failed hands can be examined afterwards.

To cap a shift, set `max_cost_usd: 5` in rig.yaml or pass `rig run --max-cost 5`. Each
hand's done line shows the shift's running total (`· shift $1.20 of $5.00`); once the total
reaches the limit, hands stop before their next request and later stages don't start.
Requests already in flight still finish, so a shift can end slightly over the limit.
`rig check` and the start of each run warn when a hand's model has no price; such hands'
spending isn't counted toward the cost limit.

## rig.yaml

```yaml
version: 1
name: feature-crew
workspace: .            # file tools are confined here

defaults:
  model: claude-opus-5-5
  effort: medium        # low | medium | high | xhigh | max
  max_tokens: 16000
  max_turns: 20         # at least 1 (also max_tokens, foreman.max_delegations)
  fallbacks: default    # server-side refusal fallback; null disables
  clear_tool_results: false  # clear old tool results in long conversations; on by default for the foreman

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

Hand names (the keys under `hands:`) may contain only letters (Korean included), digits, `-` and `_`; no spaces, dots, slashes or `#`.

Each hand receives the task plus its upstream hands' outputs:

```
<task>…</task>
<handoff from="planner">…</handoff>
```

A closing tag of one of these blocks inside the content (`</task>`, `</handoff>`, …) is written as `<\/task>`, `<\/handoff>`, …, so content can't end a block early.

If the model declines a request (a safety classifier refusal), the hand stops and its output
says, in Korean, which category declined it, why, and what to change; the progress output
names the hand (`✗ coder: [거절됨: reasoning_extraction] …`). The most common case,
`reasoning_extraction`, means a prompt asked the model to write out its internal reasoning
("show your thought process step by step"); drop that wording. Asking for a summary of
the result is fine. `fallbacks: default` doesn't retry that category on another model.

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
  env_passthrough: [GITHUB_TOKEN]   # secret-looking variables commands still get

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

Commands get rig's environment minus variables whose names look secret: `*_KEY`,
`*_TOKEN`, `*_SECRET`, `*PASSWORD*` and `ANTHROPIC_*` (case-insensitive), so e.g.
`ANTHROPIC_API_KEY` never reaches test code. `env_passthrough` lists names to keep
anyway, for projects whose tests really need one.

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
  same name, `rig/<shift-id>` (`-2`, `-3`, ... appended in a repository where that branch
  already exists). Every project must be in a git repository; rig checks that
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
- every last-stage hand's reply (or the foreman's) as the summary, one section per hand
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

Shifts started with `rig run` in a terminal show up too: every shift writes its progress
lines to `progress.log` in its folder (and `running.json` while it runs), and the history
list refreshes on its own and a newly started shift opens in the log panel by itself (or
press **Log** on any row). When it ends, the tab title says so, and **끝나면 알림 받기**
turns on a desktop notification.
Start `rig serve` in the folder you run rig from (the one holding `.rig/`).

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

Long conversations, above all a foreman collecting delegate results, can outgrow the
context window. With `clear_tool_results: true`, once a hand's prompt passes about 100k
input tokens the API replaces older tool results with a placeholder and keeps the latest 5.
Set it per hand or in `defaults`; it is on for the foreman unless its YAML sets
`clear_tool_results: false`. The transcript file still keeps everything.

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
   If that branch already exists (e.g. another rig root on the same repo started a shift in
   the same second), rig uses `rig/<shift-id>-2`, `-3`, ... instead.
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
Unless `publish` says otherwise (below), rig never merges or pushes; that's left to you.
`rig logs` lists each shift's branch, so you can find it again later.

### Pull requests and auto-merge

`publish` hands the branch to GitHub when the shift ends, using `git push` and the GitHub
CLI (`gh`, logged in with `gh auth login`):

```yaml
publish:
  pr: true              # push the branch and open a PR (turns on --worktree by itself)
  approver: reviewer    # this hand's last reply is posted on the PR as a review comment
  auto_merge: true      # merge when everything below holds
  # base: main          # PR target; default: the branch checked out when the shift started
  # merge_method: squash  # squash | merge | rebase
```

A PR is merged only when **all** of these hold; otherwise it stays open with the reason in
the output and in `shift.json` (`prs`):

1. the shift finished cleanly (no stop, refusal, or error);
2. a line of the approver's last reply starts with `LGTM` (`approve_word`); "not LGTM yet"
   in a sentence doesn't count;
3. every CI check on the PR passed. rig waits for them (`ci_timeout`, default 30 minutes);
   if none appear within `ci_grace` (2 minutes) it doesn't merge, unless
   `require_checks: false`.

If the base branch moved on while CI ran and the merge is refused, rig updates the PR
branch from the base (`gh pr update-branch`), waits for CI again and merges. If the PR
conflicts with the base, rig closes it with a comment saying why (the branch is kept), so
an approved PR that can't be merged isn't left open; run the task again to redo it on the
new base.

Before the shift, rig fetches the PR's base branch (`sync`, on by default) and starts from
whichever is newer: the remote (so after an auto-merged PR the next shift needs no
`git pull`) or your local branch (so a TODO item you committed but haven't pushed is
included). If they have diverged, or the fetch fails, it starts from local and says so.
Your own checkout isn't touched.

The review is posted as a comment because GitHub doesn't let an account approve its own
PR. With several workspaces, each changed repository gets its own PR. `self.rig.yaml` uses
this: the reviewer ends with a line that is just `LGTM` only when nothing must change.

## Developing rig with rig

[`self.rig.yaml`](self.rig.yaml) is a foreman rig that works on rig itself: it takes an
item from [`TODO.md`](TODO.md), has the coder implement it with tests, requires the
reviewer to sign off, and ticks the item. When the Backlog runs out (or the task asks for
ideas), the **planner** reads the code and docs and adds 3 to 5 new items first; when an
item changes the `rig serve` / `rig report` pages or CLI wording, the **designer** writes a
short spec for the coder and checks the result. Run it in worktree mode so the result is a
branch you review and merge:

```bash
uv run rig -f self.rig.yaml run --worktree "Do the next item in TODO.md"
```

The worktree starts from `HEAD`, so commit TODO.md changes before running.

`scripts/next.sh` does the whole routine in one command (macOS/Linux): update main with
`scripts/sync-main.sh`, start `rig serve` in the background if it isn't up (to watch the run under "Log"), then
run the next TODO item with a $3 cost limit. Pass a task to run something else; set
`MAX_COST` or `PORT` to change the defaults.

```bash
scripts/next.sh
scripts/next.sh "fix the typo in README"
```

`scripts/auto.sh` runs `scripts/next.sh` again and again: up to `RUNS` runs (default 5) or
`BUDGET` dollars in total (default 10), and it stops early after two runs in a row without a
merged PR, or when `.rig/stop` exists (`touch .rig/stop` from another terminal stops it after
the current run). On a Mac, `caffeinate -i scripts/auto.sh` keeps it from sleeping.

```bash
scripts/auto.sh
RUNS=10 BUDGET=20 caffeinate -i scripts/auto.sh
```

`scripts/sync-main.sh` never stops on git trouble: it cancels a merge, rebase or
cherry-pick left half-done, stashes uncommitted changes and puts them back, fast-forwards
when local main is behind, and replays unpushed local commits on top of the remote. If
those commits conflict, they're kept on a `backup/main-<time>` branch and main is set to
the remote's; if the stashed changes conflict, they stay in `git stash list`. Either way it
says what it did and how to get the kept work back.

## Roadmap

- **crews**: reusable groups of hands
- More tools (shell, web), MCP servers, per-hand budgets
