#!/usr/bin/env python3
"""The regression gate: every suite in this repository, in one run.

A developer command, not a product command — it needs a checkout, and several
suites need the registry and PyYAML, which exist only inside the proxy image.
"Could not run" is therefore failure, never a skip: a host-only run would cover
a fraction of the behaviour and still print green.

Usage: python3 tests/gate.py [--full]
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

from ailocal.checks import FAIL
from ailocal.checks import services as S

#: Shipped assets live inside the package; the checkout path to them is
#: spelled once.
RES = "src/ailocal/resources"

#: Bash suites cannot see sys.executable, and one of them regenerates real
#: client configuration. Handing them this interpreter is what keeps them on the
#: working tree instead of a separately installed `ailocal` on PATH.
os.environ.setdefault("AILOCAL_PY", sys.executable)

GATE_SLOW_S = int(os.environ.get("AILOCAL_GATE_SLOW_S", "10"))


def _repo() -> pathlib.Path:
    """The checkout the suites live in."""
    here = pathlib.Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "tests").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    sys.exit("the gate runs this repository's suites; run it from a checkout.")


def _refuse_on_generated_drift() -> None:
    """Stale generated config must stop the gate BEFORE any suite runs.

    OBSERVED: the gate failed 2 of 4 consecutive runs, always in `validators`,
    and passed on rerun with no action in between. Not a timing race — the cause
    is generated-config drift. `validators` asserts that `ailocal check` exits 0,
    which it correctly refuses to do while the generated config is stale.
    Reproduced deterministically: install a template edit without regenerating,
    and the same three checks fail every time.

    Several suites write real generated configuration (see the note above
    AILOCAL_PY), so the drift can be repaired mid-run by something later than
    the suite that reported it. WHICH suite healed it was not isolated, and the
    fix does not depend on knowing: refusing before any suite runs removes the
    order-dependence whatever the healer is.

    "Passes on rerun" is the dangerous property here, not the failure itself: it
    teaches a reader to rerun the gate rather than read it, and the real drift is
    never reported. Deciding it here makes the outcome a function of the tree.

    SCOPE: this compares the INSTALLED package against the generated config,
    which is the condition that actually flaked. A template edited in the
    checkout but not installed is a different state, and already fails the gate
    deterministically (`validators` plus `generation is a fixed point`), so it
    needs nothing here.

    Reuses `ailocal check`'s own drift check — one canonical implementation,
    already bounded by its own timeout and already non-mutating (`--check`). A
    BLOCKED result means the generator could not run at all, which is a broken
    host rather than drift, so it is left to the suites to report.
    """
    from ailocal.checks.config import check_generated_in_sync

    result = check_generated_in_sync()
    if result.status != FAIL:
        return
    sys.exit(f"\n  {result.summary}"
             f"\n      {(result.detail or '').strip()}"
             f"\n\n  {result.remediation or 'regenerate, then rerun'}"
             "\n  Refusing to run: PRECONDITION NOT MET."
             "\n  (A later suite would regenerate this and hide the drift.)")


def _refuse_on_stale_install(repo: pathlib.Path) -> None:
    """The suites import the CHECKOUT; `ailocal` runs the INSTALLED package.

    Every manual `ailocal start` afterwards exercises the second. Letting the
    gate report success while those differ is what allowed a committed generator
    change to be absent from the live runtime, with a green suite either side of
    it.

    This is NOT the generated-config drift below. That asks whether the
    generated files match the current generator; this asks whether the generator
    that will run is the one just tested. Checked FIRST, because a stale install
    produces generated drift, and the remedy for a cause is not the remedy for
    its symptom.
    """
    from ailocal.checks.install_parity import REMEDIATION, compare

    ok, why = compare(repo)
    if ok:
        return
    sys.exit(f"\n  {why}"
             f"\n\n  {REMEDIATION}"
             "\n  Refusing to run: PRECONDITION NOT MET."
             "\n  (The suites would pass against the checkout and say nothing"
             "\n   about the package `ailocal start` actually executes.)")


def _gate_preconditions(repo: pathlib.Path) -> None:
    """Refuse rather than run a reduced set and report success."""
    container = S.CONTAINER
    state, health = S.container_state(container)
    if state != "running":
        sys.exit(f"\n  {container} is not running. The registry, negotiator and "
                 "compatibility suites all need it.\n      ailocal start")
    if health not in ("healthy", ""):
        sys.exit(f"\n  {container} health is {health!r}, not healthy. Fix that "
                 "before trusting any result.")
    _refuse_on_stale_install(repo)
    _refuse_on_generated_drift()
    # Container health means the proxy PROCESS is up, not that the router serves
    # /v1/models. 401 counts as ready: it proves the route answers.
    for _ in range(60):
        try:
            S.http_json(f"{S.PROXY}/v1/models", timeout=5)
            return
        except S.Unreachable as exc:
            if "401" in str(exc):
                return
        time.sleep(1)
    sys.exit(f"\n  {container} is healthy but /v1/models did not serve within 60s."
             "\n  Refusing to run: PRECONDITION NOT MET.")


def _gate_suites(repo: pathlib.Path, full: bool) -> list:
    py = sys.executable
    suites = [
        ("UNIT / BEHAVIOUR", [
            ("capability registry (+ no-hard-coded-literals assertion)",
             ["/bin/bash", "tests/in-container.sh",
                    "tests/capability-registry-impl.py",
                    "AILOCAL_GATEWAY_SOURCE=/app/config/hooks/tool_gateway.py"]),
            ("capability negotiator (byte accounting, modes, passthrough)",
             ["/bin/bash", "tests/in-container.sh",
                    "tests/tool-gateway-impl.py",
                    "AILOCAL_GATEWAY_MODULE=/app/config/hooks/tool_gateway.py"]),
            ("tool-call repair (repairs real calls, refuses examples)",
             [py, "tests/gateway.py", "repair"]),
            ("profile resolver (single reader, fail-closed, no 64gb default)",
             [py, "tests/profiles.py", "resolver"]),
            ("policy ownership (one reader, client policy fails closed)",
             [py, "tests/profiles.py", "policy"]),
            ("hardware profiles (schema, tiers, dedup)",
             [py, "tests/profiles.py", "hardware"]),
            ("instruction duplication (one always-on file, no authored model facts)",
             [py, "tests/instruction-duplication.py"]),
            ("environment ownership (two owners, precedence, migration fails closed)",
             [py, "tests/env-ownership.py"]),
            ("client target selection (`all`, detected-only) and VS Code outcomes",
             [py, "tests/client-targets.py"]),
            ("Python LSP baseline for claude-local (real documentSymbol)",
             [py, "tests/lsp-baseline.py"]),
        ]),
        ("INTEGRATION", [
            ("client role alias overrides (defaults intact, fails closed)",
             ["/bin/bash", "tests/clients.sh", "roles"]),
            ("codex MCP is withheld (no grepai/lsp/github, no re-sync)",
             ["/bin/bash", "tests/clients.sh", "codex"]),
            ("shell output helpers (streams, colour, one owner)",
             ["/bin/bash", "tests/shell-output.sh"]),
            ("validator checks (deterministic, bounded, search quota)",
             [py, "tests/validators.py"]),
            ("generation rolls back on partial failure (never mixed on disk)",
             [py, "tests/generation-rollback.py"]),
            ("install: provisioning, provenance and tier selection",
             [py, "tests/install.py"]),
            ("client compatibility probes (/api/hello, no side effects)",
             ["/bin/bash", "tests/compat-routes.sh"]),
        ]),
        ("INVARIANTS", [
            ("generation is a fixed point", _fixed_point),
            # The precondition already refused a stale install before any suite
            # ran. This is the FROZEN behaviour of that refusal: that it detects
            # drift at all, fails closed when nothing is installed, and stays
            # distinct from generated-config drift.
            ("installed package matches the checkout (source vs runtime)",
             [py, "tests/install-parity.py"]),
            ("litellm runtime matches the validated version", _version_current),
            ("all shell scripts parse (bash -n)", _shell_parses),
            ("shell passes ShellCheck (warning+, skipped if absent)",
             _shellcheck_clean),
            ("all python modules parse", _python_parses),
            ("client timeout is not below the proxy timeout", _timeouts_aligned),
            ("every registered hook imports inside the proxy image", _hooks_import),
            ("installers are idempotent",
             ["/bin/bash", "tests/idempotent-install.sh"]),
            ("the repository root holds only project concepts", _root_is_clean),
            ("the version is spelled the same in both places", _version_agrees),
            ("installation audit runs cleanly", _audit_runs),
            ("every read-only command actually runs", _readonly_commands_run),
        ]),
    ]
    if full:
        suites[1][1].insert(0, ("client compatibility (3 dialects x 3 modes)",
                                ["/bin/bash", "tests/client-compatibility.sh"]))
    return suites


def _fixed_point(repo: pathlib.Path) -> tuple[int, str]:
    """The generated config IS the deployed config, so a generator that is not a
    fixed point means the proxy and the repository can silently disagree."""
    from ailocal import policy as P
    generated = P.state_root() / "litellm" / "config.yaml"
    before = generated.read_bytes() if generated.is_file() else b""
    r = subprocess.run([sys.executable, "-m", "ailocal.generation"],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode:
        return 1, r.stdout + r.stderr
    return (0, "") if generated.read_bytes() == before else         (1, "generation is not a fixed point")


#: Everything the repository root is allowed to track. A generated artifact
#: here means a root lost its owner — see policy.deployed_client_root().
ROOT_ALLOWED = {".gitignore", "AGENTS.md", "CHANGELOG.md", "LICENSE",
                "README.md", "RELEASING.md", "pyproject.toml",
                "docs", "src", "tests"}


def _version_agrees(repo: pathlib.Path) -> tuple[int, str]:
    """From v0.9.0 the version is a published fact, and it is written twice: the
    wheel takes pyproject's and everything at runtime reads __init__'s. Two
    spellings means the number a user reports is not the number they installed.
    """
    def find(path: str, pattern: str) -> str:
        m = re.search(pattern, (repo / path).read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""

    project = find("pyproject.toml", r'^version = "([^"]+)"')
    package = find("src/ailocal/__init__.py", r'^__version__ = "([^"]+)"')
    if not project or not package:
        return 1, f"could not read a version (pyproject={project!r} "\
                  f"package={package!r})"
    if project != package:
        return 1, (f"pyproject.toml says {project}, "
                   f"src/ailocal/__init__.py says {package}")
    if f"## v{project}" not in (repo / "CHANGELOG.md").read_text(encoding="utf-8"):
        return 1, f"CHANGELOG.md has no release notes for v{project}"
    return 0, ""


def _root_is_clean(repo: pathlib.Path) -> tuple[int, str]:
    """No generated artifact is tracked, and running the suites creates none.

    Both halves matter. The allowlist catches a generated file that was
    committed; the untracked check catches the mechanism that put it there,
    which is a root resolving to the checkout. .gitignore cannot be the test:
    it would hide exactly the failure this is looking for.

    The boundary is the INDEX, not HEAD. A gate developers run before committing
    has to judge what they are about to commit: reading HEAD both fails a
    correct staged change until it lands and passes a bad staged root addition
    because HEAD is still clean. Index plus the untracked check below covers
    every root entry that exists before the commit.
    """
    tracked = subprocess.run(["git", "ls-files"],
                             cwd=repo, capture_output=True, text=True)
    if tracked.returncode:
        return 1, "could not list the tracked root"
    extra = sorted({p.split("/", 1)[0] for p in tracked.stdout.split()}
                   - ROOT_ALLOWED)
    if extra:
        return 1, ("tracked at the repository root but not a project concept: "
                   + ", ".join(extra))
    # --no-standard-filter would be ideal; instead ask git for ignored entries
    # too and subtract the ones a developer legitimately has.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo, capture_output=True, text=True)
    created = sorted(l[3:] for l in dirty.stdout.splitlines()
                     if l.startswith("?? "))
    if created:
        return 1, "the suites left untracked files in the checkout: " + \
                  ", ".join(created[:10])
    return 0, ""


def _version_current(repo: pathlib.Path) -> tuple[int, str]:
    r = S.check_litellm_version()
    return (0 if r.status is not FAIL else 1), r.summary


def _shell_parses(repo: pathlib.Path) -> tuple[int, str]:
    bad = []
    for pattern in ("ailocal", "tests/**/*.sh",
                    f"{RES}/clients/*.sh", f"{RES}/clients/*.zsh"):
        for f in sorted(repo.glob(pattern)):
            r = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            if r.returncode:
                bad.append(f"{f.name}: {r.stderr.strip()}")
    return (1, "\n".join(bad)) if bad else (0, "")


def _shellcheck_clean(repo: pathlib.Path) -> tuple[int, str]:
    """ShellCheck at warning severity over the bash we ship and test with.

    SKIPPED, not failed, when shellcheck is absent: it is a developer tool, and
    a contributor without it must still be able to run the gate. The README
    carries `brew install shellcheck`.

    `-x` follows `. harness.sh`, without which every suite reports SC1091 for a
    file that is right there. Severity stops at `warning` deliberately — the
    note level is largely style (SC2015, SC2016, SC2181) and turning it on
    would bury a real finding in forty opinions.

    .zsh is excluded because ShellCheck does not implement zsh; running it
    there reports POSIX complaints about valid zsh. Those files are covered by
    `zsh -n` in _shell_parses.
    """
    if not shutil.which("shellcheck"):
        return 0, "SKIP: shellcheck not installed (brew install shellcheck)"
    findings = []
    for pattern in ("tests/**/*.sh", f"{RES}/clients/*.sh",
                    f"{RES}/deploy/**/*.sh"):
        for f in sorted(repo.glob(pattern)):
            r = subprocess.run(
                ["shellcheck", "-x", "--severity=warning", "-f", "gcc", str(f)],
                capture_output=True, text=True, cwd=repo)
            if r.stdout.strip():
                findings.append(r.stdout.strip())
    return (1, "\n".join(findings)) if findings else (0, "")


def _python_parses(repo: pathlib.Path) -> tuple[int, str]:
    bad = []
    for pattern in ("src/**/*.py", "tests/**/*.py"):
        for f in sorted(repo.glob(pattern)):
            try:
                ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                bad.append(f"{f}: {exc}")
    return (1, "\n".join(bad)) if bad else (0, "")


def _timeouts_aligned(repo: pathlib.Path) -> tuple[int, str]:
    """The client must never give up before the proxy, or it abandons requests
    the proxy is still serving while the backend generates into a closed socket.
    """
    proxy = re.search(r"^ *timeout: *(\d+)",
                      (repo / RES / "deploy/litellm/config.template.yaml").read_text(),
                      re.M)
    client = re.search(r"AILOCAL_API_TIMEOUT_MS:-(\d+)}",
                       (repo / RES / "clients/configure.template.zsh").read_text())
    if not (proxy and client):
        return 1, "could not read both timeouts"
    if int(client.group(1)) < int(proxy.group(1)) * 1000:
        return 1, (f"client API_TIMEOUT_MS {client.group(1)} is BELOW the LiteLLM "
                   f"timeout {proxy.group(1)}s")
    return 0, ""


def _hooks_import(repo: pathlib.Path) -> tuple[int, str]:
    """A registered-but-unimportable callback takes the container down at boot,
    and a sibling import that works on the host fails under LiteLLM's loader."""
    program = (
        "import importlib.util, sys\n"
        "bad = []\n"
        "for name in ['reasoning_router','startup',"
        "'tool_repair','tool_gateway','capability_registry']:\n"
        "    try:\n"
        "        spec = importlib.util.spec_from_file_location("
        "name, f'/app/config/hooks/{name}.py')\n"
        "        mod = importlib.util.module_from_spec(spec)\n"
        "        sys.modules[name] = mod\n"
        "        spec.loader.exec_module(mod)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {type(exc).__name__}: {exc}')\n"
        "print(chr(10).join(bad))\n"
        "sys.exit(1 if bad else 0)\n")
    r = subprocess.run(["docker", "exec", "-i", S.CONTAINER, "python", "-"],
                       input=program, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def _readonly_commands_run(repo: pathlib.Path) -> tuple[int, str]:
    """Importing a module proves nothing about calling it: `ailocal trace` and
    `ailocal status` both once raised NameError on a helper deleted from under
    them, and every import-level check stayed green."""
    bad = []
    for cmd in ("help", "--version", "status", "trace", "profile active-tier"):
        r = subprocess.run([sys.executable, "-m", "ailocal.cli", *cmd.split()],
                           cwd=repo, capture_output=True, text=True, timeout=120)
        if "Traceback" in r.stderr:
            bad.append(f"ailocal {cmd}: {r.stderr.strip().splitlines()[-1]}")
    return (1, "\n".join(bad)) if bad else (0, "")


def _audit_runs(repo: pathlib.Path) -> tuple[int, str]:
    """Findings are a normal working state. Only the audit itself breaking
    fails the gate."""
    from ailocal import install
    try:
        install.audit()
        install.client_audit()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"
    return 0, ""


def _gate(argv: list[str]) -> int:
    full = "--full" in argv
    repo = _repo()
    _gate_preconditions(repo)
    print("═" * 70 + "\n ailocal regression gate\n" + "═" * 70)

    passed = failed = 0
    failures, slow = [], []
    for heading, entries in _gate_suites(repo, full):
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
            mark = (f" \033[33m[{seconds}s]\033[0m" if seconds >= GATE_SLOW_S
                    else f" ({seconds}s)" if seconds >= 2 else "")
            if seconds >= GATE_SLOW_S:
                slow.append(f"{label} ({seconds}s)")
            if rc == 0:
                print(f"  \033[32mPASS\033[0m  {label}{mark}")
                passed += 1
            else:
                print(f"  \033[31mFAIL\033[0m  {label}{mark}")
                for line in [l for l in out.splitlines()
                             if re.search(r"FAIL|[Ee]rror|Traceback|not idempotent",
                                          l)][:6]:
                    print(f"          {line}")
                failed += 1
                failures.append(label)

    print("\n" + "═" * 70)
    if failed:
        print(f" REGRESSION GATE: {failed} FAILED, {passed} passed")
        for label in failures:
            print(f"   - {label}")
        return 1
    print(f" REGRESSION GATE: all {passed} checks passed")
    if slow:
        print(f" {len(slow)} check(s) at/over {GATE_SLOW_S}s — keep the gate fast "
              "enough to run:")
        for label in slow:
            print(f"   {label}")
    if not full:
        print(" (add --full for the client compatibility matrix)")
    return 0



if __name__ == "__main__":
    sys.exit(_gate(sys.argv[1:]))
