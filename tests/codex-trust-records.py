#!/usr/bin/env python3
"""Regeneration must keep the trust records CODEX writes into config.toml.

config.toml is CO-OWNED (see _keep_codex_projects). Codex 0.163.0 records an
approved directory as `[projects."<path>"]` and a trusted hook as
`[hooks.state."<key>"]` with a `trusted_hash`. Dropping the hook record re-arms
"Hooks need review" on the next interactive session after every `ailocal start`.

OBSERVED before the fix: a live config carrying a hooks.state block came out of
the generator with zero hooks.state lines.

NON-DESTRUCTIVE: points CODEX_CONFIG at a temporary file. Nothing live is read
or written.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Suite  # noqa: E402

_suite = Suite()
check = _suite.check

from ailocal import generation as g  # noqa: E402

TEMPLATE = 'model = "ailocal-implementation"\n\n[model_providers.ailocal]\nname = "x"\n'
HOOK = ('[hooks.state."/x/codex/config.toml:session_start:0:0"]\n'
        'trusted_hash = "sha256:6ef5bb"\n')
PROJECT = '[projects."/x/repo"]\ntrust_level = "trusted"\n'


def main() -> int:
    original = g.CODEX_CONFIG
    with tempfile.TemporaryDirectory() as td:
        g.CODEX_CONFIG = pathlib.Path(td) / "config.toml"
        try:
            check(g._keep_codex_projects(TEMPLATE) == TEMPLATE,
                  "no live file: the template is returned unchanged")

            g.CODEX_CONFIG.write_text(
                TEMPLATE + "\n" + PROJECT + "\n[hooks.state]\n\n" + HOOK)
            out = g._keep_codex_projects(TEMPLATE)
            check(PROJECT in out, "a directory trust record survives regeneration")
            check(HOOK in out, "a hook trust record (trusted_hash) survives regeneration")
            check(out.count("[model_providers.ailocal]") == 1,
                  "ailocal-owned tables are not duplicated")

            g.CODEX_CONFIG.write_text(TEMPLATE + 'stale_hand_edit = true\n\n' + HOOK)
            out = g._keep_codex_projects(TEMPLATE)
            check("stale_hand_edit" not in out,
                  "nothing but trust records is carried over")

            g.CODEX_CONFIG.write_text(out)
            check(g._keep_codex_projects(out) == out,
                  "carrying records over is a fixed point")
        finally:
            g.CODEX_CONFIG = original
    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
