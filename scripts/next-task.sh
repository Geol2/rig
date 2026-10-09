#!/usr/bin/env bash
# Print the task for the next run: the first unchecked item under "## Backlog" in TODO.md,
# by name, so the run, its PR and the history on the web page say what is being done.
# With an empty Backlog, the task asks the planner for new items first.
#
#   scripts/next-task.sh [TODO.md]
set -uo pipefail

todo="${1:-TODO.md}"
title="$(sed -n '/^## Backlog/,/^## /p' "$todo" 2>/dev/null \
  | grep -m 1 '^- \[ \] \*\*' \
  | sed -E 's/^- \[ \] \*\*(([^*]|\*[^*])+)\*\*.*/\1/')"

if [[ -n "$title" ]]; then
  echo "TODO 항목 처리: $title"
  echo
  echo "TODO.md의 Backlog에서 \"$title\" 항목 하나만 처리해줘. 끝나면 체크하고 Done으로 옮겨줘."
else
  echo "TODO 새 항목 기획 후 첫 항목 처리"
  echo
  echo "TODO.md의 Backlog에 남은 항목이 없어. planner에게 새 항목을 추가하게 하고, 그중 첫 번째를 처리해줘."
fi
