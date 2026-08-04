#!/usr/bin/env python3
"""Entry point behind `ailocal validate` and `ailocal smoke`.

Both commands are the same shape: collect CheckResults, render them, exit on
the outcome. They differ only in which checks they collect.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from checks import exit_code, render  # noqa: E402
from checks import config as C  # noqa: E402
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


def _master_key() -> str:
    env = pathlib.Path(__file__).resolve().parent.parent.parent.parent / ".env"
    for line in env.read_text().splitlines() if env.is_file() else []:
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _expected() -> tuple[list[str], dict[str, int], list[str]]:
    """Aliases, advertised geometry and required backends for the active tier."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    import profile_config as P

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
    token = _master_key()
    aliases, geometry, backends = _expected()

    print(f"{BOLD}Runtime smoke{RESET}")
    results = [S.check_docker(), S.check_container(), S.check_proxy_port(),
               S.check_proxy_health(), S.check_ollama(),
               S.check_models_present(backends)]
    results += S.check_aliases(token, aliases)
    results += S.check_geometry(token, geometry)
    results += [S.check_generation(token, alias), S.check_searxng(),
                S.check_search_tool_registered()]
    render(results)
    code = exit_code(results)
    print()
    print("\033[32mSMOKE: OK\033[0m" if code == 0 else "\033[31mSMOKE: FAILED\033[0m")
    return code


COMMANDS = {"validate": _validate, "smoke": _smoke}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: run.py {{{'|'.join(COMMANDS)}}} [options]")
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
