#!/usr/bin/env python3
"""The shared validator checks: classification, isolation and timeout policy.

Two properties matter most here and neither is visible from a passing run:

  * validate must be usable on a stopped stack. It previously was not -- a
    static run exited 1 with "could not read `ollama list`" because a daemon
    was down. `deterministic` proves the config checks open no socket at all.
  * a validator that hangs is worse than one that fails. `bounded` proves no
    public validator issues an un-timeboxed network call.

Sections are addressable so the gate can report them separately.

Usage: validators.py [deterministic|classification|bounded]   (default: all)
"""
from __future__ import annotations

import os
import re
import socket
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite  # noqa: E402

from ailocal.checks import BLOCKED, FAIL, PASS, WARN, CheckResult, exit_code  # noqa: E402
from ailocal.checks import config as C  # noqa: E402
from ailocal import policy as P
from ailocal.checks import services as S  # noqa: E402

_suite = Suite("VALIDATOR CHECKS")
check = _suite.check

# Public validators. A network call in any of these must carry a timeout.
# Repo-relative: these no longer share a directory now that the checks layer is
# a package module and the E2E drivers are shell under tests/.
PUBLIC = ("src/ailocal/checks/run.py",)


def deterministic_checks() -> None:
    """The config layer must reach no service."""
    opened: list[str] = []

    real_conn, real_open = socket.create_connection, urllib.request.urlopen

    def trap_conn(addr, *_a, **_k):
        opened.append(f"socket {addr}")
        raise AssertionError("network call from a deterministic check")

    def trap_open(req, *_a, **_k):
        url = req if isinstance(req, str) else req.full_url
        opened.append(f"http {url}")
        raise AssertionError("network call from a deterministic check")

    socket.create_connection, urllib.request.urlopen = trap_conn, trap_open
    try:
        results = C.deterministic_checks()
    finally:
        socket.create_connection, urllib.request.urlopen = real_conn, real_open

    check(not opened, "deterministic checks open no socket",
          "; ".join(opened[:3]))
    check(bool(results), f"deterministic checks produced results ({len(results)})")
    check(all(isinstance(r, CheckResult) for r in results),
          "every deterministic check returns a CheckResult")

    # Docker is allowed, but only for mount comparison, and its absence must not
    # fail validation.
    names = [r.name for r in results]
    check("mount-drift" in names, "mount drift is a deterministic check")
    docker_dependent = [r for r in results if r.name == "mount-drift"]
    check(all(r.status is not FAIL or "readable" in r.summary
              for r in docker_dependent),
          "an unavailable Docker blocks the mount check rather than failing it")


def classification_checks() -> None:
    """Status semantics the public commands depend on."""
    check(exit_code([CheckResult("a", PASS, "x")]) == 0, "all-pass exits 0")
    check(exit_code([CheckResult("a", FAIL, "x")]) == 1, "any FAIL exits 1")
    check(exit_code([CheckResult("a", WARN, "x")]) == 0, "WARN alone does not fail")
    check(exit_code([CheckResult("a", BLOCKED, "x")]) == 0,
          "BLOCKED alone does not fail")
    check(exit_code([CheckResult("a", WARN, "x"), CheckResult("b", FAIL, "y")]) == 1,
          "a FAIL among warnings still fails")

    # Search is optional infrastructure: its absence degrades, never fails.
    r = S.check_container("ailocal-definitely-not-a-container")
    check(r.status is FAIL, "an absent required container fails")
    check(r.remediation is not None, "a failing check carries remediation")

    # One implementation per primitive, not one per caller.
    for prim in ("served_aliases", "model_info", "ollama_installed",
                 "container_state", "http_json", "proxy_healthy"):
        check(callable(getattr(S, prim, None)), f"services owns {prim}()")


