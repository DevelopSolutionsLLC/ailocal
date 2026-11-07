"""Check the pipx-installed package `ailocal` runs is the checkout under test.

tests/* import the checkout; `ailocal ...` runs the installed package, so a
green gate proves nothing about the latter. Compared by content digest: the
package embeds no git metadata and `version` does not move per edit.
Package drift ≠ generated-cfg drift (`generation --check`): other remedy.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess

#: Build artefacts: present on either side depending on prior imports.
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}

#: Ends w/ `ailocal start`: reinstall detaches the container's bind mount.
REMEDIATION = "pipx install --force . && ailocal start"


def fingerprint(package_root: pathlib.Path | str) -> str:
    """Return a digest of every shipped file's path and bytes.

    Paths are hashed so adding or removing a file is a change.
    """
    root = pathlib.Path(package_root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _SKIP_DIRS & set(path.parts) or path.suffix in _SKIP_SUFFIXES:
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            continue
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def installed_root() -> pathlib.Path | None:
    """Return where the `ailocal` command's interpreter imports the package.

    Asked of that interpreter, the only authority on what production runs.
    """
    exe = shutil.which("ailocal")
    if not exe:
        return None
    python = pathlib.Path(exe).resolve().parent / "python"
    if not python.exists():
        return None
    # Drop the gate's PYTHONPATH=src, else this imports the checkout and the
    # parity check compares the checkout with itself and can never fail.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        out = subprocess.run(
            [str(python), "-c",
             "import ailocal, sys; sys.stdout.write(ailocal.__path__[0])"],
            capture_output=True, text=True, timeout=60, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    root = pathlib.Path(out.stdout.strip()) if out.stdout.strip() else None
    return root if root and root.is_dir() else None


def compare(repo: pathlib.Path) -> tuple[bool, str]:
    """Return `(in_parity, explanation)` for the checkout at `repo`.

    Fails closed on an absent or unreadable install: "cannot tell" ≠ "fine".
    """
    checkout = pathlib.Path(repo) / "src" / "ailocal"
    if not checkout.is_dir():
        return False, f"no package in the checkout at {checkout}"
    installed = installed_root()
    if installed is None:
        return False, ("`ailocal` is not installed, or its interpreter cannot "
                       "import it, so the runtime cannot be validated")
    want, got = fingerprint(checkout), fingerprint(installed)
    if want == got:
        return True, f"installed package matches the checkout ({want[:12]})"
    return False, (
        f"INSTALLED PACKAGE IS STALE — {installed} does not match {checkout} "
        f"(checkout {want[:12]}, installed {got[:12]}). `ailocal` and "
        f"`ailocal start` would run code that is not the code under test.")
