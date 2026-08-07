#!/usr/bin/env python3
"""Entry point behind `ailocal check` — the one answer to "is ailocal
configured and working?".

One engine: collect CheckResults from every layer, render them through the one
renderer, exit on the one rule. There are no modes. Every finding carries its
fix.

  configuration  Deterministic consistency read from files on disk. Opens no
                 socket, so it reports the same thing with the stack stopped.
  runtime        Containers, proxy health, served aliases, advertised
                 geometry, Ollama inventory, one bounded model response,
                 search. Every call carries a timeout.
  supply chain   Every declared image pinned by digest, the running image
                 identical to the declared one, services on loopback only,
                 provenance where a publisher signs.
  installation   Client configs, login services and the port they claim.
  host           Machine-level guidance: .env permissions, tooling, the model
                 store, residency and parallelism.

Exit 0 when nothing failed, 1 otherwise. A warning is advisory and does not
fail the run; an unresolvable active tier does, because diagnosing against an
assumed tier reports on a configuration the machine is not running.

Two flags, both opt-in because both reach the network beyond the local stack:
`--updates` asks upstream what image versions exist (it never pulls and never
rewrites a pin), and `--external-search` issues one federated query, which
DOES consume metered Brave allowance. Never use it in a loop.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys
import time


from ailocal.checks import FAIL, WARN, render  # noqa: E402
from ailocal.checks import config as C  # noqa: E402
from ailocal.checks import host as H  # noqa: E402
from ailocal.checks import services as S  # noqa: E402

BOLD, RESET = "\033[1;36m", "\033[0m"


def _expected() -> tuple[list[str], dict[str, int], list[str]]:
    """Aliases, advertised geometry and required backends for the active tier."""
    from ailocal import policy as P

    tier = P.resolve_active_tier()
    profile = P.load_profile(tier)
    aliases, geometry, backends = [], {}, []
    for role in P.ROLES:
        if role not in profile:
            continue
        cfg = P.resolve_role(tier, role)
        if not cfg.get("enabled", True):
            continue
        aliases.append(f"ailocal-{role}")
        if role != "embeddings":
            geometry[f"ailocal-{role}"] = cfg["context_input"]
        if cfg.get("active"):
            backends.append(cfg["active"])
    return aliases, geometry, sorted(set(backends))


def _check(argv: list[str]) -> int:
    """0 when nothing failed, 1 otherwise."""
    from ailocal import policy as P
    from ailocal import install

    try:
        tier = P.resolve_active_tier()
        arch = P.resolve_role(tier, "architecture")
    except Exception as exc:  # noqa: BLE001
        print("  \033[31m✗\033[0m cannot resolve the active profile — refusing to "
              "report on an assumed tier", file=sys.stderr)
        print(f"      {getattr(exc, 'code', type(exc).__name__)}: {exc}",
              file=sys.stderr)
        return 1

    token = S.master_key()
    aliases, geometry, backends = _expected()
    results: list = []

    def section(heading: str, checks: list) -> None:
        print(f"{BOLD}{heading}{RESET}")
        render(checks)
        results.extend(checks)
        print()

    section("Configuration", C.deterministic_checks())

    runtime = [S.check_docker(), S.check_container(), S.check_litellm_version(),
               S.check_proxy_health(), S.check_ollama(),
               S.check_models_present(backends)]
    runtime += S.check_aliases(token, aliases)
    runtime += S.check_geometry(token, geometry)
    runtime += [S.check_generation(token), S.check_context_window(token),
                S.check_searxng(),
                S.check_searxng_query(), S.check_brave_key_configured(),
                S.check_search_tool_registered()]
    if "--external-search" in argv:
        runtime.append(S.check_searxng_external())
    section("Runtime", runtime)

    section("Supply chain", S.supply_chain_checks("--updates" in argv))
    section("Installation", install.audit())
    section("Host", H.doctor_only_checks(arch["active"], arch["context_input"]))

    failures = [r for r in results if r.status is FAIL]
    warnings = [r for r in results if r.status is WARN]
    if failures:
        print(f"\033[31mCHECK: {len(failures)} failing check(s) above\033[0m",
              file=sys.stderr)
        return 1
    note = f" ({len(warnings)} advisory warning(s))" if warnings else ""
    print(f"\033[32mCHECK: OK\033[0m{note}")
    return 0


# ── the regression gate ─────────────────────────────────────────────────────
# "Could not run" is failure, never a skip: several suites need the registry and
# PyYAML, which exist only inside the proxy image, so a host-only run would
# cover a fraction of the behaviour and still print green.

#: Shipped assets live inside the package; the checkout path to them is
#: spelled once.
RES = "src/ailocal/resources"

GATE_SLOW_S = int(os.environ.get("AILOCAL_GATE_SLOW_S", "10"))


def _repo() -> pathlib.Path:
    """The checkout the suites live in. `ailocal test` is a developer command."""
    here = pathlib.Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "tests").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    sys.exit("ailocal test runs the repository's suites; run it from a checkout.")


def _gate_preconditions(repo: pathlib.Path) -> None:
    """Refuse rather than run a reduced set and report success."""
    container = S.CONTAINER
    state, health = S.container_state(container)
    if state != "running":
        sys.exit(f"\n  {container} is not running. The registry, negotiator and "
                 "compatibility suites all need it.\n      ailocal start")
    if health not in ("healthy", ""):
        sys.exit(f"\n  {container} health is {health!r}, not healthy. Fix that "
                 "before trusting any result.")
    # Container health means the proxy PROCESS is up, not that the router serves
    # /v1/models. 401 counts as ready: it proves the route answers.
    for _ in range(60):
        try:
            S.http_json(f"{S.PROXY}/v1/models", timeout=5)
            return
        except S.Unreachable as exc:
            if "401" in str(exc):
                return
        time.sleep(1)
    sys.exit(f"\n  {container} is healthy but /v1/models did not serve within 60s."
             "\n  Refusing to run: PRECONDITION NOT MET.")


def _gate_suites(repo: pathlib.Path, full: bool) -> list:
    py = sys.executable
    suites = [
        ("UNIT / BEHAVIOUR", [
            ("capability registry (+ no-hard-coded-literals assertion)",
             ["/bin/bash", "tests/in-container.sh",
                    "tests/capability-registry-impl.py",
                    "AILOCAL_GATEWAY_SOURCE=/app/config/hooks/tool_gateway.py"]),
            ("capability negotiator (byte accounting, modes, passthrough)",
             ["/bin/bash", "tests/in-container.sh",
                    "tests/tool-gateway-impl.py",
                    "AILOCAL_GATEWAY_MODULE=/app/config/hooks/tool_gateway.py"]),
            ("persona injection", [py, "tests/gateway.py", "persona"]),
            ("tool-call repair (repairs real calls, refuses examples)",
             [py, "tests/gateway.py", "repair"]),
            ("E1 trace schema, redaction and token reconciliation",
             [py, "tests/gateway.py", "trace"]),
            ("profile resolver (single reader, fail-closed, no 64gb default)",
             [py, "tests/profiles.py", "resolver"]),
            ("policy ownership (one reader, client policy fails closed)",
             [py, "tests/profiles.py", "policy"]),
            ("hardware profiles (schema, tiers, dedup)",
             [py, "tests/profiles.py", "hardware"]),
            ("Python LSP baseline for claude-local (real documentSymbol)",
             [py, "tests/lsp-baseline.py"]),
        ]),
        ("INTEGRATION", [
            ("client role alias overrides (defaults intact, fails closed)",
             ["/bin/bash", "tests/clients.sh", "roles"]),
            ("codex MCP is withheld (no grepai/lsp/github, no re-sync)",
             ["/bin/bash", "tests/clients.sh", "codex"]),
            ("shell output helpers (streams, colour, one owner)",
             ["/bin/bash", "tests/shell-output.sh"]),
            ("validator checks (deterministic, bounded, search quota)",
             [py, "tests/validators.py"]),
            ("generation rolls back on partial failure (never mixed on disk)",
             [py, "tests/generation-rollback.py"]),
            ("install: provisioning, provenance and tier selection",
             [py, "tests/install.py"]),
            ("client compatibility probes (/api/hello, no side effects)",
             ["/bin/bash", "tests/compat-routes.sh"]),
        ]),
        ("INVARIANTS", [
            ("ailocal sync is a fixed point", _fixed_point),
            ("litellm runtime matches the validated version", _version_current),
            ("all shell scripts parse (bash -n)", _shell_parses),
            ("all python modules parse", _python_parses),
            ("client timeout is not below the proxy timeout", _timeouts_aligned),
            ("every registered hook imports inside the proxy image", _hooks_import),
            ("installers are idempotent",
             ["/bin/bash", "tests/idempotent-install.sh"]),
            ("installation audit runs cleanly", _audit_runs),
        ]),
    ]
    if full:
        suites[1][1].insert(0, ("client compatibility (3 dialects x 3 modes)",
                                ["/bin/bash", "tests/client-compatibility.sh"]))
    return suites


def _fixed_point(repo: pathlib.Path) -> tuple[int, str]:
    """The generated config IS the deployed config, so a generator that is not a
    fixed point means the proxy and the repository can silently disagree."""
    from ailocal import policy as P
    generated = P.state_root() / "litellm" / "config.yaml"
    before = generated.read_bytes() if generated.is_file() else b""
    r = subprocess.run([sys.executable, "-m", "ailocal.generation"],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode:
        return 1, r.stdout + r.stderr
    return (0, "") if generated.read_bytes() == before else         (1, "ailocal sync is not a fixed point")


def _version_current(repo: pathlib.Path) -> tuple[int, str]:
    r = S.check_litellm_version()
    return (0 if r.status is not FAIL else 1), r.summary


def _shell_parses(repo: pathlib.Path) -> tuple[int, str]:
    bad = []
    for pattern in ("ailocal", "tests/**/*.sh",
                    f"{RES}/clients/*.sh", f"{RES}/clients/*.zsh"):
        for f in sorted(repo.glob(pattern)):
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            if r.returncode:
                bad.append(f"{f.name}: {r.stderr.strip()}")
    return (1, "\n".join(bad)) if bad else (0, "")


def _python_parses(repo: pathlib.Path) -> tuple[int, str]:
    bad = []
    for pattern in ("src/**/*.py", "tests/**/*.py"):
        for f in sorted(repo.glob(pattern)):
            try:
                ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                bad.append(f"{f}: {exc}")
    return (1, "\n".join(bad)) if bad else (0, "")


def _timeouts_aligned(repo: pathlib.Path) -> tuple[int, str]:
    """The client must never give up before the proxy, or it abandons requests
    the proxy is still serving while the backend generates into a closed socket.
    """
    proxy = re.search(r"^ *timeout: *(\d+)",
                      (repo / RES / "deploy/litellm/config.template.yaml").read_text(),
                      re.M)
    client = re.search(r"AILOCAL_API_TIMEOUT_MS:-(\d+)}",
                       (repo / RES / "clients/configure.template.zsh").read_text())
    if not (proxy and client):
        return 1, "could not read both timeouts"
    if int(client.group(1)) < int(proxy.group(1)) * 1000:
        return 1, (f"client API_TIMEOUT_MS {client.group(1)} is BELOW the LiteLLM "
                   f"timeout {proxy.group(1)}s")
    return 0, ""


def _hooks_import(repo: pathlib.Path) -> tuple[int, str]:
    """A registered-but-unimportable callback takes the container down at boot,
    and a sibling import that works on the host fails under LiteLLM's loader."""
    program = (
        "import importlib.util, sys\n"
        "bad = []\n"
        "for name in ['persona_injector','reasoning_router','startup',"
        "'tool_repair','tool_gateway','capability_registry']:\n"
        "    try:\n"
        "        spec = importlib.util.spec_from_file_location("
        "name, f'/app/config/hooks/{name}.py')\n"
        "        mod = importlib.util.module_from_spec(spec)\n"
        "        sys.modules[name] = mod\n"
        "        spec.loader.exec_module(mod)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {type(exc).__name__}: {exc}')\n"
        "print(chr(10).join(bad))\n"
        "sys.exit(1 if bad else 0)\n")
    r = subprocess.run(["docker", "exec", "-i", S.CONTAINER, "python", "-"],
                       input=program, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def _audit_runs(repo: pathlib.Path) -> tuple[int, str]:
    """Findings are a normal working state. Only the audit itself breaking
    fails the gate."""
    from ailocal import install
    try:
        install.audit()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    return 0, ""


def _gate(argv: list[str]) -> int:
    full = "--full" in argv
    repo = _repo()
    _gate_preconditions(repo)
    print("═" * 70 + "\n ailocal regression gate\n" + "═" * 70)

    passed = failed = 0
    failures, slow = [], []
    for heading, entries in _gate_suites(repo, full):
        print(f"\n{heading}")
        for label, runner in entries:
            started = time.monotonic()
            if callable(runner):
                try:
                    rc, out = runner(repo)
                except Exception as exc:  # noqa: BLE001
                    rc, out = 1, f"{type(exc).__name__}: {exc}"
            else:
                r = subprocess.run(runner, cwd=repo, capture_output=True, text=True)
                rc, out = r.returncode, r.stdout + r.stderr
            seconds = int(time.monotonic() - started)
            mark = (f" \033[33m[{seconds}s]\033[0m" if seconds >= GATE_SLOW_S
                    else f" ({seconds}s)" if seconds >= 2 else "")
            if seconds >= GATE_SLOW_S:
                slow.append(f"{label} ({seconds}s)")
            if rc == 0:
                print(f"  \033[32mPASS\033[0m  {label}{mark}")
                passed += 1
            else:
                print(f"  \033[31mFAIL\033[0m  {label}{mark}")
                for line in [l for l in out.splitlines()
                             if re.search(r"FAIL|[Ee]rror|Traceback|not idempotent",
                                          l)][:6]:
                    print(f"          {line}")
                failed += 1
                failures.append(label)

    print("\n" + "═" * 70)
    if failed:
        print(f" REGRESSION GATE: {failed} FAILED, {passed} passed")
        for label in failures:
            print(f"   - {label}")
        return 1
    print(f" REGRESSION GATE: all {passed} checks passed")
    if slow:
        print(f" {len(slow)} check(s) at/over {GATE_SLOW_S}s — keep the gate fast "
              "enough to run:")
        for label in slow:
            print(f"   {label}")
    if not full:
        print(" (add --full for the client compatibility matrix)")
    return 0


COMMANDS = {"check": _check, "test": _gate}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m ailocal.checks.run <{'|'.join(COMMANDS)}> [options]",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
