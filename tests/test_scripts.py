"""scripts/sync-main.sh against real repositories: a bare remote and a clone of it; auto.sh,
next-task.sh, resume-target.py and next.sh."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "sync-main.sh"
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="bash script")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"{name}: {text.strip()}")


def sync(repo):
    out = subprocess.run(["bash", str(SCRIPT)], cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout + out.stderr


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    upstream = tmp_path / "upstream"  # where "GitHub" gets new commits (merged PRs)
    git(tmp_path, "clone", "-q", str(remote), str(upstream))
    git(upstream, "checkout", "-qb", "main")
    commit(upstream, "TODO.md", "- [ ] a\n")
    git(upstream, "push", "-q", "origin", "main")
    local = tmp_path / "local"  # the user's checkout
    git(tmp_path, "clone", "-q", str(remote), str(local))
    return upstream, local


def merged_pr(upstream, name="feature.py", text="x = 1\n"):
    commit(upstream, name, text)
    git(upstream, "push", "-q", "origin", "main")
    return git(upstream, "rev-parse", "HEAD")


def test_behind_fast_forwards(repos):
    upstream, local = repos
    head = merged_pr(upstream)
    assert "✓ main 최신으로 받음" in sync(local)
    assert git(local, "rev-parse", "HEAD") == head


def test_leftover_merge_is_cancelled_then_updated(repos):
    upstream, local = repos
    commit(upstream, "TODO.md", "- [x] a\n")
    git(upstream, "push", "-q", "origin", "main")
    commit(local, "TODO.md", "- [ ] a\n- [ ] b\n")
    git(local, "fetch", "-q")
    subprocess.run(["git", "merge", "origin/main"], cwd=local, capture_output=True)  # conflicts, left half-done
    assert (local / ".git" / "MERGE_HEAD").exists()

    out = sync(local)
    assert "끝나지 않은 병합을 취소했습니다" in out
    assert not (local / ".git" / "MERGE_HEAD").exists()
    # Then the same conflict again, now in the rebase: local commit kept on a backup branch.
    assert "backup/main-" in out
    assert git(local, "rev-parse", "HEAD") == git(upstream, "rev-parse", "HEAD")
    backup = git(local, "branch", "--list", "backup/*").strip()
    assert "- [ ] b" in git(local, "show", f"{backup}:TODO.md")


def test_ahead_is_kept(repos):
    _, local = repos
    commit(local, "TODO.md", "- [ ] a\n- [ ] b\n")
    head = git(local, "rev-parse", "HEAD")
    assert "아직 올리지 않은 커밋 1개" in sync(local)
    assert git(local, "rev-parse", "HEAD") == head


def test_diverged_without_conflict_is_rebased(repos):
    upstream, local = repos
    merged_pr(upstream)
    commit(local, "notes.md", "mine\n")
    assert "로컬 커밋 1개를 최신 main" in sync(local)
    assert (local / "feature.py").exists() and (local / "notes.md").exists()
    assert git(local, "rev-list", "--count", "origin/main..HEAD") == "1"


def test_uncommitted_changes_come_back(repos):
    upstream, local = repos
    merged_pr(upstream)
    (local / "TODO.md").write_text("- [ ] a\n- [ ] editing\n", encoding="utf-8")
    sync(local)
    assert (local / "feature.py").exists()
    assert "editing" in (local / "TODO.md").read_text(encoding="utf-8")


def test_uncommitted_changes_that_conflict_stay_stashed(repos):
    upstream, local = repos
    merged_pr(upstream, "TODO.md", "- [x] a\n")
    (local / "TODO.md").write_text("- [ ] a, edited\n", encoding="utf-8")
    out = sync(local)
    assert "git stash list" in out
    assert (local / "TODO.md").read_text(encoding="utf-8") == "- [x] a\n"  # no conflict markers left
    assert "a, edited" in git(local, "stash", "show", "-p")


def test_other_branch_and_no_remote_are_skipped(repos, tmp_path):
    _, local = repos
    git(local, "checkout", "-qb", "work")
    assert "main이 아닌 work 브랜치" in sync(local)
    git(local, "checkout", "-q", "main")
    git(local, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert "받아오지 못했습니다" in sync(local)


AUTO = Path(__file__).parent.parent / "scripts" / "auto.sh"

# Stands in for scripts/next.sh: writes one shift.json per call from the next line of plan.txt
# ("<cost> <merged> <ok>"), or nothing at all for "none".
FAKE_NEXT = """#!/usr/bin/env bash
n=$(( $(cat calls 2>/dev/null || echo 0) + 1 )); echo $n > calls
line=$(sed -n "${n}p" plan.txt)
[[ "$line" == none ]] && exit 1
read -r cost merged ok <<<"$line"
d=.rig/shifts/2026010$n-000000; mkdir -p $d
printf '{"ok": %s, "totals": {"cost_usd": %s}, "prs": [{"merged": %s}]}' "$ok" "$cost" "$merged" > $d/shift.json
[[ -f stop_after ]] && [[ $n -ge $(cat stop_after) ]] && touch .rig/stop
exit 0
"""


def auto(tmp_path, plan, stop_after=None, **env):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(AUTO, tmp_path / "scripts" / "auto.sh")
    fake = tmp_path / "fake_next.sh"
    fake.write_text(FAKE_NEXT, encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "plan.txt").write_text("\n".join(plan) + "\n", encoding="utf-8")
    if stop_after:
        (tmp_path / "stop_after").write_text(str(stop_after), encoding="utf-8")
    out = subprocess.run(["bash", "scripts/auto.sh"], cwd=tmp_path, capture_output=True, text=True,
                         env={**os.environ, "NEXT": str(fake), "PAUSE": "0", **env})
    calls = int((tmp_path / "calls").read_text()) if (tmp_path / "calls").exists() else 0
    return out.returncode, out.stdout + out.stderr, calls


def test_auto_runs_up_to_the_count(tmp_path):
    code, out, calls = auto(tmp_path, ["0.5 true true"] * 5, RUNS="3")
    assert code == 0 and calls == 3
    assert "끝: 3번 실행, 총 $1.5" in out


def test_auto_stops_at_the_budget(tmp_path):
    code, out, calls = auto(tmp_path, ["2 true true"] * 5, BUDGET="3")
    assert calls == 2 and "예산 $3을 다 써서 멈춥니다" in out


def test_auto_stops_after_two_runs_without_a_merge(tmp_path):
    code, out, calls = auto(tmp_path, ["0.1 false false", "0.1 true true", "0.1 false false", "0.1 false true", "0.1 true true"])
    assert calls == 4 and "두 번 연속 병합된 PR이 없어서" in out


def test_auto_stops_on_the_stop_file(tmp_path):
    (tmp_path / ".rig").mkdir()
    (tmp_path / ".rig" / "stop").touch()  # left over from before: removed at the start
    code, out, calls = auto(tmp_path, ["0.1 true true"] * 5, stop_after=2)
    assert calls == 2 and ".rig/stop 파일이 있어서 멈춥니다" in out
    assert not (tmp_path / ".rig" / "stop").exists()


def test_auto_stops_when_no_shift_was_made(tmp_path):
    code, out, calls = auto(tmp_path, ["none"])
    assert code == 1 and calls == 1 and "실행 기록이 생기지 않았습니다" in out


NEXT_TASK = Path(__file__).parent.parent / "scripts" / "next-task.sh"


def next_task(tmp_path, todo):
    (tmp_path / "TODO.md").write_text(todo, encoding="utf-8")
    out = subprocess.run(["bash", str(NEXT_TASK), str(tmp_path / "TODO.md")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_next_task_names_the_first_backlog_item(tmp_path):
    out = next_task(tmp_path, (
        "# TODO\n\n## Done\n\n- [x] **Old one**: done.\n\n## Backlog\n\n"
        "- [x] **Ticked**: skip.\n"
        "- [ ] **`rig logs` shows *status* correctly**: the list prints...\n"
        "- [ ] **Second**: later.\n\n## Needs a design first\n\n- [ ] **Not this**: x\n"))
    first = out.splitlines()[0]
    assert first == "TODO 항목 처리: `rig logs` shows *status* correctly"
    assert '"`rig logs` shows *status* correctly" 항목 하나만' in out


def test_next_task_with_an_empty_backlog_asks_the_planner(tmp_path):
    out = next_task(tmp_path, "## Backlog\n\n- [x] **Done**: x\n\n## Needs a design first\n\n- [ ] **Not this**: x\n")
    assert out.splitlines()[0] == "TODO 새 항목 기획 후 첫 항목 처리" and "planner" in out


def test_next_task_without_a_todo_file(tmp_path):
    out = subprocess.run(["bash", str(NEXT_TASK), str(tmp_path / "missing.md")], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("TODO 새 항목 기획")


SCRIPTS = Path(__file__).parent.parent / "scripts"
RESUME_TARGET = SCRIPTS / "resume-target.py"
TASK = "TODO 항목 처리: x"


@pytest.fixture
def rig_repo(repo):
    """The `repo` fixture with a rig.yaml whose workspace is the repo itself."""
    (repo / "rig.yaml").write_text(
        "name: t\nworkspace: .\nhands:\n  coder:\n    role: Code.\n    tools: [write_file]\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".rig/\n", encoding="utf-8")
    return repo


def stopped_shift(repo, shift_id, task=TASK, resumed_from=None, branch=True, **summary):
    """A shift folder like one `rig run --worktree` leaves when interrupted, with commit on rig/<id>."""
    base = git(repo, "rev-parse", "main")
    sha = base
    if branch:
        tree = git(repo, "rev-parse", "main^{tree}")
        sha = git(repo, "commit-tree", tree, "-p", base, "-m", f"rig: {shift_id}")
        git(repo, "update-ref", f"refs/heads/rig/{shift_id}", sha)
    data = {"rig": "t", "task": task, "ok": False, "error": "interrupted", "resumed_from": resumed_from,
            "worktrees": [{"repo": str(repo), "branch": f"rig/{shift_id}", "base": base, "commit": sha,
                           "changed": True, "kept_at": None}],
            "hands": {}, **summary}
    folder = repo / ".rig" / "shifts" / shift_id
    folder.mkdir(parents=True)
    (folder / "shift.json").write_text(json.dumps(data), encoding="utf-8")


def resume_target(repo, task=TASK):
    out = subprocess.run([sys.executable, str(RESUME_TARGET), str(repo / "rig.yaml"), task],
                         capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PYTHONPATH": str(SCRIPTS.parent / "src")})
    assert out.returncode == 0, out.stderr
    return out.stdout, out.stderr


def test_resume_target_picks_up_a_stopped_shift(rig_repo):
    stopped_shift(rig_repo, "20260101-000000")
    assert resume_target(rig_repo, f"  {TASK}\n") == ("20260101-000000\n", "↻ 중단된 작업 이어 하기: 20260101-000000\n")


def test_resume_target_picks_up_a_shift_resumed_once(rig_repo):
    stopped_shift(rig_repo, "20260101-000000")
    stopped_shift(rig_repo, "20260101-000001", resumed_from="20260101-000000")
    assert resume_target(rig_repo) == ("20260101-000001\n", "↻ 중단된 작업 이어 하기: 20260101-000001\n")


def test_resume_target_starts_afresh_for_another_task(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", task="something else")
    assert resume_target(rig_repo) == ("", "")


def test_resume_target_starts_afresh_after_an_ok_shift(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", ok=True, error=None)
    assert resume_target(rig_repo) == ("", "")


def test_resume_target_picks_up_an_ok_shift_that_hit_its_budget(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", ok=True, error=None, stopped="budget")
    assert resume_target(rig_repo) == ("20260101-000000\n", "↻ 중단된 작업 이어 하기: 20260101-000000\n")


def open_pr(repo, shift_id, note):
    return {"repo": str(repo), "branch": f"rig/{shift_id}", "url": "https://github.com/o/r/pull/7", "number": 7,
            "merged": False, "closed": False, "note": note}


def test_resume_target_picks_up_an_ok_shift_whose_pr_failed_ci(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", ok=True, error=None,
                  prs=[open_pr(rig_repo, "20260101-000000", "not merged: CI failed: tests")])
    assert resume_target(rig_repo) == ("20260101-000000\n", "↻ CI 실패한 PR #7 고치기 (실패: tests)\n")


@pytest.mark.parametrize("note", ["not merged: reviewer didn't approve (no LGTM)",
                                  "not merged after updating the branch: CI failed: tests"])
def test_resume_target_starts_afresh_after_an_ok_shift_whose_pr_is_open_for_another_reason(rig_repo, note):
    stopped_shift(rig_repo, "20260101-000000", ok=True, error=None, prs=[open_pr(rig_repo, "20260101-000000", note)])
    assert resume_target(rig_repo) == ("", "")


def test_resume_target_ci_fix_counts_towards_the_chain_limit(rig_repo):
    stopped_shift(rig_repo, "20260101-000000")
    stopped_shift(rig_repo, "20260101-000001", resumed_from="20260101-000000")
    stopped_shift(rig_repo, "20260101-000002", resumed_from="20260101-000001", ok=True, error=None,
                  prs=[open_pr(rig_repo, "20260101-000002", "not merged: CI failed: tests")])
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000002 (이미 두 번 이어 했음)" in err


def test_resume_target_starts_afresh_after_a_merged_pr(rig_repo):
    stopped_shift(rig_repo, "20260101-000000",
                  prs=["junk", {"url": "https://example.com/pr/1", "merged": True, "note": None}])
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000000 (이미 병합된 PR 있음)" in err


def test_resume_target_starts_afresh_for_another_rig(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", rig="other")
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000000 (이어 할 수 없음)" in err


def test_resume_target_starts_afresh_after_two_resumes(rig_repo):
    stopped_shift(rig_repo, "20260101-000000")
    stopped_shift(rig_repo, "20260101-000001", resumed_from="20260101-000000")
    stopped_shift(rig_repo, "20260101-000002", resumed_from="20260101-000001")
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000002 (이미 두 번 이어 했음)" in err


def test_resume_target_starts_afresh_without_a_branch(rig_repo):
    stopped_shift(rig_repo, "20260101-000000", branch=False)  # branch deleted
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000000 (브랜치에 이어 할 커밋 없음)" in err
    stopped_shift(rig_repo, "20260101-000001", worktrees=[])  # nothing committed
    out, err = resume_target(rig_repo)
    assert out == "" and "! 이어 하지 않고 새로 시작: 20260101-000001 (브랜치에 이어 할 커밋 없음)" in err
    assert "Error" not in err and "branch" not in err


def test_resume_target_without_shifts(rig_repo):
    assert resume_target(rig_repo) == ("", "")


FAKE_UV = """#!/usr/bin/env bash
printf '%s\\n' "$@" -- >> "$UV_LOG"
exit 0
"""


def next_sh(tmp_path, resume_id):
    """Runs scripts/next.sh with fake sync-main/next-task/resume-target, uv and curl; returns
    (stdout, each uv call's argument list). The fake uv logs one argument per line and `--`
    after each call (TASK has no newline)."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "next.sh", tmp_path / "scripts" / "next.sh")
    fakes = {"scripts/sync-main.sh": "#!/usr/bin/env bash\nexit 0\n",
             "scripts/next-task.sh": f"#!/usr/bin/env bash\necho '{TASK}'\n",
             "bin/uv": FAKE_UV, "bin/curl": "#!/usr/bin/env bash\nexit 0\n"}
    for name, text in fakes.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    # next.sh runs resume-target.py through uv, so the fake uv stands in for it and prints the id.
    if resume_id:
        (tmp_path / "bin" / "uv").write_text(FAKE_UV.replace(
            "exit 0", f'for arg in "$@"; do [[ "$arg" == scripts/resume-target.py ]] && echo {resume_id}; done\n'
                      'exit 0'), encoding="utf-8")
    log = tmp_path / "uv.log"
    out = subprocess.run(["bash", "scripts/next.sh"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
                              "ANTHROPIC_API_KEY": "x", "UV_LOG": str(log), "MAX_COST": "3"})
    assert out.returncode == 0, out.stderr
    calls, call = [], []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "--":
            calls.append(call)
            call = []
        else:
            call.append(line)
    return out.stdout, calls


def resume_target_call(calls):
    return next(call for call in calls if "scripts/resume-target.py" in call)


def test_next_sh_resumes_the_stopped_shift(tmp_path):
    out, calls = next_sh(tmp_path, "20260101-000000")
    assert f"▶ {TASK}" in out and "↻" not in out  # resume-target.py itself says "↻ …"
    assert resume_target_call(calls)[-3:] == ["scripts/resume-target.py", "self.rig.yaml", TASK]
    assert calls[-1][-4:] == ["--max-cost", "3", "--resume", "20260101-000000"]
    assert not any(TASK in arg for arg in calls[-1])


def test_next_sh_starts_afresh_without_a_shift_to_resume(tmp_path):
    out, calls = next_sh(tmp_path, None)
    assert "↻" not in out
    assert resume_target_call(calls)[-3:] == ["scripts/resume-target.py", "self.rig.yaml", TASK]
    assert calls[-1][-4:] == ["run", "--max-cost", "3", TASK] and "--resume" not in calls[-1]
