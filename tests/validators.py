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
import tempfile
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, RESOURCES, Suite  # noqa: E402

from ailocal.checks import BLOCKED, FAIL, PASS, SKIP, WARN, CheckResult, exit_code  # noqa: E402
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
    shipped = P.load_client_policy(RESOURCES)
    with patch.object(P, "load_client_policy", return_value=shipped):
        check(all(r.status is PASS for r in C.check_client_mappings()),
              "shipped delegate ceilings are not treated as model targets")
    import copy
    for field in ("launch_default", "slots"):
        policy = copy.deepcopy(shipped)
        if field == "slots":
            policy["claude"][field]["opus"] = "missing-capability"
        else:
            policy["claude"][field] = "missing-capability"
        with patch.object(P, "load_client_policy", return_value=policy):
            check(any(r.status is FAIL and "missing-capability" in r.summary
                      for r in C.check_client_mappings()),
                  f"unknown Claude {field} routing target fails the health check")
    policy = copy.deepcopy(shipped)
    policy['compat']['fixture-metadata'] = 'disk_gb'
    with patch.object(P, "load_client_policy", return_value=policy):
        check(any(r.status is FAIL and "disk_gb" in r.summary
                  for r in C.check_client_mappings()),
              "profile metadata cannot satisfy a model routing target")
    policy = copy.deepcopy(shipped)
    policy["codex"]["delegate_max_mode"] = "invalid-mode"
    with patch.object(P, "load_client_policy", return_value=policy):
        check(any(r.status is FAIL and "delegate_max_mode" in (r.detail or "")
                  for r in C.check_client_mappings()),
              "invalid delegation ceiling fails the health check")
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
    with patch.object(S.policy, "effective_summary", return_value={"roles": {
            "completion": {"enabled": False, "max_input_tokens": 100},
            "implementation": {"enabled": True, "max_input_tokens": 2000},
            "cyber": {"enabled": True, "max_input_tokens": 500}}}), \
            patch.object(S, "http_json", return_value={
                "error": "ContextWindowExceededError"}) as request:
        verdict = S.check_context_window("fixture-key")
        payload = request.call_args.kwargs["payload"]
        check(verdict.status == PASS and payload["model"] == "ailocal-cyber",
              "admission probe selects an enabled capability when completion is disabled")
        check(len(payload["messages"][0]["content"].split()) > 500,
              "admission probe exceeds the selected capability's own input limit")
        request.reset_mock()
        check(S.check_context_window("fixture-key", "ailocal-completion").status == FAIL
              and not request.called,
              "an explicitly disabled probe target fails without model inference")
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

    # The detached-mount fault a package reinstall leaves behind is repaired by
    # `ailocal start`. It used to say `docker restart <container>`: correct, but
    # it asks the reader to know the container's name and leaves compose and
    # generated-config changes unreconciled. Nobody should have to understand
    # Docker inode behaviour to recover from `pipx install`.
    import inspect
    mount_src = inspect.getsource(S.check_config_mount)
    check("remediation=\"ailocal start\"" in mount_src,
          "the detached-mount fault points at the canonical repair command")
    check("docker restart" not in mount_src.split('"""')[-1],
          "it no longer hands out a raw docker command as the fix")

    # One implementation per primitive, not one per caller.
    for prim in ("served_aliases", "model_info", "ollama_installed",
                 "container_state", "http_json", "proxy_healthy"):
        check(callable(getattr(S, prim, None)), f"services owns {prim}()")


def bounded_checks() -> None:
    """No public validator may issue an unbounded network call."""
    with patch.object(S, "ollama_installed", return_value={"fixture:gguf", "alias:latest"}):
        check(S.check_models_present(["fixture:mlx"]).status == FAIL,
              "a different quantization/tag cannot satisfy a required model")
        check(S.check_models_present(["fixture:gguf", "alias"]).status == PASS,
              "exact tags and omitted :latest resolve normally")
    check(S.runtime._find({"fixture:gguf": {"size": 1}}, "fixture:mlx") is None,
          "residency cannot borrow another variant's memory or expiry")
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


