#!/usr/bin/env bash
# Restart the rig web page with the newest code: update this checkout, stop the page that's
# running, and serve again in this window (Ctrl+C stops it).
#
#   scripts/restart.sh                 # Windows: scripts\restart.cmd
#   PORT=8001 scripts/restart.sh
#   FORCE=1 scripts/restart.sh         # even while a run started on the page is in progress
#                                      # (it's stopped and saved first, then can be resumed)
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
here="$(cd "$(dirname "$0")" && pwd)"
cd "$RIG_ROOT"
port="${PORT:-8000}"

rig_page() { curl -s "http://localhost:$port/" 2>/dev/null | grep -q '<title>rig</title>'; }
# A run started on the page lives inside that server: stopping it stops the run.
page_busy() { curl -s "http://localhost:$port/api/run" 2>/dev/null | grep -q '"status": "running"\|"others": \["'; }
stop_port() {
  if command -v powershell.exe >/dev/null 2>&1; then  # Windows: the server is a python.exe, not rig.exe
    powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort $1 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }" >/dev/null 2>&1
  elif command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true)"
    [[ -n "$pids" ]] && kill $pids 2>/dev/null
  fi
  for _ in $(seq 1 25); do curl -s -o /dev/null "http://localhost:$1/" || return 0; sleep 0.2; done
}

if rig_page && page_busy && [[ -z "${FORCE:-}" ]]; then
  echo "✗ 웹 페이지에서 실행 중인 작업이 있어서 다시 켜지 않았습니다. 끝난 뒤 다시 실행하세요." >&2
  echo "  지금 꼭 다시 켜야 하면: FORCE=1 scripts/restart.sh  (Windows: scripts\\restart.cmd force)" >&2
  exit 1
fi

"$here/sync-main.sh"
uv sync -q --locked --no-build --no-install-project --inexact
export PYTHONPATH=src
if curl -s -o /dev/null "http://localhost:$port/"; then
  echo "■ 켜져 있던 웹 페이지(포트 $port)를 끕니다"
  stop_port "$port"
fi
mkdir -p .rig
git rev-parse HEAD >.rig/serve.commit 2>/dev/null || true
echo "▶ 웹 페이지를 새 코드로 켭니다: http://localhost:$port/  (이 창을 닫거나 Ctrl+C로 끔)"
exec uv run -q --no-sync python -m rig -f self.rig.yaml serve --port "$port"
