"""scripts/sync-main.sh against real repositories: a bare remote and a clone of it."""

import os
import shutil
import subprocess
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
