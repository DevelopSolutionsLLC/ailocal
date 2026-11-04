#!/usr/bin/env python3
"""Run the vendored local-artifacts tests with the runtime `ailocal clients claude`
provisions. A missing runtime fails: provisioning is what this guards.
`--full` adds the suites that drive a real Chrome."""
import os
import pathlib
import subprocess
import sys

import harness  # noqa: F401  (puts the checkout's src/ on sys.path)
from ailocal import policy

SRC = pathlib.Path(__file__).resolve().parent.parent / "src/ailocal/resources/integrations/local-artifacts"
CORE = ["test_architecture.py", "test_autoopen.py", "test_design.py", "test_lifetime.py",
        "test_persistence.py", "test_server.py", "test_routing_contract.py",
        "test_mermaid_validate.py"]
FULL = ["test_browser.py", "test_mermaid_grammar.py"]


def main() -> int:
    py = policy.state_root() / "local-artifacts/.venv/bin/python"
    if not py.is_file():
        print(f"  FAIL  artifact runtime missing ({py}); run: ailocal clients claude")
        return 1
    # No browser tab, and preview servers reap themselves seconds after the run.
    env = dict(os.environ, LOCAL_ARTIFACTS_AUTO_OPEN="0", LOCAL_ARTIFACTS_IDLE_EXIT="5")
    failed = []
    for name in CORE + (FULL if "--full" in sys.argv else []):
        r = subprocess.run([str(py), str(SRC / name)], cwd=SRC, env=env,
                           capture_output=True, text=True, timeout=900)
        print(f"  {'PASS' if r.returncode == 0 else 'FAIL'}  {name}")
        if r.returncode:
            failed.append(name)
            print("        " + (r.stdout + r.stderr).strip().replace("\n", "\n        "))
    print(f"\n  {len(failed)} failed" if failed else "\n  all passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
