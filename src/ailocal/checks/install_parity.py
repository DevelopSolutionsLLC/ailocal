"""Is the package `ailocal` executes the package under test?

Two execution paths exist and only one of them is what production runs:

    tests/*            import src/ailocal from the CHECKOUT (harness.py sets
                       sys.path and PYTHONPATH together)
    `ailocal ...`      runs the PIPX-INSTALLED package, always

Nothing compared them, so a green gate said nothing about the code
`ailocal start` would execute. [REAL] that divergence silently reverted a
committed settings.json line: the repository held the new generator, the
installed package held the old one, `ailocal start` ran the old one, and the
only symptom was `generation --check` reporting drift — which reads like a
stale checkout, not a stale install.

This is deliberately a CONTENT comparison, not a version or commit comparison.
Nothing embeds git metadata in the package, `version` does not change on every
edit, and adding build metadata to answer "is this the same code" would be a
larger mechanism than the question deserves. After a correct
`pipx install --force .` the two trees are byte-identical, so their digests are
the whole answer.

PACKAGE drift is not GENERATED-CONFIG drift. `generation --check` owns the
second: whether the generated files match what the current generator would
write. This owns the first: whether the generator that will run is the one that
was tested. Both can fail independently and they have different remedies.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess

#: Build artefacts, not shipped source: present on one side or the other
#: depending on whether anything has imported the tree yet.
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}

#: The supported way back to parity. It ENDS with `ailocal start` on purpose:
#: `pipx install --force` recreates the venv and detaches the running
#: container's bind mount (AGENTS.md), so reinstalling alone leaves the stack
#: serving the config it booted with.
REMEDIATION = "pipx install --force . && ailocal start"


def fingerprint(package_root: pathlib.Path | str) -> str:
    """Content digest of a package tree: every shipped file, path AND bytes.

    Paths are hashed as well as contents so that adding or removing a file is a
    change even when no surviving file's bytes moved.
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
    """Where the `ailocal` command's own interpreter imports the package from.

    Asked of that interpreter rather than guessed from a path, because the
    console script is what production runs and it is the only authority on
    which environment answers for it.
    """
    exe = shutil.which("ailocal")
    if not exe:
        return None
    python = pathlib.Path(exe).resolve().parent / "python"
    if not python.exists():
        return None
    # PYTHONPATH MUST NOT REACH THIS SUBPROCESS. The gate runs with
    # PYTHONPATH=src so the suites import the checkout; inherited here, the
    # installed interpreter would import the CHECKOUT too, report its path, and
    # the comparison would trivially succeed against itself — a parity check
    # that can never fail. [REAL] this happened, and the acceptance run caught
    # it: a drifted checkout sailed through the precondition.
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
    """`(in_parity, explanation)` for the checkout at `repo`.

    Fails CLOSED on an absent or unreadable installation: "cannot tell" must
    not read as "fine" in a check whose entire purpose is to stop a runtime
    claim from resting on an assumption.
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
