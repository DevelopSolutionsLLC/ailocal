#!/usr/bin/env python3
"""Run research contracts using the component's own provisioned SDK runtime."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from ailocal import policy

if __name__ == '__main__':
    py = policy.state_root() / 'local-research/.venv/bin/python'
    if not py.is_file():
        sys.exit('FAIL: research runtime missing; run: ailocal clients claude')
    sys.exit(subprocess.run([str(py), str(Path(__file__).with_name('research-tests.py'))], timeout=120).returncode)
