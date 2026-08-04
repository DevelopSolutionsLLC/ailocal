#!/usr/bin/env python3
"""Entry point behind `ailocal validate` and `ailocal smoke`.

Both commands are the same shape: collect CheckResults, render them, exit on
the outcome. They differ only in which checks they collect.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from checks import FAIL, exit_code, render  # noqa: E402
from checks import config as C  # noqa: E402

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


COMMANDS = {"validate": _validate}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: run.py {{{'|'.join(COMMANDS)}}} [options]")
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
