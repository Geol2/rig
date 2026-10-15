#!/usr/bin/env bash
# One command for working on rig with rig: update, make sure the web page is up, run.
#
#   scripts/next.sh                    # the next item in TODO.md, by name (scripts/next-task.sh)
#   scripts/next.sh "fix the typo in README"
#   MAX_COST=8 scripts/next.sh         # cost limit in USD (default: max_cost_usd in self.rig.yaml)
#   PORT=8001 scripts/next.sh          # rig serve port (default 8000)
#   같은 task의 최근 shift가 중단됐으면 이어 받음(최대 두 번; scripts/resume-target.py)
set -euo pipefail

# Windows can't replace a file that a running script holds open, and sync-main.sh may update
# this very script; so it runs from a copy under .rig/run/ (harmless on macOS/Linux).
root="$(cd "$(dirname "$0")/.." && pwd)"
me="$(basename "$0")"
if [[ "${RIG_COPY_OF:-}" != "$me" ]]; then
  copy="$root/.rig/run/${me%.sh}"
  mkdir -p "$copy" && cp "$root"/scripts/*.sh "$copy/"
  RIG_COPY_OF="$me" RIG_ROOT="$root" exec bash "$copy/$me" "$@"
fi
here="$(cd "$(dirname "$0")" && pwd)"  # the copy's folder: helper scripts that git may replace run from here
cd "$RIG_ROOT"
port="${PORT:-8000}"
log=".rig/serve.log"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "✗ ANTHROPIC_API_KEY가 없습니다. 등록한 뒤 새 터미널에서 다시 실행하세요." >&2
  echo "  macOS(zsh): echo 'export ANTHROPIC_API_KEY=\"sk-ant-...\"' >> ~/.zshrc" >&2
  echo "  Windows: setx ANTHROPIC_API_KEY \"sk-ant-...\"  (Git Bash만이면 ~/.bashrc에 export)" >&2
  exit 1
fi

# 1. Update this checkout, so the rig code that runs is the newest (clears leftover merges and
#    works around conflicts; see sync-main.sh). Shifts start from the newest of local and
#    remote main anyway (publish.sync); this is for rig itself.
"$here/sync-main.sh"
# The task: what was passed, or else the next TODO.md item by name (read after the update).
if [[ $# -gt 0 ]]; then task="$*"; else task="$(scripts/next-task.sh)"; fi
# Dependencies from the lockfile, wheels only (no build scripts run); rig itself runs from src/.
# Relative on purpose: hands' commands inherit it, and in a worktree it means that worktree's src/.
# --inexact: leave an installed rig (Scripts/rig.exe) alone; on Windows it's locked while any
# `uv run rig ...` is running, and removing it would fail.
uv sync -q --locked --no-build --no-install-project --inexact
export PYTHONPATH=src

# 2. Start rig serve in the background unless it's already up with this code. One started from
#    older code (or by an older next.sh, which didn't record it) is stopped and started again,
#    so the page has the latest features. Something else on the port is left alone.
mkdir -p .rig
code="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
rig_page() { curl -s "http://localhost:$port/" 2>/dev/null | grep -q '<title>rig</title>'; }
stop_port() {
  if command -v powershell.exe >/dev/null 2>&1; then  # Windows: the server is a python.exe, not rig.exe
    powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort $1 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
  elif command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true)"
    [[ -n "$pids" ]] && kill $pids 2>/dev/null
  fi
  for _ in $(seq 1 25); do curl -s -o /dev/null "http://localhost:$1/" || return 0; sleep 0.2; done
}
if rig_page && [[ "$(cat .rig/serve.commit 2>/dev/null)" != "$code" ]]; then
  echo "↻ 웹 페이지를 새 코드로 다시 켭니다"
  stop_port "$port"
fi
if curl -s -o /dev/null "http://localhost:$port/"; then
  echo "✓ 웹 페이지 이미 켜져 있음: http://localhost:$port/"
else
  nohup uv run -q --no-sync python -m rig -f self.rig.yaml serve --port "$port" >"$log" 2>&1 &
  echo $! >.rig/serve.pid
  echo "$code" >.rig/serve.commit
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "http://localhost:$port/" && break
    sleep 0.2
  done
  echo "✓ 웹 페이지 켬: http://localhost:$port/  ('지난 실행' → '로그'로 진행 상황 보기)"
fi

# 3. Run. rig makes a branch, opens the PR, and merges it when the review and CI pass.
# Cost limit: MAX_COST if given, else max_cost_usd from self.rig.yaml (the web page uses that too).
cost=()
if [[ -n "${MAX_COST:-}" ]]; then cost=(--max-cost "$MAX_COST"); fi
echo "▶ $(head -n 1 <<<"$task")"
# A stopped shift of the same task, or one whose PR CI failed, is continued instead (`--resume`
# takes no task). resume-target.py says "↻ …" on stderr, which goes straight to the terminal.
resume="$(uv run -q --no-sync python scripts/resume-target.py self.rig.yaml "$task" || true)"
if [[ -n "$resume" ]]; then
  exec uv run -q --no-sync python -m rig -f self.rig.yaml run ${cost[@]+"${cost[@]}"} --resume "$resume"
fi
exec uv run -q --no-sync python -m rig -f self.rig.yaml run ${cost[@]+"${cost[@]}"} "$task"
