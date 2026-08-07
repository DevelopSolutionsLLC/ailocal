#!/usr/bin/env python3
"""Generation is per-file atomic WITH ROLLBACK — never mixed on disk.

THE DEFECT THIS REPRODUCES. flush_stage() wrote every temp file first and
replaced effective-profile.json last, and the docstring called that
transactionally atomic. It was not: os.replace() is atomic PER FILE, and the
set is replaced one file at a time, so a failure part-way through left some
destinations new and some old.

Ordering alone does not save the deployed system either. Writing the marker
last makes marker-aware readers fail closed, but LiteLLM reads
litellm/config.yaml directly and each client reads its own generated
file directly -- none of them consult effective-profile.json. Before the fix,
failing after three replaces left capabilities.generated.json new
while the marker and client configs were still old. Mixed state, on disk,
servable.

So flush_stage now backs up each destination before replacing it and restores
every already-replaced destination if any later replacement fails.

WHAT THIS PROVES, AND WHAT IT DOES NOT. It injects an I/O error, which the
recovery path can respond to. It does NOT simulate SIGKILL: nothing in-process
can roll back after the process is gone. The accurate wording is "per-file
atomic with rollback on partial failure", and that is what the code says.

Read-only with respect to real configuration: every source it perturbs is
restored, and the tree is regenerated before the test exits.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, load_module

ROOT = REPO
FAIL_AFTER = 3

_suite = Suite()
check = _suite.check


def load_sync():
    return load_module("sync_models", ROOT / "lib" / "sync-models.py")


def digest(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "ABSENT"


def destinations(sm) -> list[pathlib.Path]:
    return [sm.LITELLM_CONFIG, sm.CAPS_JSON, sm.CODEX_CATALOG, sm.CLAUDE_SETTINGS,
            sm.CODEX_CONFIG, sm.CODEX_PLAN, sm.CODEX_REVIEW, sm.CONTINUE_CONFIG,
            sm.CONFIGURE_ZSH, sm.COPILOT_REPO_MD, sm.CONTRACT_JSON,
            ROOT / "config" / "effective-profile.json"]


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
    argv = sys.argv[:]
    sys.argv = ["sync-models.py"]
    try:
        sm.main()
    except (SystemExit, OSError):
        pass
    finally:
        sys.argv = argv
        sm.os.replace = real
    after = {p: digest(p) for p in dests}
    return before, after, calls["n"]


def main() -> int:
    profile = ROOT / "profiles" / "64gb.toml"
    original = profile.read_text()

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
        sm = load_sync()
        sys.argv = ["sync-models.py"]
        try:
            sm.main()
        except SystemExit:
            pass

    print()
    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
