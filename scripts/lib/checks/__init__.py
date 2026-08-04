"""Structured results shared by validate, smoke and doctor.

Seven validators each carried their own output style and exit convention. A
check is written once, returns a CheckResult, and the public commands decide how
to render it and what to do with the outcome.

Deliberately small: no registration, no discovery, no plugin machinery. A check
is a function that returns CheckResult.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

__all__ = ["CheckStatus", "CheckResult", "render", "exit_code",
           "PASS", "FAIL", "WARN", "BLOCKED", "SKIP"]


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"        # a real defect; fails the command
    WARN = "warn"        # degraded but usable; never fails validate/smoke
    BLOCKED = "blocked"  # could not be determined (a dependency is absent)
    SKIP = "skip"        # deliberately not applicable here


PASS, FAIL = CheckStatus.PASS, CheckStatus.FAIL
WARN, BLOCKED, SKIP = CheckStatus.WARN, CheckStatus.BLOCKED, CheckStatus.SKIP


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    summary: str
    detail: str | None = None
    remediation: str | None = None


_MARK = {PASS: ("\033[32m✓\033[0m", 1), FAIL: ("\033[31m✗\033[0m", 2),
         WARN: ("\033[33m⚠\033[0m", 2), BLOCKED: ("\033[33m?\033[0m", 2),
         SKIP: ("\033[2m—\033[0m", 1)}


def render(results, *, verbose: bool = False, remediation: bool = False) -> None:
    """Print results. `remediation` is doctor's mode; validate and smoke omit it."""
    for r in results:
        mark, stream = _MARK[r.status]
        out = sys.stdout if stream == 1 or not remediation else sys.stderr
        print(f"  {mark} {r.summary}", file=out)
        if r.detail and (verbose or r.status in (FAIL, WARN, BLOCKED)):
            for line in r.detail.splitlines():
                print(f"      {line}", file=out)
        if remediation and r.remediation and r.status is not PASS:
            for line in r.remediation.splitlines():
                print(f"      → {line}", file=out)


def exit_code(results) -> int:
    """0 clean, 1 if anything failed. WARN and BLOCKED do not fail a command."""
    return 1 if any(r.status is FAIL for r in results) else 0
