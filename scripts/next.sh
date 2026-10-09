#!/usr/bin/env bash
# One command for working on rig with rig: update, make sure the web page is up, run.
#
#   scripts/next.sh                    # the next item in TODO.md
#   scripts/next.sh "fix the typo in README"
#   MAX_COST=5 scripts/next.sh         # cost limit in USD (default 3)
#   PORT=8001 scripts/next.sh          # rig serve port (default 8000)
set -euo pipefail

cd "$(dirname "$0")/.."
task="${*:-Do the next item in TODO.md}"
port="${PORT:-8000}"
log=".rig/serve.log"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "✗ ANTHROPIC_API_KEY가 없습니다. ~/.zshrc에 export ANTHROPIC_API_KEY=... 를 넣고 새 터미널에서 다시 실행하세요." >&2
  exit 1
fi

# 1. Update this checkout, so the rig code that runs is the newest (clears leftover merges and
#    works around conflicts; see sync-main.sh). Shifts start from the newest of local and
#    remote main anyway (publish.sync); this is for rig itself.
scripts/sync-main.sh
# Dependencies from the lockfile, wheels only (no build scripts run); rig itself runs from src/.
# Relative on purpose: hands' commands inherit it, and in a worktree it means that worktree's src/.
uv sync -q --locked --no-build --no-install-project
export PYTHONPATH=src

# 2. Start rig serve in the background unless something already answers on the port.
#    It keeps running after this script ends; stop it with: kill $(cat .rig/serve.pid)
mkdir -p .rig
if curl -s -o /dev/null "http://localhost:$port/"; then
  echo "✓ 웹 페이지 이미 켜져 있음: http://localhost:$port/"
else
  nohup uv run -q --no-sync python -m rig -f self.rig.yaml serve --port "$port" >"$log" 2>&1 &
  echo $! >.rig/serve.pid
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "http://localhost:$port/" && break
    sleep 0.2
  done
  echo "✓ 웹 페이지 켬: http://localhost:$port/  ('지난 실행' → '로그'로 진행 상황 보기; 끄기: kill \$(cat .rig/serve.pid))"
fi

# 3. Run. rig makes a branch, opens the PR, and merges it when the review and CI pass.
echo "▶ $task"
exec uv run -q --no-sync python -m rig -f self.rig.yaml run --max-cost "${MAX_COST:-3}" "$task"
