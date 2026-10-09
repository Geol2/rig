#!/usr/bin/env bash
# Bring local main up to date with the remote, without ever stopping on git trouble.
#
#   scripts/sync-main.sh [remote]      # default: origin
#
# - A merge, rebase or cherry-pick left half-done is cancelled (rig's changes arrive through
#   PRs, so a local merge in progress is almost always a leftover).
# - Uncommitted changes to tracked files are stashed first and put back afterwards.
# - Local main behind the remote: fast-forward. Ahead (commits not pushed yet): kept as is.
# - Both have new commits: local commits are replayed on top of the remote. If that conflicts,
#   they're kept on a backup/main-<time> branch and main is set to the remote's.
# Always exits 0 so the caller can go on with whatever code is there.
set -uo pipefail

remote="${1:-origin}"
git_dir="$(git rev-parse --git-dir)" || exit 0

if [[ -f "$git_dir/MERGE_HEAD" ]]; then
  what="$(head -n 1 "$git_dir/MERGE_MSG" 2>/dev/null)"
  git merge --abort && echo "! 끝나지 않은 병합을 취소했습니다${what:+: $what}"
fi
if [[ -d "$git_dir/rebase-merge" || -d "$git_dir/rebase-apply" ]]; then
  git rebase --abort && echo "! 끝나지 않은 rebase를 취소했습니다"
fi
if [[ -f "$git_dir/CHERRY_PICK_HEAD" ]]; then
  git cherry-pick --abort && echo "! 끝나지 않은 cherry-pick을 취소했습니다"
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
  echo "! main이 아닌 $branch 브랜치라서 업데이트는 건너뜁니다."
  exit 0
fi
if ! git fetch -q "$remote" main; then
  echo "! ${remote}에서 받아오지 못했습니다. 지금 코드 그대로 진행합니다."
  exit 0
fi
upstream="$(git rev-parse FETCH_HEAD)"

stashed=""
if ! git diff --quiet HEAD; then
  git stash push -q -m "sync-main: 업데이트 전 로컬 변경" && stashed=1
fi

short="$(git rev-parse --short "$upstream")"
if git merge-base --is-ancestor HEAD "$upstream"; then
  if git merge -q --ff-only "$upstream"; then
    echo "✓ main 최신으로 받음 ($short)"
  else
    echo "! main을 최신으로 받지 못했습니다 (추적 안 되는 파일과 겹침?). 지금 코드 그대로 진행합니다."
  fi
elif git merge-base --is-ancestor "$upstream" HEAD; then
  echo "✓ main이 ${remote}보다 앞서 있음 (아직 올리지 않은 커밋 $(git rev-list --count "$upstream"..HEAD)개)"
else
  ahead="$(git rev-list --count "$upstream"..HEAD)"
  if git rebase -q "$upstream" >/dev/null 2>&1; then
    echo "✓ 로컬 커밋 ${ahead}개를 최신 main($short) 위로 옮김"
  else
    git rebase --abort
    backup="backup/main-$(date +%Y%m%d-%H%M%S)"
    git branch "$backup"
    git reset -q --hard "$upstream"
    echo "! 로컬 커밋 ${ahead}개가 최신 main과 충돌해서 $backup 브랜치에 보관하고, main은 $remote 것($short)으로 맞췄습니다."
    echo "  내용 보기: git log -p main..$backup    다시 적용: git cherry-pick main..$backup"
  fi
fi

if [[ -n "$stashed" ]] && ! git stash pop -q; then
  # The stash stays in the list when pop fails; clear the half-applied files.
  git reset -q --hard
  echo "! 작업 중이던 변경이 최신 코드와 충돌해서 그대로 보관해 뒀습니다: git stash list (적용: git stash pop)"
fi
exit 0
