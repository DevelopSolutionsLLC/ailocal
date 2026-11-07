#!/usr/bin/env python3
"""The session scratchpad hook (#78): one private directory per session, never under a
base someone else claimed, and a session id cannot escape it. Runs the shipped hook."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402

HOOK = RESOURCES / "clients/scratchpad-hook.sh"
S = Suite("SCRATCHPAD HOOK")


def run_hook(root: Path, sid: str) -> Path | None:
    out = subprocess.run(["bash", str(HOOK), "claude"], input=json.dumps({"session_id": sid}),
                         capture_output=True, text=True, timeout=10,
                         env=dict(os.environ, AILOCAL_SCRATCH_ROOT=str(root)))
    m = re.search(r"Session scratchpad: (\S+)", out.stdout)
    return Path(m[1]) if out.returncode == 0 and m else None


def main() -> int:
    with tempfile.TemporaryDirectory() as box:
        root = Path(box).resolve()
        base = root / f"ailocal-scratchpad-{os.getuid()}"
        made = run_hook(root, "../../escape/x")
        S.check(made is not None and made.parent == base and not (root / "escape").exists(),
                "a session id cannot place the scratchpad outside its base", str(made))
        S.check(made is not None and made.stat().st_mode & 0o777 == 0o700 and base.stat().st_mode & 0o777 == 0o700,
                "the scratchpad and its base are private (0700)")
        shutil.rmtree(base)
        (elsewhere := root / "attacker").mkdir()
        base.symlink_to(elsewhere)
        made = run_hook(root, "s1")
        S.check(made is not None and made.parent != base and not any(elsewhere.iterdir()),
                "a base pre-claimed by a symlink is never used", str(made))
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
