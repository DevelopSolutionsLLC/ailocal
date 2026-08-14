#!/usr/bin/env python3
"""instruction-duplication.py — authored client instructions state a thing once.

Two failures were live in `~/.copilot/instructions/`:

  1. `ailocal.instructions.md` and `session-primer.md` were both deployed there
     with `applyTo: "**"`, so BOTH loaded on every turn and the terminal
     protocol was stated twice, in two wordings. Two statements of one rule is
     worse than one: they drift, and the model gets to choose.

  2. Both files carried a hand-written capability table naming backend models and
     context budgets — authored prose duplicating GENERATED state. It had
     drifted: three context budgets were wrong (80k/64k against a real 131072)
     and the `fast` capability was missing from both. Nothing could detect it,
     because nothing compared them.

These are properties of the SHIPPED resources, so they are checked against the
resource tree rather than a deployed machine.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COPILOT = REPO / "src" / "ailocal" / "resources" / "clients" / "copilot"
CLIENTS = REPO / "src" / "ailocal" / "resources" / "clients"

FAILS: list[str] = []

#: Model tags drift with every profile change. An authored instruction file that
#: names one is asserting a fact it cannot keep true.
MODEL_TAG = re.compile(r"\b(gemma\d|qwen[\d.]+|llama\d|nomic-embed|mistral)[\w.:-]*", re.I)
#: "80k in", "(64k)", "context_input=131072" — a budget written down by hand.
BUDGET = re.compile(r"\b\d+k\s*(in|input|context)\b|\bcontext_input\b|\b\d{4,6}\s*tokens\b", re.I)


def check(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


def always_on(directory: Path) -> list[Path]:
    """Instruction files that apply to every file, i.e. load on every turn."""
    out = []
    for p in sorted(directory.glob("*.md")):
        head = p.read_text(encoding="utf-8")[:400]
        if re.search(r'^applyTo:\s*"\*\*"', head, re.M):
            out.append(p)
    return out


def test_one_always_on_instruction_file_per_client() -> None:
    files = always_on(COPILOT)
    check(len(files) == 1,
          f"exactly one always-on Copilot instruction file (found {len(files)}: "
          f"{', '.join(p.name for p in files)})")


def test_no_authored_model_or_budget_facts() -> None:
    """Capability NAMES are stable and may be authored. Backends and budgets are
    generated, and belong to the profile and catalog."""
    for p in sorted(CLIENTS.rglob("*.md")) + sorted(CLIENTS.rglob("*.template")):
        body = p.read_text(encoding="utf-8")
        tag = MODEL_TAG.search(body)
        check(tag is None,
              f"{p.relative_to(CLIENTS)} names no backend model tag"
              + (f" (found {tag.group(0)!r})" if tag else ""))
        budget = BUDGET.search(body)
        check(budget is None,
              f"{p.relative_to(CLIENTS)} writes down no context budget"
              + (f" (found {budget.group(0)!r})" if budget else ""))


def test_the_terminal_protocol_is_stated_once() -> None:
    """The specific rule that was duplicated. Counted across every authored file
    a Copilot session loads, not within one file."""
    hits = [p for p in sorted(COPILOT.glob("*.md"))
            if "never append" in p.read_text(encoding="utf-8").lower()]
    check(len(hits) <= 1,
          f"the `exit` terminal rule is stated in at most one file (found {len(hits)})")


def test_shipped_instructions_carry_no_repository_local_policy() -> None:
    """These files are installed into a USER's client. This repository's own
    conventions — its author's name, its branch rules — are not the user's."""
    banned = ("victor", "vtchevalier", "develop solutions", "in this repo")
    for p in sorted(CLIENTS.rglob("*.md")):
        body = p.read_text(encoding="utf-8").lower()
        found = [b for b in banned if b in body]
        check(not found,
              f"{p.relative_to(CLIENTS)} ships no repository-local policy"
              + (f" (found {found})" if found else ""))


def main() -> int:
    for fn in (test_one_always_on_instruction_file_per_client,
               test_no_authored_model_or_budget_facts,
               test_the_terminal_protocol_is_stated_once,
               test_shipped_instructions_carry_no_repository_local_policy):
        print(f"\n{fn.__name__}")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            FAILS.append(f"{fn.__name__} raised {exc!r}")
            print(f"  ✗ {fn.__name__} raised {exc!r}")
    print(f"\n{'FAILED: ' + str(len(FAILS)) if FAILS else 'PASS'}")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
