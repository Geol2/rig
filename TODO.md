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
- [x] **Keep secrets out of `run` subprocesses**: commands from the `run` tool inherit the
  whole environment, including `ANTHROPIC_API_KEY`, so `uv run pytest` or any project code
  can read it (`tools.py` `_run`, `worktree.py` `env`). Drop variables whose names look
  secret by default (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`, `ANTHROPIC_*`), with a
  `run.env_passthrough` list in rig.yaml for names a project really needs. Document it; test
  that a child process can't see the key.
- [x] **Warn when the cost limit can't work**: with a model missing from `cost.PRICES`, its
  cost is unknown and `max_cost_usd` / `--max-cost` silently stops limiting (`cost.py`).
  `rig check` and the start of `rig run` (and `rig serve`) should warn, naming the hand and
  model, whenever a hand's model has no price, and say so louder when a limit is set.
- [x] **Save each hand's transcript**: `HandResult.transcript` collects the full conversation
  (tool calls and results) but is never written (`hand.py`). Write it next to the output as
  `<key>.transcript.json` (SDK blocks via `model_dump()`), including for hands that errored
  or were stopped, so failed hands can be examined afterwards. Mention it in the README.
- [x] **Final result with a parallel last stage**: in lines mode, when the last stage has
  several hands, only the last one in YAML order becomes `shift.final` and gets printed
  (`runner.py` `_run_lines`). Print every final-stage hand's output under its name, and have
  `rig report` treat them all as the summary.
  Note: shift.json `final` lists the final hands' keys; a lines shift that stops early has none.
- [x] **Validate hand names**: hand keys in rig.yaml can be anything, but they become file
  names (`runner.py` `record` writes `shift.dir / f"{stem}.md"`), so a name like `a/b` or
  `../x` writes outside the shift folder or raises only after the hand has run and been
  paid for. A `#` breaks foreman result keys (`name#n`, split in `_latest`), and a space or
  `->` can't be used in `lines`. In `spec.py` `Rig._check_lines`, reject hand names that
  aren't plain names (letters, digits, `-`, `_`), and say which name is wrong and what is
  allowed. Add the bad names to `test_invalid_rigs_rejected` and check that all templates
  and `self.rig.yaml` still load.
- [x] **Prompt tags can't be broken by their content**: task, handoff and input values are
  placed inside `<task>`, `<handoff>`, `<inputs>` tags without escaping (`runner.py`
  `build_prompt`), so content containing `</handoff>` can make the model misread the
  structure. Neutralize closing tags inside values (e.g. `</handoff>` → `<\/handoff>`) and
  test it.
  Note: closing tags of task/inputs/input/handoff/instructions become `<\/tag>` (case-insensitive, whole names only); opening tags are left as-is.
- [x] **Faster run history in `rig serve`**: the history list re-reads and re-parses every
  past shift's outputs on each request to count findings (`serve.py` `shifts`). Store the
  findings count in shift.json when a shift ends and read only shift.json in the list.
  Note: older shift.json files without `findings` fall back to counting from the outputs.
- [x] **Structured progress events**: `rig serve` gets the shift id by matching the text of
  the first progress line with a regex (`serve.py` `Run.log`), which breaks if the wording
  changes. Have `run_shift` also report structured events (at least shift started with its
  id), and use that in serve instead of the regex. Keep the CLI output the same.
  Note: `run_shift(on_status=...)` gets `ShiftEvent`s ("started" before the first line, "finished" with ok/error/stopped, not on Ctrl-C); serve takes the shift id from "started".
- [x] **CI on Python 3.11 too**: `requires-python = ">=3.11"` but CI only ran 3.13. The test
  matrix now runs 3.11 and 3.13 on Ubuntu and Windows.
  Note: `tests/test_foreman.py` had a backslash inside an f-string expression (a SyntaxError before 3.12); no other 3.12+ constructs were found. Ruff was split out (see "Needs a design first") because it needs `uv lock` and running ruff, which hands can't do.
- [x] **Worker tests for the missing paths**: `tests/test_worker.py` doesn't cover
  `pause_turn` continuing the loop, a model without a price (cost `None`, meter `unpriced`),
  or `max_tokens` ending a hand. Add those.
- [x] **Clear old tool results in long hands**: conversations only grow; a long hand (most of
  all a foreman collecting delegate results) eventually exceeds the context window and ends
  with `api_error`, losing its work. Turn on the API's tool-result clearing (context
  editing, `clear_tool_uses_20250919`) behind a `defaults` / per-hand option, on by default
  for the foreman. Check the exact request shape in the Claude API docs first.
  Note: `clear_tool_results` (defaults / per hand; foreman on by default) sends `clear_tool_uses_20250919` (trigger 100k input tokens, keep 5, clear_at_least 20k) with the `context-management-2025-06-27` beta; the transcript keeps everything.

