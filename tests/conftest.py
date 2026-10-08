"""On GitHub Actions, report each failing test as an annotation on the PR."""

import os
import sys


def pytest_runtest_logreport(report):
    if not (report.failed and os.environ.get("GITHUB_ACTIONS")):
        return
    path, line, _ = report.location
    # The end of the failure (assertion and its values) is the useful part; workflow commands
    # need newlines and '%' escaped.
    text = "\n".join(str(report.longrepr).splitlines()[-25:])
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = report.nodeid.replace("%", "%25").replace(",", "%2C").replace("::", " ")
    sys.__stdout__.write(f"::error file={path},line={(line or 0) + 1},title={title}::{text}\n")
    sys.__stdout__.flush()
