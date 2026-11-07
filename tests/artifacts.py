#!/usr/bin/env python3
"""Run the vendored local-artifacts tests with the runtime `ailocal clients claude`
provisions. A missing runtime fails: provisioning is what this guards.
`--full` adds the suites that drive a real Chrome."""
import os
import pathlib
import subprocess
import sys

from harness import Suite
from ailocal import policy

SRC = pathlib.Path(__file__).resolve().parent.parent / "src/ailocal/resources/integrations/local-artifacts"
CORE = ["test_architecture.py", "test_autoopen.py", "test_design.py", "test_lifetime.py",
        "test_persistence.py", "test_server.py", "test_routing_contract.py",
        "test_mermaid_validate.py"]
FULL = ["test_browser.py", "test_mermaid_grammar.py"]


def main() -> int:
    s = Suite()
    py = policy.state_root() / "local-artifacts/.venv/bin/python"
    if not py.is_file():  # a precondition, not a test: --full already reports ten
        s.check(False, "the artifact runtime is provisioned", f"{py} missing; run: ailocal clients claude")
        return s.report()
    # No browser tab, and preview servers reap themselves seconds after the run.
    env = dict(os.environ, LOCAL_ARTIFACTS_AUTO_OPEN="0", LOCAL_ARTIFACTS_IDLE_EXIT="5")
    for name in CORE + (FULL if "--full" in sys.argv else []):
        r = subprocess.run([str(py), str(SRC / name)], cwd=SRC, env=env,
                           capture_output=True, text=True, timeout=900)
        s.check(r.returncode == 0, name, (r.stdout + r.stderr).strip().replace("\n", "\n        "))
    return s.report()

if __name__ == "__main__":
    sys.exit(main())
