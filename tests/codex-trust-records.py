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
import tomllib

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

            g.CODEX_CONFIG.write_text(
                TEMPLATE + "\n" + HOOK + "\n[tui]\ntheme = \"hand-edit\"\n"
                "screen_reader_detection_done = true\n")
            out = g._keep_codex_projects(TEMPLATE)
            check("[tui]\nscreen_reader_detection_done = true\n" in out,
                  "Codex's screen-reader flag survives regeneration")
            check("hand-edit" not in out, "other [tui] keys are not carried over")
            check(out.count("[tui]") == 1, "the flag lands in exactly one [tui] table")
            g.CODEX_CONFIG.write_text(out)
            check(g._keep_codex_projects(TEMPLATE) == out,
                  "carrying the flag over is a fixed point")
            check("screen_reader" not in g._keep_codex_projects(TEMPLATE + "\n[tui]\n"),
                  "a template [tui] table is never duplicated")

            # OBSERVED: a flag pattern ending in `.*` under DOTALL captured the
            # rest of the file, and the hook record after [tui] was written twice.
            g.CODEX_CONFIG.write_text(TEMPLATE + "\n[tui]\nscreen_reader_detection_done = true\n\n"
                                      + HOOK + "\n# ── Model Providers ──\n")
            out = g._keep_codex_projects(TEMPLATE)
            check(out.count("[hooks.state.") == 1, "a table after [tui] is not carried twice")
            check("Model Providers" not in out,
                  "a comment that precedes the next table is not carried with a record")
            g.CODEX_CONFIG.write_text(TEMPLATE + "\n" + HOOK + "\n" + HOOK)
            check(g._keep_codex_projects(TEMPLATE).count("[hooks.state.") == 1,
                  "a live file holding a record twice is repaired, not carried")
            check(tomllib.loads(out).get("tui") == {"screen_reader_detection_done": True},
                  "the merged file is valid TOML")
        finally:
            g.CODEX_CONFIG = original
    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
