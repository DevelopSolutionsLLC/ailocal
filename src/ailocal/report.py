"""Terminal report helpers; all on stdout so one captured stream is whole."""

from __future__ import annotations

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW, MAGENTA, CYAN = ("\033[32m", "\033[31m", "\033[33m",
                                     "\033[35m", "\033[1;36m")


def _c(text: object, colour: str) -> str:
    return f"{colour}{text}{RESET}"


def step(m: object) -> None: print(f"\n▶ {m}")
def ok(m: object) -> None:   print(f"  {_c('✓', GREEN)} {m}")
def bad(m: object) -> None:  print(f"  {_c('✗', RED)} {m}")
def warn(m: object) -> None: print(f"  {_c('⚠', YELLOW)} {m}")
def dim(m: object) -> None:  print(f"  {_c('—', DIM)} {m}")
def hdr(m: object) -> None:  print(f"\n{BOLD}{m}{RESET}")


info = ok
skip = dim
