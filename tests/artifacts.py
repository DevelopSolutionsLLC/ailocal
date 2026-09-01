#!/usr/bin/env python3
"""The bundled local artifact capability, run through its own runtime.

The component is vendored under `resources/integrations/local-artifacts` and
carries the tests it was developed with. This driver runs them the way the
capability actually runs: against the CHECKOUT's copy of the component, using
the interpreter `ailocal clients claude` provisions in the state root -- the
same one registered in `.claude.json`. ailocal itself stays standard-library
only, so there is no other interpreter that can import `mcp`.

An absent runtime is a FAILURE, not a skip: it means provisioning did not run,
which is precisely the regression this suite exists to catch. `test_browser.py`
is the exception -- it drives a real Chrome and belongs to `--full`.
"""
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src/ailocal/resources/integrations/local-artifacts"

#: Deterministic, hermetic (each writes only into its own temp dirs).
#:
#: test_mermaid_validate.py is the validator's CONTROL FLOW -- the four states
#: and the publish gate that consumes them -- with the browser replaced through
#: the runner seam, plus ONE bounded real parse so a green CORE cannot hide a
#: dead authoritative path. The grammar corpus itself is FULL: it launches a
#: browser per fixture. That split keeps the invariant CI must never lose
#: ("invalid or unchecked Mermaid does not publish successfully") in the normal
#: gate without making CORE browser-heavy.
CORE = ["test_architecture.py", "test_autoopen.py", "test_design.py",
        "test_lifetime.py", "test_persistence.py", "test_server.py",
        "test_routing_contract.py", "test_mermaid_validate.py"]
#: Needs a real browser and free loopback ports.
FULL = ["test_browser.py", "test_mermaid_grammar.py"]


def _runtime() -> pathlib.Path:
    """The interpreter provisioned for the component, or a clear failure."""
    state = pathlib.Path(os.environ.get("XDG_STATE_HOME",
                                        pathlib.Path.home() / ".local/state"))
    return state / "ailocal/local-artifacts/.venv/bin/python"


def main() -> int:
    full = "--full" in sys.argv
    py = _runtime()
    if not py.is_file():
        print(f"  FAIL  artifact runtime missing ({py})")
        print("        run: ailocal clients claude")
        return 1
    if not (SRC / "server.py").is_file():
        print(f"  FAIL  bundled component missing from the checkout ({SRC})")
        return 1

    failed = []
    for name in CORE + (FULL if full else []):
        path = SRC / name
        if not path.is_file():
            print(f"  FAIL  {name} is not in the bundled component")
            failed.append(name)
            continue
        # An automated run never opens a browser. The tab a finished session
        # leaves behind points at a port nothing is listening on any more, and a
        # gate that produces a screenful of "connection refused" is a gate people
        # stop running.
        # A publish now starts a preview server that deliberately OUTLIVES the
        # process that published, so an automated run would leave one behind for
        # the full idle timeout. Tests get a short one instead: whatever they
        # spawn reaps itself seconds after the suite ends.
        env = dict(os.environ, LOCAL_ARTIFACTS_AUTO_OPEN="0",
                   LOCAL_ARTIFACTS_IDLE_EXIT="5")
        r = subprocess.run([str(py), str(path)], cwd=str(SRC), env=env,
                           capture_output=True, text=True, timeout=900)
        ok = r.returncode == 0
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failed.append(name)
            tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
            for line in tail:
                print(f"        {line}")
    if not full:
        print("  NOTE  " + ", ".join(FULL) + " (real Chrome) run "
              "under `ailocal-gate --full`")

    print(f"\n  {len(CORE) + (len(FULL) if full else 0) - len(failed)} "
          f"passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