def gate_drift_checks() -> None:
    """The gate must REFUSE on generated drift, not discover it mid-run.

    Drift used to surface as three failing checks in `exits` above, because
    `ailocal check` correctly exits 1 while the generated config is stale. A
    later gate suite then regenerated it, so the same gate passed on rerun and
    the drift went unreported — measured at 2 of 4 consecutive runs.

    Decision logic only: the real drift check is stubbed, so this stays fast and
    carries no timing component. It is the ORDER that regressed, not detection.
    """
    from harness import load_module
    gate = load_module("gate", REPO / "tests/gate.py")
    from ailocal.checks import config as cfg

    original = cfg.check_generated_in_sync

    def stub(status):
        return lambda: CheckResult(
            "generated-sync", status,
            "generated files have drifted from their source",
            "DRIFT — generated files are stale: litellm/config.yaml",
            "ailocal start regenerates them")

    def refuse(status):
        """(refused?, message) for a drift check reporting `status`."""
        cfg.check_generated_in_sync = stub(status)
        try:
            gate._refuse_on_generated_drift()
            return False, ""
        except SystemExit as exc:
            return True, str(exc.code)
        finally:
            cfg.check_generated_in_sync = original

    refused, message = refuse(FAIL)
    check(refused, "drift stops the gate before any suite runs")
    # A refusal that does not say what is stale just moves the confusion earlier.
    check("litellm/config.yaml" in message, "the refusal names the stale artifact")
    check("ailocal start" in message, "the refusal carries the remediation")
    for status, label in ((PASS, "an in-sync tree"), (WARN, "a WARN"),
                          (BLOCKED, "a generator that could not run")):
        check(not refuse(status)[0], f"{label} does not stop the gate")


def supply_scope_checks() -> None:
    """Other projects and isolated installs must not change this install's verdict."""
    names = {"foreign": "/unrelated-service", "isolated": "/test-service",
             "local": "/custom-container-name"}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "compose" in argv and "ps" in argv:
            value = "local"
        elif argv[1:3] == ["ps", "-q"]:
            value = "foreign isolated local"
        elif argv[1] == "port":
            value = "4000/tcp -> 127.0.0.1:4000"
        elif argv[-1] == "{{.Name}}":
            value = names[argv[2]]
        elif argv[-1] == "{{.Config.Image}}":
            value = "example/proxy:test@sha256:expected"
        elif argv[-1] == "{{.Image}}":
            value = "sha256:image"
        else:
            value = "example/proxy@sha256:expected"
        return SimpleNamespace(stdout=value, returncode=0)

    with patch.object(S, "_run", side_effect=run):
        results = S.check_loopback()
        name, digest = S._running_digest("example/proxy")
    check(len(results) == 1 and "custom-container-name" in results[0].summary,
          "reachability reports only this installation's containers")
    check([a for a in calls if a[1] == "port"] == [["docker", "port", "local"]],
          "unrelated projects and isolated test containers are not audited")
    check((name, digest) == ("custom-container-name", "sha256:expected"),
          "digest comparison selects this installation, not another container of the image")
    check(not any(a[1:3] == ["ps", "-q"] for a in calls),
          "supply-chain checks never enumerate all projects on the host")

    with tempfile.TemporaryDirectory() as box, \
            patch.object(P, "deployed_client_root", return_value=Path(box)):
        check(C.check_local_artifacts().status is SKIP,
              "a fresh install does not require an optional Claude integration")
        home = Path(box) / "claude"
        home.mkdir()
        (home / ".claude.json").write_text('{"mcpServers": {"artifact": {"command": "missing"}}}')
        check(C.check_local_artifacts().status is FAIL,
              "an explicitly registered artifact runtime must exist")


SECTIONS = {"deterministic": deterministic_checks,
            "supply-scope": supply_scope_checks,
            "search-quota": search_quota_checks,
            "classification": classification_checks,
            "bounded": bounded_checks,
            "gate-drift": gate_drift_checks,
            "exits": exits_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
