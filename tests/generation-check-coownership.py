#!/usr/bin/env python3
"""`--check` must not call a co-owner's key order "drift".

settings.json is CO-OWNED (see regen_claude_settings): Claude Code writes
`theme`, `enabledPlugins` and `extraKnownMarketplaces` into the same file and
serialises with its OWN key order — `env` first, the `//` banner last. The merge
re-emits template order, so a byte compare reported DRIFT on every run once the
co-owner had touched the file. `ailocal start` cleared it only until Claude Code
wrote again, which is what made the gate refuse with nothing actually stale.

OBSERVED before the fix: expected and live differed by 3 bytes and ZERO semantic
paths — key order alone.

NON-DESTRUCTIVE: renders into a temporary directory. Nothing live is read for
comparison and nothing is written outside tmp.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Suite, load_module  # noqa: E402

_suite = Suite()
check = _suite.check

from ailocal import generation as g  # noqa: E402


def _reordered(text):
    """The same document, serialised the way a co-owner would order it."""
    doc = json.loads(text)
    banner = {k: v for k, v in doc.items() if k == "//"}
    rest = {k: v for k, v in doc.items() if k != "//"}
    return json.dumps({**rest, **banner}, indent=2) + "\n"


def main() -> int:
    tier = g.resolve_tier(None)
    models = g.load_models_yaml(g.profile_path(tier=tier))
    clients = g.load_clients_yaml()
    g._STAGE.clear(); g._STAGED_TEXT.clear()
    g.regen_claude_settings(models, clients)
    staged = [t for p, t in g._STAGED_TEXT.items() if p.name == "settings.json"]
    g._STAGE.clear(); g._STAGED_TEXT.clear()
    check(len(staged) == 1, "the generator stages exactly one settings.json")
    if not staged:
        return _suite.report()
    text = staged[0]

    with tempfile.TemporaryDirectory() as td:
        dest = pathlib.Path(td) / "settings.json"

        dest.write_text(text)
        check(g._same(dest, text), "identical bytes are in sync")

        reordered = _reordered(text)
        check(reordered != text, "the reordered fixture really is a different file")
        check(json.loads(reordered) == json.loads(text),
              "the reordered fixture is semantically identical")
        dest.write_text(reordered)
        check(g._same(dest, text),
              "a co-owner's key order is NOT drift")

        changed = json.loads(text)
        changed["model"] = "ailocal-something-else"
        dest.write_text(json.dumps(changed, indent=2) + "\n")
        check(not g._same(dest, text),
              "a changed ailocal-owned value IS still drift")

        dest.write_text("{not json")
        check(not g._same(dest, text), "unparseable JSON is drift, not a crash")
    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
