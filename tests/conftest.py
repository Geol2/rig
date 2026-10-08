"""On GitHub Actions, report each failing test as an annotation on the PR."""

import os
import sys


def _annotate(path: str, line: int, title: str, text: str) -> None:
    # The end of the failure (assertion and its values) is the useful part; workflow commands
    # need newlines and '%' escaped.
    text = "\n".join(text.splitlines()[-25:])
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = title.replace("%", "%25").replace(",", "%2C").replace("::", " ")
    out = f"::error file={path},line={line},title={title}::{text}\n"
    # Bytes, as UTF-8: Windows runners' console encoding can't take Korean or "✗".
    stream = getattr(sys.__stdout__, "buffer", None)
    if stream:
        stream.write(out.encode("utf-8", errors="replace"))
        stream.flush()
    else:
        sys.__stdout__.write(out)


def pytest_runtest_logreport(report):
    if report.failed and os.environ.get("GITHUB_ACTIONS"):
        path, line, _ = report.location
        _annotate(path, (line or 0) + 1, f"{report.nodeid} ({report.when})", str(report.longrepr))


def pytest_collectreport(report):
    if report.failed and os.environ.get("GITHUB_ACTIONS"):
        _annotate(report.nodeid.split("::")[0] or "tests", 1, f"collecting {report.nodeid}", str(report.longrepr))
