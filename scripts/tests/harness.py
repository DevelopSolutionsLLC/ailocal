"""Common mechanics for the ailocal test suites.

Owns reporting, exit status, module loading, temporary state and subprocess
execution — the parts every suite reimplemented. It holds no ailocal policy:
domain assertions stay in the domain suites.

Standard library only, matching the rest of core ailocal.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

_GREEN, _RED, _YELLOW, _RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


class Suite:
    """Collects named checks and turns them into a process exit status.

    Output is stable: one `PASS`/`FAIL`/`SKIP` line per check. The gate keys off
    the exit status and greps FAIL lines only to render context, so the label is
    the diagnostic — make it say what was expected.
    """

    def __init__(self, title: str = "") -> None:
        self.failures: list[str] = []
        self.passed = 0
        self.skipped = 0
        if title:
            print(title)

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, ok: object, label: str, detail: str = "") -> bool:
        """Record one named check. Returns the boolean outcome for chaining."""
        ok = bool(ok)
        if ok:
            self.passed += 1
            print(f"  {_GREEN}PASS{_RESET}  {label}")
        else:
            self.failures.append(f"{label}: {detail}" if detail else label)
            print(f"  {_RED}FAIL{_RESET}  {label}")
            if detail:
                print(f"        {detail}")
        return ok

    def skip(self, label: str, reason: str = "") -> None:
        """Record a check that could not run. Never fails the suite."""
        self.skipped += 1
        suffix = f" — {reason}" if reason else ""
        print(f"  {_YELLOW}SKIP{_RESET}  {label}{suffix}")

    def error(self, label: str, exc: BaseException) -> None:
        """Record an unexpected exception as a failure rather than a traceback."""
        self.check(False, label, f"{type(exc).__name__}: {exc}")

    def report(self) -> int:
        """Print the summary and return the exit status: 0 clean, 1 otherwise."""
        print()
        if self.failures:
            print(f"FAILED ({len(self.failures)})")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        tail = f", {self.skipped} skipped" if self.skipped else ""
        print(f"all checks passed ({self.passed}{tail})")
        return 0


def load_module(name: str, path: os.PathLike | str) -> types.ModuleType:
    """Import a Python file that is not on sys.path, under an explicit name.

    Raises FileNotFoundError rather than surfacing an opaque loader error.
    """
    path = pathlib.Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_lib(name: str) -> types.ModuleType:
    """Import a module from scripts/lib by its module name."""
    return load_module(name, REPO / "scripts" / "lib" / f"{name}.py")


@contextlib.contextmanager
def temp_dir(prefix: str = "ailocal-test-"):
    """A temporary directory removed on exit, including after a failure."""
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def temp_home(prefix: str = "ailocal-home-"):
    """An isolated HOME and XDG_CONFIG_HOME, restored on exit.

    Tests that write client configuration must never touch the real ~/.config:
    a test that mutates the deployment to prove the deployment works is a second
    way to break it.
    """
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME")}
    with temp_dir(prefix) as home:
        (home / ".config").mkdir()
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
        try:
            yield home
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def run(cmd: list[str], timeout: int = 60, cwd: os.PathLike | str | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command, capturing output. A timeout is reported, never raised.

    On timeout the result carries returncode 124 and the reason in stderr, so a
    hung child fails one check instead of aborting the suite.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124, exc.stdout or "", f"timed out after {timeout}s")


def main(fn) -> None:
    """Run a suite entry point and exit with its status."""
    sys.exit(fn())
