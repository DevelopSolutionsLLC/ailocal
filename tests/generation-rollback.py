#!/usr/bin/env python3
"""Generation is per-file atomic WITH ROLLBACK — never mixed on disk.

DESTRUCTIVE, and separate for that reason: it perturbs a tracked profile and
monkeypatches os.replace inside a loaded generation module. Every source it
touches is restored and the tree is regenerated before it exits.

WHAT IT PROVES, AND WHAT IT DOES NOT. It injects an I/O error, which the
recovery path can respond to. It does NOT simulate SIGKILL: nothing in-process
can roll back after the process is gone. "Per-file atomic with rollback on
partial failure" is the accurate claim, and it is what the code says.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite, load_module

ROOT = REPO
FAIL_AFTER = 3

_suite = Suite()
check = _suite.check


def load_sync():
    return load_module("generation", ROOT / "src" / "ailocal" / "generation.py")


def digest(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "ABSENT"


def destinations(sm) -> list[pathlib.Path]:
    return [sm.LITELLM_CONFIG, sm.CAPS_JSON, sm.CODEX_CATALOG, sm.CLAUDE_SETTINGS,
            sm.CODEX_CONFIG, sm.CODEX_PLAN, sm.CODEX_REVIEW,
            sm.CONFIGURE_ZSH, sm.CONTRACT_JSON]


def run(fail_after: int | None) -> tuple[dict, dict, int]:
    """Generate once. `fail_after` injects an I/O error on that forward replace."""
    sm = load_sync()
    dests = destinations(sm)
    before = {p: digest(p) for p in dests}
    calls = {"n": 0}
    real = os.replace

    def flaky(src, dst):
        # Restores (backup -> destination) must be allowed: this models an I/O
        # error, not a SIGKILL, so the recovery path has to be able to run.
        if str(src).endswith(".bak-sync"):
            return real(src, dst)
        calls["n"] += 1
        if fail_after is not None and calls["n"] == fail_after + 1:
            raise OSError("INJECTED FAULT: simulated I/O error mid-replace")
        return real(src, dst)

    sm.os.replace = flaky
    try:
        sm.main([])
    except (SystemExit, OSError):
        pass
    finally:
        sm.os.replace = real
    after = {p: digest(p) for p in dests}
    return before, after, calls["n"]


def main() -> int:
    profile = RESOURCES / "profiles" / "64gb.toml"
    original = profile.read_text()

    print("A DRIFTED TEMPLATE STOPS GENERATION")
    # A missing marker must abort: skipping one emits a config.yaml with no
    # model_list, which every consumer reads as a valid empty stack.
    sm = load_sync()
    try:
        sm.splice("no markers here\n", "<<BEGIN>>", "<<END>>", "x", "model_list")
        got = "RETURNED"
    except SystemExit as exc:
        got = str(exc)
    check("markers not found" in got, "a template with no markers exits non-zero",
          f"got {got!r}")

    print("GENERATION ROLLBACK")
    try:
        # A source change, so a successful generation produces DIFFERENT bytes.
        # Without it a "no files changed" result would be indistinguishable from
        # a rollback, and the test would pass while proving nothing.
        perturbed = original.replace("temperature = 0.1\n", "temperature = 0.15\n", 1)
        check(perturbed != original, "the fixture perturbs a real profile value")
        profile.write_text(perturbed)

        before, after, n = run(fail_after=FAIL_AFTER)
        changed = [p for p in before if before[p] != after[p]]
        check(n > FAIL_AFTER,
              f"the fault fired after {FAIL_AFTER} replaces (saw {n} calls)")
        check(not changed,
              f"every destination rolled back to its old hash "
              f"({len(changed)} changed)")
        for p in changed:
            print(f"        MIXED: {p.relative_to(ROOT)} {before[p]} -> {after[p]}")

        leftover = sorted(str(p.relative_to(ROOT))
                          for p in ROOT.rglob("*.tmp-sync"))
        check(not leftover, f"no .tmp-sync files survive a failure ({leftover})")
        leftover = sorted(str(p.relative_to(ROOT))
                          for p in ROOT.rglob("*.bak-sync"))
        check(not leftover, f"no .bak-sync files survive a failure ({leftover})")

        # The other half of the guarantee: with no fault, ALL destinations move.
        before2, after2, _ = run(fail_after=None)
        moved = [p for p in before2 if before2[p] != after2[p]]
        check(bool(moved),
              f"a clean run applies the change ({len(moved)} destination(s) updated)")
    finally:
        profile.write_text(original)
        # Leave the tree on the real configuration, not the fixture's.
        try:
            load_sync().main([])
        except SystemExit:
            pass

    print()
    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
