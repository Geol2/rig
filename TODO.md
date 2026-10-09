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
- [x] **Lone surrogates can crash a shift**: a hand's output with a lone surrogate (e.g. from
  an undecodable file name via `list_dir`) makes the `.md` write in `runner.py` `record` and
  the `shift.json` / `running.json` writes raise `UnicodeEncodeError` (strict UTF-8 with
  `ensure_ascii=False`). Write them with `errors="replace"` like the transcript files, and
  test it.
  Note: the `.md`, shift.json, running.json and progress.log writes, `rig serve` responses and the CLI's stdout now use `errors="replace"` (a surrogate becomes "?").
- [x] **Bounds for max_turns, max_tokens and max_delegations**: `Defaults`, `Hand` and
  `Foreman` in `spec.py` accept any int. `Rig.resolve` uses `h.max_turns or d.max_turns`, so
  `max_turns: 0` on a hand silently falls back to the default. A negative value makes the
  hand stop at once with `[stopped after -1 turns]` (`hand.py` loop), a negative
  `max_tokens` fails at the API only after the shift has started, and `max_delegations: 0`
  gives a foreman that can't delegate. Add `ge=1` to these fields so `rig check` rejects
  them, and test that each one fails validation.
  Note: `defaults.max_tokens`/`max_turns`, each hand's (and the foreman's) `max_tokens`/`max_turns`, and `foreman.max_delegations` now need `ge=1`; `rig check` rejects 0 or negative values with the field path.
- [x] **Duplicate keys and non-UTF-8 files in rig.yaml**: `spec.load` uses `yaml.safe_load`,
  which keeps only the last of two same-named keys, so a second `coder:` under `hands`
  silently replaces the first. A rig.yaml saved in another encoding (e.g. cp949 from a Korean
  Windows editor) raises `UnicodeDecodeError`, which `cli._load_or_exit` doesn't catch, so
  the user gets a traceback. Reject duplicate mapping keys with the key and its line number,
  and have `_load_or_exit` say "save the file as UTF-8". The serve page should show these
  errors too. Tests in `test_cli.py` for both cases.
  Note: spec.load raises RigFileError (duplicate key with its path and both lines, aliases included; non-UTF-8 with byte/line/column and "save the file as UTF-8"); `<<` merge overrides aren't duplicates; a UTF-8 BOM is fine; rig serve lists undecodable rig files with the error.
- [x] **Catch hands that never run, and an approver that can't approve**: in foreman mode,
  hands not in `foreman.crew` never run, and `publish.approver` only has to be one of
  `hands` (`spec.py` `_check_lines`). An approver left off the crew never replies, so
  `auto_merge` can never happen and nothing says why until a whole shift has been paid
  for. In lines mode, a hand on no line runs by itself in stage 1 and no other hand gets
  its output. Make an approver outside `rig.models()` a validation error. Have `rig check`
  (and the start of `rig run`) warn about hands that aren't on the crew, or aren't on any
  line when the rig has `lines`. Test the error and the warnings.
  Note: in foreman mode the approver must be on the crew (validation error); `Rig.hand_warnings()` (hands off the crew / on no line) is printed before the price warnings in `rig check` and at the start of `rig run`.
