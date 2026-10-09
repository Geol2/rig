#!/usr/bin/env bash
# Run scripts/next.sh again and again, so rig keeps working through TODO.md on its own.
#
#   scripts/auto.sh                  # up to 5 runs, $10 in total
#   RUNS=10 BUDGET=20 scripts/auto.sh
#   touch .rig/stop                  # (another terminal) stop after the current run
#
# Stops when any of these happens:
#   - RUNS runs are done (default 5)
#   - the runs together cost BUDGET dollars or more (default 10)
#   - .rig/stop exists (it's removed when auto.sh starts and when it stops on it)
#   - two runs in a row end without a merged PR (failed, stopped, not approved, CI red,
#     or nothing to change), so a stuck item doesn't burn money
# Each run still has its own MAX_COST limit (default 3), as with scripts/next.sh.
set -uo pipefail

cd "$(dirname "$0")/.."
runs="${RUNS:-5}"
budget="${BUDGET:-10}"
pause="${PAUSE:-10}"          # seconds between runs
next="${NEXT:-scripts/next.sh}"
shifts=".rig/shifts"
stop=".rig/stop"

mkdir -p .rig
rm -f "$stop"
py() { if command -v python3 >/dev/null; then python3 "$@"; else uv run -q --no-sync python "$@"; fi; }

# Prints "<cost> <merged 0|1> <ok>" for a shift folder.
result() {
  py - "$1/shift.json" <<'EOF'
import json, sys
try:
    s = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    print("0 0 crashed"); raise SystemExit
cost = (s.get("totals") or {}).get("cost_usd") or 0
merged = any(p.get("merged") for p in s.get("prs") or [])
state = "ok" if s.get("ok") else ("stopped" if s.get("stopped") else ("error" if s.get("error") else "incomplete"))
print(f"{cost:.4f} {int(merged)} {state}")
EOF
}

newest() { ls -1 "$shifts" 2>/dev/null | sort | tail -n 1; }

spent=0
misses=0
count=0
for ((i = 1; i <= runs; i++)); do
  echo
  echo "━━━ 자동 실행 $i/$runs · 지금까지 \$$spent / 예산 \$$budget ━━━"
  before="$(newest)"
  "$next" "$@"
  after="$(newest)"
  if [[ -z "$after" || "$after" == "$before" ]]; then
    echo "✗ 실행 기록이 생기지 않았습니다 (설정 오류나 API 키 문제?). 자동 실행을 멈춥니다."
    exit 1
  fi
  count=$i
  read -r cost merged state <<<"$(result "$shifts/$after")"
  spent="$(py -c "print(round($spent + $cost, 2))")"
  if [[ "$merged" == 1 ]]; then
    misses=0
    echo "✓ ${i}번째 실행: PR 병합됨 (\$$cost)"
  else
    misses=$((misses + 1))
    echo "! ${i}번째 실행: 병합된 PR 없음 ($state, \$$cost)"
  fi

  if [[ -f "$stop" ]]; then
    rm -f "$stop"
    echo "■ .rig/stop 파일이 있어서 멈춥니다."
    break
  fi
  if [[ "$misses" -ge 2 ]]; then
    echo "■ 두 번 연속 병합된 PR이 없어서 멈춥니다. 웹 페이지의 로그나 'rig logs last'로 이유를 확인하세요."
    break
  fi
  if py -c "import sys; sys.exit(0 if $spent >= $budget else 1)"; then
    echo "■ 예산 \$${budget}을 다 써서 멈춥니다."
    break
  fi
  if [[ "$i" -lt "$runs" ]]; then
    echo "… ${pause}초 뒤 다음 실행 (멈추려면 다른 터미널에서: touch .rig/stop)"
    sleep "$pause"
  fi
done
echo
echo "끝: ${count}번 실행, 총 \$$spent"
