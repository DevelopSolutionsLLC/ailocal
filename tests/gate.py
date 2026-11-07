#!/usr/bin/env python3
"""Run every suite once: the regression gate.

Usage: python3 tests/gate.py [--full]. A suite that cannot run fails;
nothing is skipped to green.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from ailocal.checks import FAIL
from ailocal.checks import services as S

RES = "src/ailocal/resources"
# Shell suites call this interpreter, not an `ailocal` on PATH.
os.environ.setdefault("AILOCAL_PY", sys.executable)
SLOW_S = int(os.environ.get("AILOCAL_GATE_SLOW_S", "10"))
ROOT_ALLOWED = {".gitignore", "AGENTS.md", "CHANGELOG.md", "LICENSE",
                "README.md", "pyproject.toml",
                "docs", "src", "tests"}


def _repo() -> pathlib.Path:
    here = pathlib.Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "tests").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    sys.exit("run the gate from a checkout.")


def _refuse(summary: str, remedy: str) -> None:
    sys.exit(f"\n  {summary}\n\n  {remedy}\n  Refusing to run: PRECONDITION NOT MET.")


def _preconditions(repo: pathlib.Path) -> None:
    """Refuse states a later suite would silently repair, greening a rerun.

    Stale install, stale generated cfg, missing local-agent runtime.
    """
    from ailocal import policy as P
    from ailocal.checks.config import check_generated_in_sync
    from ailocal.checks.install_parity import REMEDIATION, compare

    state, health = S.container_state(S.CONTAINER)
    if state != "running":
        _refuse(f"{S.CONTAINER} is not running.", "ailocal start")
    if health not in ("healthy", ""):
        _refuse(f"{S.CONTAINER} health is {health!r}.", "fix the proxy first")
    ok, why = compare(repo)            # before drift: a stale install causes drift
    if not ok:
        _refuse(why, REMEDIATION)
    drift = check_generated_in_sync()
    if drift.status == FAIL:
        _refuse(f"{drift.summary}\n      {(drift.detail or '').strip()}",
                drift.remediation or "ailocal start")
    agent_py = P.state_root() / "local-agent/.venv/bin/python"
    if not agent_py.is_file():
        _refuse(f"local-agent runtime missing: {agent_py}", "ailocal clients claude codex")
    for _ in range(60):                # healthy is not yet serving /v1/models
        try:
            S.http_json(f"{S.PROXY}/v1/models", timeout=5)
            return
        except S.Unreachable as exc:
            if "401" in str(exc):
                return
        time.sleep(1)
    _refuse(f"{S.CONTAINER} did not serve /v1/models within 60s.", "docker logs " + S.CONTAINER)


def _suites(full: bool) -> list:
    py = sys.executable
    suites = [
        ("SUITES", [
            ("policy: fail-closed profiles, hosted roots, output ceiling, secrets, bind", [py, "tests/policy.py"]),
            ("clients: generation, targets, co-owned config", [py, "tests/clients.py"]),
            ("install: model provenance, owned config, launchd, teardown", [py, "tests/install.py"]),
            ("audit and update reporting", [py, "tests/audit.py"]),
            ("patched Ollama runtime", [py, "tests/ollama-patch.py"]),
            ("gateway: tool repair and proxy auth", [py, "tests/gateway.py"]),
            ("gateway in the proxy image: hook import, reasoning effort, tool identity, loop guard",
             ["/bin/bash", "tests/in-container.sh", "tests/gateway-impl.py"]),
            ("model guidance and delegation", [py, "tests/guidance.py"]),
            ("scratchpad hook: private, unescapable", [py, "tests/scratchpad.py"]),
            ("research MCP: network boundary, Brave key and budget, citations", [py, "tests/research.py"]),
            ("bundled artifacts", [py, "tests/artifacts.py"] + (["--full"] if full else [])),
            ("checks, commands and documentation", [py, "tests/checks.py"]),
            ("measurement drivers (fixtures, no model)", [py, "tests/measurement-contracts.py"]),
        ]),
        ("INVARIANTS", [
            ("generation is a fixed point", _fixed_point),
            ("litellm runtime matches the validated version", _version_current),
            ("shell parses (bash -n, zsh -n) and passes ShellCheck", _shell_clean),
            ("python parses at 3.11 and meets standard §8a (ruff)", _ruff_clean),
            ("the repository root holds only project concepts", _root_is_clean),
        ]),
    ]
    if full:
        suites[0][1].insert(0, ("client compatibility (3 dialects x 3 modes)",
                                ["/bin/bash", "tests/client-compatibility.sh"]))
    return suites


def _fixed_point(repo: pathlib.Path) -> tuple[int, str]:
    """Check the generated cfg is deployed and a second run changes nothing."""
    from ailocal import policy as P
    generated = P.state_root() / "litellm" / "config.yaml"
    before = generated.read_bytes() if generated.is_file() else b""
    r = subprocess.run([sys.executable, "-m", "ailocal.generation"],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode:
        return 1, r.stdout + r.stderr
    return (0, "") if generated.read_bytes() == before else (1, "generation is not a fixed point")


def _version_current(repo: pathlib.Path) -> tuple[int, str]:
    r = S.check_litellm_version()
    return (0 if r.status is not FAIL else 1), r.summary


def _shell_clean(repo: pathlib.Path) -> tuple[int, str]:
    """Run bash -n/zsh -n on all scripts; ShellCheck bash only (no zsh)."""
    if not shutil.which("shellcheck"):
        return 1, "shellcheck is required: brew install shellcheck"
    bad = []
    shells = {"bash": ["tests/**/*.sh", f"{RES}/clients/*.sh", f"{RES}/deploy/**/*.sh"],
              "zsh": [f"{RES}/clients/*.zsh"]}
    for shell, patterns in shells.items():
        for pattern in patterns:
            for f in sorted(repo.glob(pattern)):
                r = subprocess.run([shell, "-n", str(f)], capture_output=True, text=True)
                if r.returncode:
                    bad.append(f"{f.name}: {r.stderr.strip()}")
                if shell == "bash":
                    r = subprocess.run(["shellcheck", "-x", "--severity=warning", "-f", "gcc", str(f)],
                                       capture_output=True, text=True, cwd=repo)
                    if r.stdout.strip():
                        bad.append(r.stdout.strip())
    return (1, "\n".join(bad)) if bad else (0, "")


def _ruff_clean(repo: pathlib.Path) -> tuple[int, str]:
    """Parse at the requires-python floor; enforce standard §8a docstrings."""
    if not shutil.which("ruff"):
        return 1, "ruff is required: pipx install ruff"
    r = subprocess.run(["ruff", "check", "--output-format", "concise", "src", "tests"],
                       capture_output=True, text=True, cwd=repo)
    return r.returncode, r.stdout.strip()


def _root_is_clean(repo: pathlib.Path) -> tuple[int, str]:
    """Check nothing generated is indexed and suites left no untracked file."""
    tracked = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True)
    if tracked.returncode:
        return 1, "could not list the tracked root"
    extra = sorted({p.split("/", 1)[0] for p in tracked.stdout.split()} - ROOT_ALLOWED)
    if extra:
        return 1, "tracked at the repository root: " + ", ".join(extra)
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=repo, capture_output=True, text=True)
    created = sorted(l[3:] for l in status.stdout.splitlines() if l.startswith("?? "))
    return (1, "untracked files in the checkout: " + ", ".join(created[:10])) if created else (0, "")


def _gate(argv: list[str]) -> int:
    full = "--full" in argv
    repo = _repo()
    _preconditions(repo)
    print("═" * 70 + "\n ailocal regression gate\n" + "═" * 70)
    passed, failures, slow = 0, [], []
    for heading, entries in _suites(full):
        print(f"\n{heading}")
        for label, runner in entries:
            started = time.monotonic()
            if callable(runner):
                try:
                    rc, out = runner(repo)
                except Exception as exc:  # noqa: BLE001
                    rc, out = 1, f"{type(exc).__name__}: {exc}"
            else:
                r = subprocess.run(runner, cwd=repo, capture_output=True, text=True)
                rc, out = r.returncode, r.stdout + r.stderr
            seconds = int(time.monotonic() - started)
            mark = (f" \033[33m[{seconds}s]\033[0m" if seconds >= SLOW_S
                    else f" ({seconds}s)" if seconds >= 2 else "")
            if seconds >= SLOW_S:
                slow.append(f"{label} ({seconds}s)")
            if rc == 0:
                print(f"  \033[32mPASS\033[0m  {label}{mark}")
                passed += 1
                continue
            print(f"  \033[31mFAIL\033[0m  {label}{mark}")
            for line in [l for l in out.splitlines()
                         if re.search(r"FAIL|[Ee]rror|Traceback|not idempotent", l)][:8]:
                print(f"          {line}")
            failures.append(label)

    print("\n" + "═" * 70)
    if failures:
        print(f" REGRESSION GATE: {len(failures)} FAILED, {passed} passed")
        for label in failures:
            print(f"   - {label}")
        return 1
    print(f" REGRESSION GATE: all {passed} checks passed")
    for label in slow:
        print(f"   slow (>= {SLOW_S}s): {label}")
    if not full:
        print(" (add --full for the client compatibility matrix)")
    return 0


if __name__ == "__main__":
    sys.exit(_gate(sys.argv[1:]))
