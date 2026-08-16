#!/usr/bin/env python3
"""install-parity.py — the gate must not pass against a stale installation.

THE DEFECT THIS FREEZES

The suites import `src/ailocal` from the checkout; `ailocal` and `ailocal start`
run the pipx-installed package. Nothing compared them, so:

    repository main held the new generator
    tests/gate.py passed against the checkout
    the installed package was older
    `ailocal start` ran the OLD generator
    the live settings.json silently lost a committed line

Green suite, correct repository, wrong runtime. `generation --check` exposed it
only afterwards, and its drift message reads like a stale checkout rather than a
stale install.

Package drift and generated-config drift are deliberately separate concerns with
separate remedies, so case D asserts they do not contaminate each other.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite  # noqa: E402

sys.path.insert(0, str(REPO / "src"))
from ailocal.checks import install_parity as IP  # noqa: E402

_suite = Suite("INSTALL PARITY")
check = _suite.check


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


FILES = {"__init__.py": "x = 1\n",
         "generation.py": "def main():\n    return 0\n",
         "resources/profiles/64gb.toml": "context_input = 163840\n"}


def main() -> int:
    print("FINGERPRINT")
    with tempfile.TemporaryDirectory() as td:
        a = _tree(Path(td) / "a", FILES)
        b = _tree(Path(td) / "b", FILES)
        check(IP.fingerprint(a) == IP.fingerprint(b),
              "identical trees fingerprint identically")

        # Build artefacts differ between an imported and a fresh tree and must
        # not register as a code difference.
        (b / "__pycache__").mkdir()
        (b / "__pycache__" / "generation.cpython-314.pyc").write_bytes(b"\x00\x01")
        (b / "generation.pyc").write_bytes(b"\x00\x02")
        check(IP.fingerprint(a) == IP.fingerprint(b),
              "__pycache__ and .pyc are ignored")

        (b / "generation.py").write_text("def main():\n    return 1\n")
        check(IP.fingerprint(a) != IP.fingerprint(b),
              "a one-character source change is detected")

        b2 = _tree(Path(td) / "b2", FILES)
        (b2 / "extra.py").write_text("y = 2\n")
        check(IP.fingerprint(a) != IP.fingerprint(b2),
              "an ADDED file is detected (paths are hashed, not just contents)")

        c = _tree(Path(td) / "c", FILES)
        (c / "resources/profiles/64gb.toml").write_text("context_input = 131072\n")
        check(IP.fingerprint(a) != IP.fingerprint(c),
              "a shipped RESOURCE change is detected, not only .py")

    print("\nCASE A — checkout == installed")
    # The live environment. The closeout reinstalled, so this is the real state
    # and it is what every other suite in the gate is implicitly assuming.
    ok, why = IP.compare(REPO)
    check(ok, f"parity holds against the real installation ({why})", why)

    print("\nCASE B — checkout changed after install")
    real = IP.installed_root
    with tempfile.TemporaryDirectory() as td:
        stale = Path(td) / "ailocal"
        shutil.copytree(REPO / "src" / "ailocal", stale,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stale / "generation.py").write_text(
            (stale / "generation.py").read_text() + "\n# drifted\n")
        IP.installed_root = lambda: stale
        try:
            ok, why = IP.compare(REPO)
            check(not ok, "a drifted installation is refused")
            check("STALE" in why.upper(), "the message names a stale install", why)
            check(IP.REMEDIATION == "pipx install --force . && ailocal start",
                  "the remediation reinstalls AND restarts (bind-mount hazard)",
                  IP.REMEDIATION)

            print("\nCASE C — reinstall the current checkout")
            shutil.rmtree(stale)
            shutil.copytree(REPO / "src" / "ailocal", stale,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            ok, why = IP.compare(REPO)
            check(ok, "parity is restored once the install matches", why)

            print("\nFAIL CLOSED")
            IP.installed_root = lambda: None
            ok, why = IP.compare(REPO)
            check(not ok, "an absent installation is refused, not assumed fine")
        finally:
            IP.installed_root = real

    print("\nCASE D — generated drift is a DIFFERENT concern")
    # The generated files live outside the package, so touching them cannot move
    # the package fingerprint. Conflating the two would make one remedy look
    # like it fixed the other.
    from ailocal import policy as P
    before = IP.fingerprint(REPO / "src" / "ailocal")
    gen = P.deployed_client_root() / "claude" / "settings.json"
    check(not str(gen).startswith(str(REPO / "src" / "ailocal")),
          "generated client config lives outside the package tree", str(gen))
    check(IP.fingerprint(REPO / "src" / "ailocal") == before,
          "package fingerprint is unaffected by generated state")
    src = (REPO / "tests" / "gate.py").read_text()
    check("_refuse_on_stale_install(repo)" in src
          and "_refuse_on_generated_drift()" in src,
          "the gate refuses on BOTH conditions, separately")
    check(src.index("_refuse_on_stale_install(repo)\n    _refuse_on_generated_drift")
          > 0, "stale install is checked before generated drift (cause first)")

    return _suite.report()


if __name__ == "__main__":
    sys.exit(main())
