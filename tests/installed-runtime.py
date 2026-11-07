#!/usr/bin/env python3
"""Prove the installed wheel needs no checkout: install a copy, delete it, run.

    python3 tests/installed-runtime.py            # path proof only, no Docker

Out of the gate. Keeps the real HOME; every ailocal and client root, the Compose
project, container names and ports are overridden into a temporary box.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite, run  # noqa: E402

PROJECT = "ailocal-it"
ROOTS = {"AILOCAL_CONFIG": "config", "AILOCAL_CLIENTS": "clients", "AILOCAL_STATE": "state",
         "AILOCAL_RUNTIMES": "runtimes", "CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
         "COPILOT_HOME": "copilot", "ZDOTDIR": "zdotdir", "AILOCAL_VSCODE_USER": "vscode-user"}
ISOLATED = ("AILOCAL_", "XDG_", "COMPOSE_", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "ZDOTDIR",
            "PYTHONPATH")  # PYTHONPATH from harness would import the checkout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.parse_args()
    s = Suite()
    box = Path(tempfile.mkdtemp(prefix="ailocal-it-"))
    try:
        return _prove(s, box)
    finally:
        shutil.rmtree(box, ignore_errors=True)


def _prove(s: Suite, box: Path) -> int:
    source, venv = box / "source", box / "venv"
    roots = {name: box / sub for name, sub in ROOTS.items()}
    for path in roots.values():
        path.mkdir(parents=True)
    cfg, state = roots["AILOCAL_CONFIG"], roots["AILOCAL_STATE"]
    shutil.copytree(REPO, source, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".venv", "build", "dist", "*.egg-info"))

    s.section("BUILD AN INSTALLATION, THEN DESTROY ITS SOURCE")
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    python, ailocal = str(venv / "bin/python3"), venv / "bin/ailocal"
    pip = run([str(venv / "bin/pip"), "install", "-q", str(source)], timeout=300)

    env = {k: v for k, v in os.environ.items() if not k.startswith(ISOLATED)}
    env.update({name: str(path) for name, path in roots.items()})
    # AILOCAL_DATA unset: the data root must be the installed package itself.
    env.update(PATH=f"{venv / 'bin'}:{env.get('PATH', '')}", COMPOSE_PROJECT_NAME=PROJECT,
               AILOCAL_LITELLM_CONTAINER=f"{PROJECT}-litellm", AILOCAL_SEARXNG_CONTAINER=f"{PROJECT}-searxng",
               AILOCAL_LITELLM_PORT="4111", AILOCAL_SEARXNG_PORT="8111")

    def py(code):
        return run([python, "-c", code], timeout=300, env=env)

    r = py("from ailocal import install, policy; install.provision(install.distribution_source(), "
           "policy.config_root(), policy.state_root()); print(policy.data_root())")
    data = Path(r.stdout.splitlines()[0]) if r.returncode == 0 else box / "missing"
    s.check(pip.returncode == 0 and r.returncode == 0 and str(venv) in str(data),
            "assets provision from the installed package, not a checkout",
            (pip.stderr + r.stderr)[-400:] or str(data))
    r = py("from ailocal import policy; s = policy.ollama_patch_spec(); "
           "print('\\n'.join(str(v) for k, v in s.items() if k.endswith('_patch_file') or k == 'patch_file'))")
    shipped = [cfg / "profiles/64gb.toml", data / "deploy/litellm/hooks/tool_gateway.py",
               data / "integrations/local-research/server.py", *map(Path, r.stdout.splitlines())]
    missing = [str(p) for p in shipped if not p.is_file()] + ([r.stderr[-200:]] if r.returncode else [])
    s.check(len(shipped) > 3 and not missing,
            "the wheel ships profiles, proxy hooks, the research MCP and the native runtime patches",
            ", ".join(missing))
    # Never runs `install`; BRAVE_API is a placeholder and no search is sent.
    (state / "active-profile").write_text("64gb\n")
    for path, body in ((state / "env", "LITELLM_MASTER_KEY=sk-integration-test-only\nSEARXNG_SECRET=integration-test-only\n"),
                       (cfg / ".env.local", "BRAVE_API=integration-test-placeholder\n"
                                            "OLLAMA_URL=http://host.docker.internal:11434\n")):
        path.write_text(body)
        path.chmod(0o600)

    shutil.rmtree(source)

    s.section("ORDINARY COMMANDS RUN WITH NO CHECKOUT")

    broken = {}

    def cli(*args, must_pass=True, timeout=300):
        r = run([str(ailocal), *args], timeout=timeout, env=env)
        blob, name = r.stdout + r.stderr, " ".join(args[:2])
        if (str(source) in blob or "Traceback" in blob or "No such file or directory" in blob
                or (must_pass and r.returncode != 0)):
            broken[name] = blob[-300:]
        return r

    cli("help")
    cli("profile", "active-tier")
    cli("check", must_pass=False)
    cli("profile", "guide", "ailocal-cyber", "--tier", "16gb", "--json")

    s.check(not broken, "every command run here works without the checkout or a missing-file error",
            repr(broken)[:600])
    return s.report()


if __name__ == "__main__":
    sys.exit(main())
