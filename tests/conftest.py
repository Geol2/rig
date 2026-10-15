"""On GitHub Actions, report each failing test as an annotation on the PR."""

import os
import sys

import pytest

from rig.worktree import git


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
    text = f"total {total:.0f}s over {len(_durations)} tests (summed across workers)%0A" + "%0A".join(
        f"{secs:.2f}s {nodeid}".replace("%", "%25") for nodeid, secs in top)
    title = f"slowest tests ({sys.platform})"
    out = f"\n::notice title={title}::{text}\n"
    stream = getattr(sys.__stdout__, "buffer", None)
    if stream:
        stream.write(out.encode("utf-8", errors="replace"))
        stream.flush()
    else:
        sys.__stdout__.write(out)


@pytest.fixture
def repo(tmp_path):
    """A git repo on `main` with one commit (app/main.py), for worktree and resume tests."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("print('v1')\n", encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


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
