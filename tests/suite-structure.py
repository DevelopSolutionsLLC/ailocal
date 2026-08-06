#!/usr/bin/env python3
"""Structural guarantees for the consolidated suites.

A suite that merges several behaviours behind section dispatch can pass its own
assertions while being quietly broken: gateway.py once ran persona's ten checks
at import, so `gateway.py repair` alone reported 27 checks instead of 17 and the
combined total was right by coincidence. Nothing in the gate saw it.

These checks pin the properties that make section dispatch trustworthy.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, run  # noqa: E402

_suite = Suite("SUITE STRUCTURE")
check = _suite.check

# Consolidated suites and the sections each must expose.
DISPATCHED = {
    "profiles.py": ("resolver", "hardware", "policy"),
    "gateway.py": ("persona", "repair", "trace"),
    "benchmark.py": ("library", "planner", "command", "runtime"),
    "clients.sh": ("roles", "codex"),
}


def _argv(path: Path, args: tuple[str, ...]) -> list[str]:
    """Shell suites are run by bash, Python suites by this interpreter."""
    launcher = ["bash"] if path.suffix == ".sh" else [sys.executable]
    return launcher + [str(path), *args]


def counts(path: Path, *args: str) -> tuple[int, int]:
    """Run a suite and return (exit status, number of reported checks)."""
    r = run(_argv(path, args), timeout=300)
    n = sum(1 for line in r.stdout.splitlines()
            if line.startswith("  ") and ("PASS" in line or "FAIL" in line))
    return r.returncode, n


for name, sections in DISPATCHED.items():
    path = REPO / "tests" / name
    _suite.section(name)

    # Importing must not assert anything: that is what hid the gateway defect.
    # Sourcing is the shell equivalent, so the property is the same either way.
    if path.suffix == ".sh":
        r = run(["bash", "-c", f'. "{path}"'], timeout=300)
        emitted = sum(1 for l in r.stdout.splitlines()
                      if l.startswith("  ") and ("PASS" in l or "FAIL" in l))
    else:
        probe = (f"import runpy, sys; sys.argv=['x']; "
                 f"m=runpy.run_path({str(path)!r}, run_name='not_main'); "
                 f"print('CHECKS', m['_suite'].passed + len(m['_suite'].failures))")
        r = run([sys.executable, "-c", probe], timeout=300)
        emitted = next((int(l.split()[1]) for l in r.stdout.splitlines()
                        if l.startswith("CHECKS")), -1)
    check(emitted == 0, f"{name} emits no checks at import (got {emitted})")

    rc_all, n_all = counts(path)
    check(rc_all == 0, f"{name} passes with every section")

    total = 0
    for sect in sections:
        rc, n = counts(path, sect)
        check(rc == 0 and n > 0, f"{name} {sect} runs alone ({n} checks)")
        total += n

    # Sections must not leak into one another: solo totals have to add up.
    check(total == n_all,
          f"{name} solo totals sum to the combined run ({total} == {n_all})")

    rc, _ = counts(path, "no-such-section")
    check(rc != 0, f"{name} rejects an unknown section")

_suite.section("no module-level mutable state")
for name in DISPATCHED:
    src = (REPO / "tests" / name).read_text()
    kw = "declare -g " if name.endswith(".sh") else "global "
    n = sum(1 for line in src.splitlines() if line.strip().startswith(kw))
    check(n == 0, f"{name} declares no globals (found {n})")

_suite.section("shell suites report failure")
# A shell suite that keeps a private counter after adopting the harness will
# print FAIL and still exit 0. Assert the wiring, not the assertions.
for name in sorted(p.name for p in (REPO / "tests").glob("*.sh")
                   if p.name != "harness.sh"):
    src = (REPO / "tests" / name).read_text()
    if "harness.sh" not in src:
        _suite.skip(f"{name} does not use the shared harness")
        continue
    stale = [l for l in src.splitlines()
             if "$fails" in l and "report" not in l and not l.strip().startswith("#")
             and "fails before" not in l]
    check(not stale, f"{name} has no private failure counter", "; ".join(stale[:2]))

sys.exit(_suite.report())
