"""Shared mechanics for the test suites: reporting, module loading, temp dirs.

Importing it points AILOCAL_DATA at the checkout's resources (unless already
set) and puts the checkout's src/ first for this process and its children.
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

REPO = pathlib.Path(__file__).resolve().parent.parent
RESOURCES = REPO / "src" / "ailocal" / "resources"

os.environ.setdefault("AILOCAL_DATA", str(RESOURCES))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
_pp = os.environ.get("PYTHONPATH", "")
if str(REPO / "src") not in _pp.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO / "src"), _pp) if p)

_GREEN, _RED, _YELLOW, _RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

#: Tests one suite (one component) may report. A suite past it fails, so the
#: limit is enforced rather than remembered (#113).
MAX_TESTS = 5


def _over_limit(count: int) -> str:
    return (f"{count} tests exceed the {MAX_TESTS}-per-component limit: merge or delete "
            "the ones a kept test already covers" if count > MAX_TESTS else "")


class Suite:
    """One PASS/FAIL/SKIP line per check; report() returns the exit status."""

    def __init__(self, title: str = "") -> None:
        self.failures: list[str] = []
        self.passed = self.skipped = 0
        if title:
            print(title)

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, ok: object, label: str, detail: str = "") -> bool:
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
        self.skipped += 1
        print(f"  {_YELLOW}SKIP{_RESET}  {label}{f' — {reason}' if reason else ''}")

    def error(self, label: str, exc: BaseException) -> None:
        self.check(False, label, f"{type(exc).__name__}: {exc}")

    def report(self) -> int:
        print()
        if over := _over_limit(self.passed + len(self.failures) + self.skipped):
            self.failures.append(over)
        if self.failures:
            print(f"FAILED ({len(self.failures)})")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        tail = f", {self.skipped} skipped" if self.skipped else ""
        print(f"all checks passed ({self.passed}{tail})")
        return 0


def unittest_main() -> None:
    """unittest.main() with the per-component limit applied to the tests it ran."""
    import unittest
    result = unittest.main(exit=False).result
    if over := _over_limit(result.testsRun):
        print(f"FAILED: {over}")
        sys.exit(1)
    sys.exit(0 if result.wasSuccessful() else 1)


def load_module(name: str, path: os.PathLike | str) -> types.ModuleType:
    """Import a file that is not on sys.path, registered under `name`."""
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


@contextlib.contextmanager
def temp_dir(prefix: str = "ailocal-test-"):
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def run(cmd: list[str], timeout: int = 60, cwd: os.PathLike | str | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """Capture output; a timeout returns code 124 instead of raising."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", f"timed out after {timeout}s")


def main(fn) -> None:
    sys.exit(fn())
