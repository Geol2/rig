"""On GitHub Actions, report each failing test as an annotation on the PR."""

import os
import shutil
import sys
from pathlib import Path
from typing import Callable

import pytest

from rig.worktree import git


# Whole shifts on real git repos (worktrees, PRs against a local remote, resuming). The same on
# every OS and slow on Windows, where each of their hundreds of git commands costs a new
# process; Windows PR checks skip them (-m "not git_e2e"), pushes to main run everything.
GIT_E2E = {"tests/test_resume.py", "tests/test_publish.py", "tests/test_multi_worktree.py", "tests/test_worktree.py"}


# Fixtures that build git repositories: tests using them count as git_e2e wherever they are.
GIT_FIXTURES = {"repo", "rig_repo", "anonymous_repo"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.nodeid.split("::")[0] in GIT_E2E or GIT_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker("git_e2e")


def _annotate(path: str, line: int, title: str, text: str) -> None:
    # The end of the failure (assertion and its values) is the useful part; workflow commands
    # need newlines and '%' escaped.
    text = "\n".join(text.splitlines()[-25:])
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = title.replace("%", "%25").replace(",", "%2C").replace("::", " ")
    # Leading newline: pytest -q's progress dots leave the cursor mid-line, and GitHub only
    # reads a workflow command at the start of a line.
    out = f"\n::error file={path},line={line},title={title}::{text}\n"
    # Bytes, as UTF-8: Windows runners' console encoding can't take Korean or "✗".
    stream = getattr(sys.__stdout__, "buffer", None)
    if stream:
        stream.write(out.encode("utf-8", errors="replace"))
        stream.flush()
    else:
        sys.__stdout__.write(out)


# Test id -> seconds (setup + call + teardown), for the slowest-tests notice on GitHub Actions.
_durations: dict[str, float] = {}
SLOWEST = 15


def pytest_runtest_logreport(report):
    _durations[report.nodeid] = _durations.get(report.nodeid, 0.0) + report.duration
    if report.failed and os.environ.get("GITHUB_ACTIONS"):
        path, line, _ = report.location
        _annotate(path, (line or 0) + 1, f"{report.nodeid} ({report.when})", str(report.longrepr))


def pytest_collectreport(report):
    if report.failed and os.environ.get("GITHUB_ACTIONS"):
        _annotate(report.nodeid.split("::")[0] or "tests", 1, f"collecting {report.nodeid}", str(report.longrepr))


def pytest_sessionfinish(session):
    """On GitHub Actions, list the slowest tests as a notice on the PR (Windows runners are
    several times slower than Linux, and their logs aren't always at hand). With pytest-xdist
    the workers' reports reach the controller, which is the one without `workerinput`."""
    if not os.environ.get("GITHUB_ACTIONS") or hasattr(session.config, "workerinput") or not _durations:
        return
    top = sorted(_durations.items(), key=lambda kv: -kv[1])[:SLOWEST]
    total = sum(_durations.values())
    files: dict[str, float] = {}
    for nodeid, secs in _durations.items():
        files[nodeid.split("::")[0]] = files.get(nodeid.split("::")[0], 0.0) + secs
    lines = [f"total {total:.0f}s over {len(_durations)} tests (summed across workers)", "by file:"]
    lines += [f"{secs:.1f}s {f}" for f, secs in sorted(files.items(), key=lambda kv: -kv[1])]
    lines += ["slowest:"] + [f"{secs:.2f}s {nodeid}" for nodeid, secs in top]
    text = "%0A".join(line.replace("%", "%25") for line in lines)
    title = f"slowest tests ({sys.platform})"
    out = f"\n::notice title={title}::{text}\n"
    stream = getattr(sys.__stdout__, "buffer", None)
    if stream:
        stream.write(out.encode("utf-8", errors="replace"))
        stream.flush()
    else:
        sys.__stdout__.write(out)


def copy_of(factory, name: str, make) -> Callable[[Path], Path]:
    """A fixture body that copies a folder `make(folder)` built once per test process: each git
    command costs a new process, which is slow on Windows, and many tests start from the same repo."""
    template: list[Path] = []

    def copy(dest: Path) -> Path:
        if not template:
            folder = factory.mktemp(f"template-{name}") / name
            folder.mkdir()
            make(folder)
            template.append(folder)
        shutil.copytree(template[0].parent, dest, dirs_exist_ok=True)
        return dest / name

    return copy


def no_background_git(repo: Path) -> None:
    """Turn off git's automatic maintenance, which can still be writing in .git (a lock file
    that comes and goes) while copy_of copies the template."""
    git("config", "maintenance.auto", "false", cwd=repo)
    git("config", "gc.auto", "0", cwd=repo)


def _make_repo(repo: Path) -> None:
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("print('v1')\n", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    no_background_git(repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)


@pytest.fixture(scope="session")
def _repo_copy(tmp_path_factory):
    return copy_of(tmp_path_factory, "repo", _make_repo)


@pytest.fixture
def repo(tmp_path, _repo_copy):
    """A git repo on `main` with one commit (app/main.py), for worktree and resume tests."""
    return _repo_copy(tmp_path)


@pytest.fixture
def anonymous_repo(tmp_path, monkeypatch):
    """A git repo with one commit where git has no user.name/user.email, whatever the host's config."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "anon"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.useConfigOnly", "true", cwd=repo)  # don't guess a name from the hostname
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=repo)
    return repo
