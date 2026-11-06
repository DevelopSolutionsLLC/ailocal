"""The one set of terminal report helpers.

Every command prints progress through these, on stdout, so a caller that
captures one stream sees the whole report; three modules each carried a copy
with its own stream policy, and a warning landed on stderr in one command and
stdout in the next.
"""

from __future__ import annotations

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW, MAGENTA, CYAN = ("\033[32m", "\033[31m", "\033[33m",
                                     "\033[35m", "\033[1;36m")


def _c(text: object, colour: str) -> str:
    return f"{colour}{text}{RESET}"


def step(m): print(f"\n▶ {m}")
def ok(m):   print(f"  {_c('✓', GREEN)} {m}")
def bad(m):  print(f"  {_c('✗', RED)} {m}")
def warn(m): print(f"  {_c('⚠', YELLOW)} {m}")
def dim(m):  print(f"  {_c('—', DIM)} {m}")
def hdr(m):  print(f"\n{BOLD}{m}{RESET}")


info = ok      # one spelling per concept: a confirmed step is `ok`
skip = dim     # a step with nothing to do