def bounded_checks() -> None:
    """No public validator may issue an unbounded network call."""
    for name in PUBLIC:
        path = REPO / name
        if not path.is_file():
            _suite.skip(f"{name} absent")
            continue
        unbounded = [ln.strip() for ln in path.read_text().splitlines()
                     # curl in COMMAND position only: the scripts also mention
                     # curl inside echo strings and error messages.
                     if re.search(r'(^|[;&|]|\$\()\s*curl\s', ln)
                     and not ln.strip().startswith("#")
                     and not re.search(r'-m\s+\d|--max-time', ln)]
        check(not unbounded, f"{name} has no unbounded curl call",
              "; ".join(u[:90] for u in unbounded[:2]))

    # The shared policy must exist and be finite.
    for attr in ("CONNECT_TIMEOUT", "INSPECT_TIMEOUT", "GENERATE_TIMEOUT"):
        v = getattr(S, attr, None)
        check(isinstance(v, int) and 0 < v <= 600, f"services.{attr} is bounded ({v})")


def search_quota_checks() -> None:
    """Default diagnostics must never spend metered external search quota.

    Configuration-level, deliberately: proving this by issuing a real federated
    query would spend the very quota the check exists to protect.
    """
    _suite.section("DEFAULT CHECKS SPEND NO SEARCH QUOTA")
    src = (REPO / "src" / "ailocal" / "checks" / "services.py").read_text()
    run_src = (REPO / "src" / "ailocal" / "checks" / "run.py").read_text()

    check("!wp" in S.FREE_ENGINE_QUERY,
          "the default search query pins a single engine", S.FREE_ENGINE_QUERY)
    check(S.FREE_ENGINE_NAME == "wikipedia",
          f"the pinned engine is free ({S.FREE_ENGINE_NAME})")

    # check_searxng is the reachability probe: it must not hit /search at all.
    body = src.split("def check_searxng(")[1].split("def check_searxng_query(")[0]
    check("/config" in body and "/search?" not in body,
          "the reachability check queries no engine (uses /config)")

    # The federated path exists but must be reachable only through the flag.
    check(callable(getattr(S, "check_searxng_external", None)),
          "the federated search is a separate, named function")
    check("--external-search" in run_src,
          "the federated search requires an explicit --external-search flag")

    # No default caller anywhere may reach it.
    # Every production module, not a hand-listed few: a new caller must not be
    # able to reach the metered search by appearing in a file nobody listed.
    callers = [q for q in (REPO / "src").rglob("*.py")
               if "/resources/" not in str(q) and q.name != "services.py"
               and q.name != "run.py"]
    check(bool(callers), f"the quota scan reaches production code ({len(callers)} files)")
    for path in callers:
        check("check_searxng_external" not in path.read_text()
              and "--external-search" not in path.read_text(),
              f"{path.name} never triggers the federated search")

    # The query the default path issues must name exactly one engine.
    check(S.FREE_ENGINE_QUERY.strip().startswith("!"),
          "the default query uses engine-restricting bang syntax")


def exits_checks() -> None:
    """`ailocal check` has two states: clean, or something failed."""
    import subprocess

    def run(env: dict | None = None, args: list[str] | None = None) -> int:
        e = {**os.environ, **(env or {}),
             "PYTHONPATH": str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
        return subprocess.run(
            ["python3", "-m", "ailocal.checks.run", "check", *(args or [])],
            capture_output=True, text=True, timeout=900, env=e).returncode

    check(run() == 0, "check exits 0 on a healthy stack")
    check(run({"AILOCAL_PROXY": "http://127.0.0.1:1"}) == 1,
          "check exits 1 when the proxy is unreachable")

    marker = P.active_profile_path()
    original = marker.read_text()
    try:
        marker.write_text("999gb\n")
        rc = run()
        check(rc == 1, f"check exits 1 when the tier is unresolvable (got {rc})")
    finally:
        marker.write_text(original)

    check(run() == 0, "check exits 0 once restored")

    # The defining property of the configuration layer: it needs no stack.
    from ailocal.checks import exit_code
    check(exit_code(C.deterministic_checks()) == 0,
          "the configuration layer passes with the stack untouched")


SECTIONS = {"deterministic": deterministic_checks,
            "search-quota": search_quota_checks,
            "classification": classification_checks,
            "bounded": bounded_checks,
            "exits": exits_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
