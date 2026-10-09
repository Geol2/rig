# TODO

Backlog for rig. `self.rig.yaml` takes the first unchecked item unless told otherwise:

```bash
uv run rig -f self.rig.yaml run --worktree "Do the next item in TODO.md"
```

Keep items small enough for one reviewed change. Add context under an item when it helps.

## Done

- [x] **`edit_file` tool**: replace an exact string in a file (`path`, `old`, `new`,
  optional `replace_all`), failing if `old` is missing or ambiguous. Hands currently
  rewrite whole files with `write_file`, which costs tokens and risks clobbering
  unrelated lines. Add it to BUILTIN_TOOLS, document it, and give it to coder hands in
  the templates and `self.rig.yaml`.
- [x] **Shift cost summary**: total input/output tokens per shift and an estimated USD
  cost (per-model prices in one table, unknown models shown as "n/a"). Print it at the
  end of `rig run` and store it in shift.json; show it in `rig logs`.
  Note: turns served by a server-side fallback model are priced at the hand's configured model.
- [x] **`rig logs` shows the branch** for `--worktree` shifts (from shift.json).
- [x] **Configurable ignore list** for `glob`/`search`: `search.ignore` in rig.yaml adds
  to or replaces the built-in IGNORED_DIRS (some projects keep sources in `build/`).
- [x] **`.gitattributes`** so text files are committed with LF (silences CRLF warnings).
- [x] **Stability fixes**: shift.json is written even when a shift crashes (with `error`); any tool
  exception becomes a tool error and a crashing hand no longer takes down its parallel siblings;
  shift IDs get a `-2`, `-3` suffix on same-second collisions; `rig serve` starts runs under a lock.
  Branch collisions across rig roots: see below.
- [x] **Unique worktree branch names across rig roots**: `rig/<shift-id>` is unique within
  one rig root, but two rig roots that share a git repository can start shifts in the same
  second and collide on the branch (`worktree.create` fails). If the branch already exists,
  add a `-2`, `-3` suffix the way shift ids do. Test with two roots on one repo.
  Note: with several repos, each repo may get a different suffix; `shift.branch` is the first changed repo's branch.

## Backlog

From the review in shift `20261009-071333` ("해당 프로젝트의 구조나 더 개선할 방향을 찾아줘"),
in priority order. Line numbers are from that review and may have moved.

- [ ] **Keep secrets out of `run` subprocesses**: commands from the `run` tool inherit the
  whole environment, including `ANTHROPIC_API_KEY`, so `uv run pytest` or any project code
  can read it (`tools.py` `_run`, `worktree.py` `env`). Drop variables whose names look
  secret by default (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`, `ANTHROPIC_*`), with a
  `run.env_passthrough` list in rig.yaml for names a project really needs. Document it; test
  that a child process can't see the key.
- [ ] **Warn when the cost limit can't work**: with a model missing from `cost.PRICES`, its
  cost is unknown and `max_cost_usd` / `--max-cost` silently stops limiting (`cost.py`).
  `rig check` and the start of `rig run` (and `rig serve`) should warn, naming the hand and
  model, whenever a hand's model has no price, and say so louder when a limit is set.
- [ ] **Save each hand's transcript**: `HandResult.transcript` collects the full conversation
  (tool calls and results) but is never written (`hand.py`). Write it next to the output as
  `<key>.transcript.json` (SDK blocks via `model_dump()`), including for hands that errored
  or were stopped, so failed hands can be examined afterwards. Mention it in the README.
- [ ] **Final result with a parallel last stage**: in lines mode, when the last stage has
  several hands, only the last one in YAML order becomes `shift.final` and gets printed
  (`runner.py` `_run_lines`). Print every final-stage hand's output under its name, and have
  `rig report` treat them all as the summary.
- [ ] **Prompt tags can't be broken by their content**: task, handoff and input values are
  placed inside `<task>`, `<handoff>`, `<inputs>` tags without escaping (`runner.py`
  `build_prompt`), so content containing `</handoff>` can make the model misread the
  structure. Neutralize closing tags inside values (e.g. `</handoff>` → `<\/handoff>`) and
  test it.
- [ ] **Faster run history in `rig serve`**: the history list re-reads and re-parses every
  past shift's outputs on each request to count findings (`serve.py` `shifts`). Store the
  findings count in shift.json when a shift ends and read only shift.json in the list.
- [ ] **Structured progress events**: `rig serve` gets the shift id by matching the text of
  the first progress line with a regex (`serve.py` `Run.log`), which breaks if the wording
  changes. Have `run_shift` also report structured events (at least shift started with its
  id), and use that in serve instead of the regex. Keep the CLI output the same.
- [ ] **CI on Python 3.11 too, and ruff**: `requires-python = ">=3.11"` but CI only runs 3.13.
  Add 3.11 to the test matrix, add ruff (lint) with a small config in pyproject.toml, fix
  what it reports, and run it in CI.
- [ ] **Worker tests for the missing paths**: `tests/test_worker.py` doesn't cover
  `pause_turn` continuing the loop, a model without a price (cost `None`, meter `unpriced`),
  or `max_tokens` ending a hand. Add those.
- [ ] **Clear old tool results in long hands**: conversations only grow; a long hand (most of
  all a foreman collecting delegate results) eventually exceeds the context window and ends
  with `api_error`, losing its work. Turn on the API's tool-result clearing (context
  editing, `clear_tool_uses_20250919`) behind a `defaults` / per-hand option, on by default
  for the foreman. Check the exact request shape in the Claude API docs first.

## Needs a design first

Not checkboxes on purpose: `self.rig.yaml` only picks `- [ ]` items. Turn one into backlog
items once its design is decided.

- **HTML out of Python strings**: `serve.py` and `report.py` embed their HTML/CSS/JS as
  strings. Move them to package resource files (`src/rig/static/`) and load them with
  `importlib.resources`.
- **Language policy**: the `rig serve` page and refusal messages are Korean; README and CLI
  are English. Decide which is used where (or make it a setting).
- **Type checking**: add mypy (or pyright) once ruff is in, and fix what it finds.
- **Compaction for very long runs**: summarize earlier turns when clearing tool results
  isn't enough (server-side compaction or rig's own summary).
- **Per-hand budgets and fallback settings**: `max_cost_usd` per hand; `fallbacks` per hand.
- **MCP servers and web tools** for hands.
- **crews**: reusable groups of hands.
- **Resume a failed shift**: rerun from the hand that failed, reusing finished hands'
  outputs from the shift directory.
