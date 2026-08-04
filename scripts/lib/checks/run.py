#!/usr/bin/env python3
"""Entry point behind `ailocal validate` and `ailocal smoke`.

Both commands are the same shape: collect CheckResults, render them, exit on
the outcome. They differ only in which checks they collect.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from checks import FAIL, WARN, exit_code, render  # noqa: E402
from checks import config as C  # noqa: E402
from checks import host as H  # noqa: E402
from checks import services as S  # noqa: E402

BOLD, RESET = "\033[1;36m", "\033[0m"


def _validate(argv: list[str]) -> int:
    tier = None
    if "--profile" in argv:
        i = argv.index("--profile")
        tier = argv[i + 1] if i + 1 < len(argv) else None
    if "--runtime" in argv:
        print("  note: --runtime moved to `ailocal smoke` "
              "(validate is deterministic and needs no running stack)",
              file=sys.stderr)

    print(f"{BOLD}Deterministic validation{RESET}"
          + (f" — profile {tier}" if tier else ""))
    results = C.deterministic_checks(tier)
    render(results)
    code = exit_code(results)
    print()
    print("\033[32mVALIDATE: OK\033[0m" if code == 0
          else "\033[31mVALIDATE: FAILED\033[0m")
    return code


def _expected() -> tuple[list[str], dict[str, int], list[str]]:
    """Aliases, advertised geometry and required backends for the active tier."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import policy as P

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


def _smoke(argv: list[str]) -> int:
    alias = next((a for a in argv if not a.startswith("-")), "ailocal-fast")
    token = S.master_key()
    aliases, geometry, backends = _expected()

    print(f"{BOLD}Runtime smoke{RESET}")
    results = [S.check_docker(), S.check_container(), S.check_proxy_port(),
               S.check_proxy_health(), S.check_ollama(),
               S.check_models_present(backends)]
    results += S.check_aliases(token, aliases)
    results += S.check_geometry(token, geometry)
    results += [S.check_generation(token, alias), S.check_searxng(),
                S.check_search_tool_registered()]
    if "--deep" in argv:
        results.append(S.check_context_window(token))
    render(results)
    code = exit_code(results)
    print()
    print("\033[32mSMOKE: OK\033[0m" if code == 0 else "\033[31mSMOKE: FAILED\033[0m")
    return code


def _doctor(argv: list[str]) -> int:
    """0 healthy, 1 refuses (untrustworthy tier), 2 degraded findings.

    Exit 1 is a refusal, not a failure count: diagnosing against an assumed
    tier would report on a configuration the machine is not running.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import policy as P

    try:
        tier = P.resolve_active_tier()
        arch = P.resolve_role(tier, "architecture")
    except Exception as exc:
        print("  \033[31m✗\033[0m cannot resolve the active profile — refusing to "
              "report on an assumed tier", file=sys.stderr)
        print(f"      {getattr(exc, 'code', type(exc).__name__)}: {exc}", file=sys.stderr)
        return 1

    token = S.master_key()
    aliases, geometry, backends = _expected()

    print(f"{BOLD}Configuration{RESET}")
    results = C.deterministic_checks()
    render(results, remediation=True)

    print(f"\n{BOLD}Runtime{RESET}")
    runtime = [S.check_docker(), S.check_container(), S.check_proxy_health(),
               S.check_ollama(), S.check_models_present(backends)]
    runtime += S.check_aliases(token, aliases)
    runtime += [S.check_searxng(), S.check_search_tool_registered()]
    render(runtime, remediation=True)

    print(f"\n{BOLD}Host{RESET}")
    host = H.doctor_only_checks(arch["active"], arch["context_input"])
    render(host, remediation=True)

    # Degraded means a real failure. Warnings are advisory -- a cold model or a
    # misplaced store is expensive, not broken -- and match the previous
    # contract, where only an error marked the run unhealthy.
    failures = [r for r in results + runtime + host if r.status is FAIL]
    warnings = [r for r in results + runtime + host if r.status is WARN]
    print()
    if not failures:
        note = f" ({len(warnings)} advisory warning(s))" if warnings else ""
        print(f"▶ DOCTOR: OK — ailocal looks healthy{note}")
        return 0
    print(f"▶ DOCTOR: DEGRADED — {len(failures)} failing check(s) above", file=sys.stderr)
    return 2


COMMANDS = {"validate": _validate, "smoke": _smoke, "doctor": _doctor}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: run.py {{{'|'.join(COMMANDS)}}} [options]")
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