- [x] **`rig logs` shows status and order correctly**: the list prints `incomplete` for a shift
  that is still running (no shift.json yet) and for one that crashed or hit the cost limit
  alike (`cli.py` `cmd_logs`). `rig logs <id>` prints `*.md` in name order, so `coder-10`
  comes before `coder-2` and the final hand isn't last. It also leaves out shift.json's
  `error` and `stopped`. Show `running` (if `running.json` exists, as `serve.py` `_live`
  checks), `error`, `stopped`, `incomplete` or `ok`. In the detail view, list hands in
  shift.json `hands` order and print the error or stop reason first. Tests in
  `test_cli.py`.
  Note: the running.json check moved to `runner.live()` (shared by serve and cli); the list shows running / error / stopped / ok / incomplete (error wins over stopped, a dead pid's running.json is incomplete); `rig logs <id>` prints the error, the stop reason and "still running" before the cost line, then hands in shift.json order (`coder#10` heading) and leftover .md in natural order.
- [x] **`write_file`/`edit_file`로 `.git`·`.rig` 수정 금지**: `tools.py` `Toolbox._path`는
  workspace 밖으로 나가는 경로만 막아서, hand가 `write_file`로 `.git/hooks/pre-commit`이나
  `.git/config`(`core.hooksPath`, `core.fsmonitor`)를 쓸 수 있습니다. 그러면 `run.allow`에 없는
  임의 코드가 다음 `git commit`이나 허용된 `git status` 때 실행되어 `run.allow` 제한이 무의미해지고,
  `.rig/`에 쓰면 다른 shift의 기록(`shift.json`, `running.json`)이나 `.rig/stop`을 바꿀 수 있습니다
  (`glob`/`search`는 이미 `ALWAYS_IGNORED`로 이 둘을 건너뜀). `write_file`과 `edit_file`(`Toolbox.run`,
  `_edit`)에서 경로의 어느 구성요소든 `.git` 또는 `.rig`이면(대소문자 무시, `workspaces`의 프로젝트
  경로 포함) "rig가 관리하는 폴더라 쓸 수 없다"는 ToolError로 거부하세요. 읽기는 그대로 둡니다.
  README의 Built-in tools 표 근처에 한 줄 추가. 완료 기준: `tests/test_tools.py`에 `.git/hooks/x`,
  `sub/.git/config`, `.RIG/shifts/a.md`, `workspaces` 모드의 `backend/.git/config` 쓰기·편집이
  거부되고 파일이 생기지 않는지, `.github/workflows/ci.yml`이나 `.gitignore`는 여전히 쓸 수 있는지 테스트.
  Note: `Toolbox._writable` checks the resolved path relative to its root (the project root in `workspaces` mode), so a `--worktree` workspace under `.rig/worktrees/` still writes and a symlink into `.git` is refused; the check runs before `write_file` creates folders, and `_path` callers are unchanged (it now wraps `_locate`, which returns root and path).
- [x] **`shift.json`을 원자적으로 쓰고, 깨진 파일 하나가 목록 전체를 망가뜨리지 않게**: `runner.py`
  `_write_summary`는 `write_text`로 바로 덮어써서(shift당 최대 두 번, publish 후 한 번 더) 쓰는 도중
  프로세스가 죽거나 디스크가 차면 반쯤 쓰인 JSON이 남습니다. 그러면 `cli.py` `_read_summary`(`rig logs`
  목록), `serve.py` `App.shifts`(history 전체가 오류)와 `App.shift_log`, `report.py` `collect`가 모두
  `json.loads`에서 예외를 내고, 한 폴더 때문에 `rig logs`가 traceback으로 끝나고 `rig serve` history가
  비게 됩니다. 같은 폴더의 임시 파일에 쓴 뒤 `os.replace`로 바꾸고, 네 곳의 읽기는 공용 함수 하나로
  모아 읽을 수 없는 `shift.json`을 `{}`로 다루세요(`rig logs` 목록 상태는 `unreadable`, 상세 보기는
  그 사실을 맨 앞에 출력). 완료 기준: `test_cli.py`와 `test_serve.py`에서 잘린 `shift.json`이 있는
  폴더가 섞여 있어도 `rig logs`, `rig logs <id>`, `App.shifts()`, `rig report`가 동작하는지, 그리고
  `_write_summary`가 임시 파일을 남기지 않는지 테스트.
  Note: `_write_summary` writes `shift.json.tmp` then `os.replace`s it (removed on failure); `report.read_summary` (used by `rig logs`, `rig report`, `App.shifts`, `App.shift_log`) returns `{}` plus a reason for a broken file; `rig logs` shows `unreadable` and the detail view starts with "✗ cannot read shift.json (…); showing hand outputs only". The serve log viewer pill still shows 완료 for `ok: null` (serve.py, `s.ok === false ? 'incomplete' : 'done'`) — a separate fix.
- [x] **중단된 shift 이어 하기: `rig run --resume <shift-id|last>`**: 지금은 shift가 중간에 멈추면
  (Ctrl+C, 비용 상한, 에러) `runner.py` `run_shift`의 `finally`가 hand들이 쓴 내용을 `rig/<id>` 브랜치에
  커밋하고 shift.json에 `error`/`stopped`를 남기지만, 다음 실행은 그걸 쓰지 않고 base 브랜치에서 같은 일을
  처음부터 다시 합니다(비용 중복, 브랜치만 쌓임). `--resume`을 주면: 그 shift의 shift.json에서 task·inputs·
  브랜치(`worktrees[].branch`, 커밋이 있었던 것)를 읽고, base 대신 **그 브랜치 끝에서** 새 worktree를 만들어
  새 shift로 실행합니다(새 shift id, shift.json에 `resumed_from: <id>`). 이어 하는 hand(foreman 모드면
  foreman, lines 모드면 첫 stage)의 프롬프트 앞에 `<resumed>` 블록으로 "이전 실행이 중단됨(이유), 이전 shift의
  hand별 결과 요약(각 `<hand>.md` 앞부분), 브랜치에 이미 있는 변경(`git diff --stat base..branch`)"을 넣고
  "이미 된 부분은 다시 하지 말고 남은 부분만 마무리"라고 지시합니다. 끝난(ok) shift나 브랜치가 없는 shift를
  주면 이유를 말하고 종료. `worktree.py`에 기존 브랜치에서 worktree를 만드는 경로가 필요합니다(새 브랜치
  이름은 지금처럼 `rig/<새 id>`, 시작점만 다름). README에 한 단락. 완료 기준: `tests/`에 Ctrl+C로 끊긴
  shift(가짜 worker로 KeyboardInterrupt)를 `--resume last`로 이어 받아 새 worktree가 이전 브랜치 커밋을
  포함하고, 프롬프트에 `<resumed>`가 들어가고, `resumed_from`이 기록되는 테스트; ok인 shift는 거부되는 테스트.
  Note: `runner.load_resume` reads task, inputs and the committed branches from the old shift.json and `run_shift(resume=…)` starts each repo's new `rig/<new id>` branch from the old branch tip (implies `--worktree`, no remote sync). shift.json records `resumed_from`, and per worktree `from_branch` and `origin_base`, so a chained resume keeps repos the middle shift didn't change (it continues from their `from_branch`) and the `<resumed>` diff stat reaches back to the first shift's base. The `<resumed>` block (reason, hand reply excerpts, diff stat) goes to the foreman or the first lines stage only. Refused, creating nothing: unknown id or no shifts, still running, unreadable shift.json, finished ok, another rig, a kept worktree, no usable branch (with a hint to resume the `resumed_from` shift), a deleted branch, or branches outside the rig's repos.

## Backlog

Requested by the owner ("중간에 그만두고 기억을 하고 계속되는 건 돼나"), first:

- [ ] **`scripts/next.sh`가 중단된 작업을 자동으로 이어 받기**: 위 항목이 끝난 뒤. `next.sh`가 task를
  정할 때(`scripts/next-task.sh` 다음), `.rig/shifts`의 가장 최근 shift가 **같은 task**이고 ok가 아니며
  (`error`/`stopped`/`incomplete`) 브랜치에 커밋이 있고 그 뒤로 병합된 PR이 없으면 새로 시작하는 대신
  `rig run --resume <그 id>`로 실행하고, 터미널에 "↻ 중단된 작업 이어 하기: <id>"를 한국어로 출력합니다.
  같은 shift를 두 번 넘게 이어 하지 않도록(이어 한 shift도 또 실패하면 새로 시작) `resumed_from` 사슬을
  확인하세요. **CI 실패로 PR이 열린 채 남은 경우도 포함**: 직전 shift가 ok였어도 shift.json `prs`에 병합되지
  않고 닫히지도 않은 PR이 있으면(이유가 `CI failed: …`), 같은 브랜치에서 이어 받아 "CI 실패(검사 이름) 고치기"를
  지시하고, 그 브랜치에 push하면 같은 PR이 갱신되게 하세요(#46/#47처럼 같은 항목을 처음부터 다시 해서
  PR이 두 개 생기는 일을 막기 위함). `scripts/auto.sh`의 "두 번 연속 실패면 멈춤"은 그대로. 완료 기준: `tests/test_scripts.py`에
  가짜 shift 폴더로 "이어 받음 / 새로 시작(같은 task 아님, ok였음, 이미 두 번 이어 함)"을 확인하는 테스트.

From the review requested as "rig를 개선할만한 사항들을 찾아줘", in priority order:

- [ ] **input 이름 검증과 치환되지 않는 `{{ … }}` 잡기**: `spec.py`의 `inputs` 키는 아무 문자열이나
  허용되지만 `INPUT_REF`는 `\w+`만 찾습니다. 그래서 `due-date` 같은 input을 role에서
  `{{ inputs.due-date }}`로 쓰면 치환도 안 되고 `_check_lines`의 미선언 input 검사에도 안 걸려, 모델이
  중괄호 문자 그대로를 받습니다. `{{ input.x }}`(단수) 같은 오타도 같은 식으로 조용히 남고, 이름에 `"`가
  있으면 `runner.py` `build_prompt`의 `<input name="…">` 속성이 깨집니다. `Rig._check_lines`에서 input
  이름을 `\w+`(fullmatch)로 제한해 어떤 이름이 틀렸는지 말하고, role(foreman 포함)에 `INPUT_REF`에
  맞지 않는 `{{ … inputs … }}`/`{{ input… }}` 형태가 있으면 해당 hand와 원문을 짚어 거부하세요. README
  inputs 절에 이름 규칙 한 줄. 완료 기준: `tests/test_inputs.py`에 하이픈·공백·따옴표 이름, `{{ inputs.due-date }}`,
  `{{ input.x }}`가 `rig check`에서 거부되는 테스트와, 모든 템플릿과 `self.rig.yaml`이 여전히 로드되는지 확인.
- [ ] **CLI 인자 검증: 중복 `-i`와 shift id 경로**: `cli.py` `_parse_inputs`는 `-i n=a -i n=b`에서
  앞 값을 조용히 버립니다. `cmd_logs`와 `cmd_report`는 `shifts_dir / args.shift`를 그대로 써서
  `rig report ..`가 `.rig/report.html`을 쓰고 `rig logs ../..`가 shift가 아닌 폴더의 `*.md`를
  출력합니다(`serve.py` `App._shift_dir`은 이미 부모 폴더를 확인함). 같은 이름이 두 번 오면
  "input 'n' given twice"로 종료하고, shift id는 `_shift_dir`처럼 `.rig/shifts` 바로 아래 폴더인지
  확인하는 공용 함수로 고르세요(`last`는 그대로). 완료 기준: `tests/test_cli.py`에 중복 `-i`, `..`,
  `../..`, 존재하지 않는 id가 각각 명확한 메시지로 종료되는지 테스트.

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
