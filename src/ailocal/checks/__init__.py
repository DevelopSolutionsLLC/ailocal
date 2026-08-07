"""The one result type, the one renderer and the one outcome rule.

A check returns a CheckResult; `ailocal check` collects them, renders them and
exits on them. No registration or discovery: a check is a function.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

__all__ = ["CheckStatus", "CheckResult", "render", "exit_code", "verdict",
           "passed", "failed", "warning", "blocked",
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


def passed(name: str, summary: str) -> CheckResult:
    return CheckResult(name, PASS, summary)


def failed(name: str, summary: str, remediation: str | None = None,
           detail: str | None = None) -> CheckResult:
    return CheckResult(name, FAIL, summary, detail, remediation)


def warning(name: str, summary: str, remediation: str | None = None,
            detail: str | None = None) -> CheckResult:
    return CheckResult(name, WARN, summary, detail, remediation)


def blocked(name: str, summary: str, detail: str | None = None) -> CheckResult:
    return CheckResult(name, BLOCKED, summary, detail)


def verdict(name: str, ok: bool, good: str, bad: str,
            remediation: str | None = None, detail: str | None = None) -> CheckResult:
    """A check whose two outcomes differ only in wording."""
    return (CheckResult(name, PASS, good) if ok
            else CheckResult(name, FAIL, bad, detail, remediation))


_MARK = {PASS: ("\033[32m✓\033[0m", 1), FAIL: ("\033[31m✗\033[0m", 2),
         WARN: ("\033[33m⚠\033[0m", 2), BLOCKED: ("\033[33m?\033[0m", 2),
         SKIP: ("\033[2m—\033[0m", 1)}


def render(results) -> None:
    """Print results. Anything that is not a pass carries its fix and goes to
    stderr, so a caller redirecting stdout still sees what is wrong."""
    for r in results:
        mark, stream = _MARK[r.status]
        out = sys.stdout if stream == 1 else sys.stderr
        print(f"  {mark} {r.summary}", file=out)
        if r.detail and r.status in (FAIL, WARN, BLOCKED):
            for line in r.detail.splitlines():
                print(f"      {line}", file=out)
        if r.remediation and r.status is not PASS:
            for line in r.remediation.splitlines():
                print(f"      → {line}", file=out)


def exit_code(results) -> int:
    """0 clean, 1 if anything failed. WARN and BLOCKED do not fail a command."""
    return 1 if any(r.status is FAIL for r in results) else 0
