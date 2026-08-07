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

import sys


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


def main(argv: list[str]) -> int:
    if argv and argv[0] != "check":
        print("usage: python -m ailocal.checks.run check [--updates] "
              "[--external-search]", file=sys.stderr)
        return 2
    return _check(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
