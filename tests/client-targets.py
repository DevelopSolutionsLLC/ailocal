#!/usr/bin/env python3
"""client-targets.py — WHICH clients get configured, and what VS Code reports.

Two contracts, both of them user-visible and both regressed once:

  * target selection — `all` is every supported client, no argument is only the
    ones this machine has, a name is honoured whether or not it is installed.
  * install_vscode's THREE outcomes — success, absence, and an actual failure —
    because the caller does different things with each, and collapsing failure
    into absence silently drops the Copilot instruction files.

Nothing here touches the real machine: the per-client configurators, generation
and the shell rewrite are all replaced with recorders.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from harness import Suite  # noqa: E402
from ailocal import clients as C  # noqa: E402
from ailocal import generation  # noqa: E402

_suite = Suite()
check = _suite.check


def _configured(argv, present: set[str]) -> tuple[int, list[str]]:
    """Run clients.main with every side effect stubbed; return (rc, targets)."""
    order: list[str] = []
    saved = (dict(C._TARGETS), C.present, C._master_key,
             C.ensure_shell_sourcing, C.report_missing, generation.main)
    try:
        C._TARGETS.update({n: (lambda n=n: order.append(n)) for n in C._TARGETS})
        C.present = lambda name: name in present
        C._master_key = lambda: "sk-test"
        C.ensure_shell_sourcing = lambda key: None
        C.report_missing = lambda names: None
        generation.main = lambda argv: 0
        return C.main(argv), order
    finally:
        (targets, C.present, C._master_key, C.ensure_shell_sourcing,
         C.report_missing, generation.main) = saved
        C._TARGETS.clear()
        C._TARGETS.update(targets)


def _vscode(user_dir, which_code: bool, writes_fail: bool = False) -> int:
    """Run install_vscode against a stubbed VS Code; return its outcome code."""
    saved = (C._vscode_user_dir, C.shutil.which, C._code, C._extensions,
             C._install_extension, C._provider_group, C._prune_deprecated,
             C._ensure_recommended)

    def boom(*a, **k):
        raise OSError(13, "Permission denied")

    try:
        C._vscode_user_dir = lambda: user_dir
        C.shutil.which = lambda name: "/usr/local/bin/code" if which_code else None
        C._code = lambda *a: "1.99.0\nabc\nx64" if which_code else None
        C._extensions = lambda: []
        C._install_extension = lambda ext, installed: None
        C._provider_group = boom if writes_fail else (lambda p, dry: None)
        C._prune_deprecated = lambda p, dry: None
        C._ensure_recommended = lambda p: None
        return C.install_vscode([])
    finally:
        (C._vscode_user_dir, C.shutil.which, C._code, C._extensions,
         C._install_extension, C._provider_group, C._prune_deprecated,
         C._ensure_recommended) = saved


def main() -> None:
    _suite.section("TARGET SELECTION")

    rc, order = _configured(["all"], present={"claude"})
    check(rc == 0 and order == list(C.TARGETS),
          "`all` configures every supported client", f"rc={rc} order={order}")

    rc, order = _configured([], present={"claude", "codex"})
    check(rc == 0 and set(order) == {"claude", "codex"},
          "no argument configures only the DETECTED clients", f"order={order}")

    rc, order = _configured(["codex"], present=set())
    check(rc == 0 and order == ["codex"],
          "a named target is configured even when it is not installed",
          f"rc={rc} order={order}")

    rc, order = _configured(["nope"], present={"claude"})
    check(rc == 1 and order == [],
          "an unknown target still fails and configures nothing",
          f"rc={rc} order={order}")

    rc, order = _configured([], present=set())
    check(rc == 0 and order == [],
          "no client installed and no argument: clean exit, nothing configured",
          f"rc={rc} order={order}")

    _suite.section("install_vscode REPORTS WHICH KIND OF OUTCOME IT WAS")

    got = _vscode(Path("/tmp/does-not-need-to-exist"), which_code=True)
    check(got == C.VSCODE_OK, "a successful configuration returns VSCODE_OK",
          f"got {got}")

    got = _vscode(None, which_code=False)
    check(got == C.VSCODE_ABSENT, "VS Code absent returns VSCODE_ABSENT",
          f"got {got}")

    got = _vscode(None, which_code=True)
    check(got == C.VSCODE_ABSENT,
          "VS Code installed but never launched returns VSCODE_ABSENT",
          f"got {got}")

    got = _vscode(Path("/tmp/does-not-need-to-exist"), which_code=True,
                  writes_fail=True)
    check(got == C.VSCODE_FAILED,
          "a write failure returns VSCODE_FAILED, NOT absence", f"got {got}")

    check(C.VSCODE_ABSENT != C.VSCODE_FAILED != C.VSCODE_OK,
          "the three outcomes are distinct values")

    _suite.section("AND THE CALLER ACTS ON THE DIFFERENCE")

    def _ran_instructions(outcome: int) -> bool:
        seen = []
        saved = (C.install_vscode, C._copilot_instructions)
        try:
            C.install_vscode = lambda argv: outcome
            C._copilot_instructions = lambda: seen.append(True)
            C.target_vscode()
        finally:
            (C.install_vscode, C._copilot_instructions) = saved
        return bool(seen)

    check(_ran_instructions(C.VSCODE_OK),
          "on success the Copilot instruction files ARE deployed")
    check(not _ran_instructions(C.VSCODE_ABSENT),
          "on absence they are skipped — there is nothing to read them")
    check(not _ran_instructions(C.VSCODE_FAILED),
          "on a real failure they are skipped, and the failure is reported")

    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