## Backlog

From the review in shift `20261009-071333` ("해당 프로젝트의 구조나 더 개선할 방향을 찾아줘"),
in priority order. Line numbers are from that review and may have moved.

- [ ] **Lone surrogates can crash a shift**: a hand's output with a lone surrogate (e.g. from
  an undecodable file name via `list_dir`) makes the `.md` write in `runner.py` `record` and
  the `shift.json` / `running.json` writes raise `UnicodeEncodeError` (strict UTF-8 with
  `ensure_ascii=False`). Write them with `errors="replace"` like the transcript files, and
  test it.

From the review requested as "rig를 개선할만한 사항들을 찾아줘", in priority order:

- [ ] **Bounds for max_turns, max_tokens and max_delegations**: `Defaults`, `Hand` and
  `Foreman` in `spec.py` accept any int. `Rig.resolve` uses `h.max_turns or d.max_turns`, so
  `max_turns: 0` on a hand silently falls back to the default. A negative value makes the
  hand stop at once with `[stopped after -1 turns]` (`hand.py` loop), a negative
  `max_tokens` fails at the API only after the shift has started, and `max_delegations: 0`
  gives a foreman that can't delegate. Add `ge=1` to these fields so `rig check` rejects
  them, and test that each one fails validation.
- [ ] **Duplicate keys and non-UTF-8 files in rig.yaml**: `spec.load` uses `yaml.safe_load`,
  which keeps only the last of two same-named keys, so a second `coder:` under `hands`
  silently replaces the first. A rig.yaml saved in another encoding (e.g. cp949 from a Korean
  Windows editor) raises `UnicodeDecodeError`, which `cli._load_or_exit` doesn't catch, so
  the user gets a traceback. Reject duplicate mapping keys with the key and its line number,
  and have `_load_or_exit` say "save the file as UTF-8". The serve page should show these
  errors too. Tests in `test_cli.py` for both cases.
- [ ] **Catch hands that never run, and an approver that can't approve**: in foreman mode,
  hands not in `foreman.crew` never run, and `publish.approver` only has to be one of
  `hands` (`spec.py` `_check_lines`). An approver left off the crew never replies, so
  `auto_merge` can never happen and nothing says why until a whole shift has been paid
  for. In lines mode, a hand on no line runs by itself in stage 1 and no other hand gets
  its output. Make an approver outside `rig.models()` a validation error. Have `rig check`
  (and the start of `rig run`) warn about hands that aren't on the crew, or aren't on any
  line when the rig has `lines`. Test the error and the warnings.
- [ ] **`rig logs` shows status and order correctly**: the list prints `incomplete` for a shift
  that is still running (no shift.json yet) and for one that crashed or hit the cost limit
  alike (`cli.py` `cmd_logs`). `rig logs <id>` prints `*.md` in name order, so `coder-10`
  comes before `coder-2` and the final hand isn't last. It also leaves out shift.json's
  `error` and `stopped`. Show `running` (if `running.json` exists, as `serve.py` `_live`
  checks), `error`, `stopped`, `incomplete` or `ok`. In the detail view, list hands in
  shift.json `hands` order and print the error or stop reason first. Tests in
  `test_cli.py`.

## Needs a design first

Not checkboxes on purpose: `self.rig.yaml` only picks `- [ ]` items. Turn one into backlog
items once its design is decided.

- **HTML out of Python strings**: `serve.py` and `report.py` embed their HTML/CSS/JS as
  strings. Move them to package resource files (`src/rig/static/`) and load them with
  `importlib.resources`.
- **Language policy**: the `rig serve` page and refusal messages are Korean; README and CLI
  are English. Decide which is used where (or make it a setting).
- **ruff (needs a person first)**: hands can only run `uv run pytest` and git (`run.allow`),
  so they can't update uv.lock or run ruff. Someone runs `uv add --dev ruff`, adds a small
  `[tool.ruff]` config (target-version py311), runs `uv run ruff check --fix` and fixes the
  rest, then adds a `uv run --locked --no-sync ruff check` step to CI; or adds `uv run ruff`
  to `run.allow` and turns this back into a backlog item.
- **Type checking**: add mypy (or pyright) once ruff is in, and fix what it finds.
- **Compaction for very long runs**: summarize earlier turns when clearing tool results
  isn't enough (server-side compaction or rig's own summary).
- **Per-hand budgets and fallback settings**: `max_cost_usd` per hand; `fallbacks` per hand.
- **MCP servers and web tools** for hands.
- **crews**: reusable groups of hands.
- **Resume a failed shift**: rerun from the hand that failed, reusing finished hands'
  outputs from the shift directory.
