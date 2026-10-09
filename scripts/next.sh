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

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "✗ ANTHROPIC_API_KEY가 없습니다. ~/.zshrc에 export ANTHROPIC_API_KEY=... 를 넣고 새 터미널에서 다시 실행하세요." >&2
  exit 1
fi

# 1. Update this checkout, so the rig code that runs is the newest. Shifts start from the newest
#    of local and remote main anyway (publish.sync); this is for rig itself.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" != "main" ]; then
  echo "! main이 아닌 $branch 브랜치라서 git pull은 건너뜁니다."
elif ! git pull --ff-only -q origin main; then
  echo "! git pull에 실패했습니다 (로컬 변경과 충돌?). 지금 코드 그대로 실행합니다."
else
  echo "✓ main 최신으로 받음 ($(git rev-parse --short HEAD))"
fi
uv sync -q

# 2. Start rig serve in the background unless something already answers on the port.
#    It keeps running after this script ends; stop it with: kill $(cat .rig/serve.pid)
mkdir -p .rig
if curl -s -o /dev/null "http://localhost:$port/"; then
  echo "✓ 웹 페이지 이미 켜져 있음: http://localhost:$port/"
else
  nohup uv run rig -f self.rig.yaml serve --port "$port" >"$log" 2>&1 &
  echo $! >.rig/serve.pid
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "http://localhost:$port/" && break
    sleep 0.2
  done
  echo "✓ 웹 페이지 켬: http://localhost:$port/  ('지난 실행' → '로그'로 진행 상황 보기; 끄기: kill \$(cat .rig/serve.pid))"
fi

# 3. Run. rig makes a branch, opens the PR, and merges it when the review and CI pass.
echo "▶ $task"
exec uv run rig -f self.rig.yaml run --max-cost "${MAX_COST:-3}" "$task"
