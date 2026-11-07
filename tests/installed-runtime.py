#!/usr/bin/env python3
"""Prove the installed wheel needs no checkout: install from a copy, delete the copy, run.

    python3 tests/installed-runtime.py            # path proof only, no Docker
    python3 tests/installed-runtime.py --stack    # also start/status/stop an isolated stack

Out of the gate. Keeps the real HOME; every ailocal and client root, the Compose
project, container names and ports are overridden into a temporary box.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, RESOURCES, Suite, run  # noqa: E402
from ailocal.policy import load_profile  # noqa: E402

PROJECT = "ailocal-it"
ROOTS = {"AILOCAL_CONFIG": "config", "AILOCAL_CLIENTS": "clients", "AILOCAL_STATE": "state",
         "AILOCAL_RUNTIMES": "runtimes", "CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
         "COPILOT_HOME": "copilot", "ZDOTDIR": "zdotdir", "AILOCAL_VSCODE_USER": "vscode-user"}
ISOLATED = ("AILOCAL_", "XDG_", "COMPOSE_", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "ZDOTDIR",
            "PYTHONPATH")  # PYTHONPATH from harness would import the checkout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stack", action="store_true", help="also start/status/stop an isolated stack")
    want_stack = ap.parse_args().stack
    s = Suite()
    box = Path(tempfile.mkdtemp(prefix="ailocal-it-"))
    try:
        return _prove(s, box, want_stack)
    finally:
        shutil.rmtree(box, ignore_errors=True)


def _prove(s: Suite, box: Path, want_stack: bool) -> int:
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
    r = run([str(venv / "bin/pip"), "install", "-q", str(source)], timeout=300)
    s.check(r.returncode == 0 and ailocal.is_file(), "the package installs with its console script", r.stderr[-400:])

    env = {k: v for k, v in os.environ.items() if not k.startswith(ISOLATED)}
    env.update({name: str(path) for name, path in roots.items()})
    # AILOCAL_DATA stays unset: the data root must be the installed package itself.
    env.update(PATH=f"{venv / 'bin'}:{env.get('PATH', '')}", COMPOSE_PROJECT_NAME=PROJECT,
               AILOCAL_LITELLM_CONTAINER=f"{PROJECT}-litellm", AILOCAL_SEARXNG_CONTAINER=f"{PROJECT}-searxng",
               AILOCAL_LITELLM_PORT="4111", AILOCAL_SEARXNG_PORT="8111")

    def py(code, label=None):
        r = run([python, "-c", code], timeout=300, env=env)
        if label:
            s.check(r.returncode == 0 and "ok" in r.stdout, label, r.stderr[-400:])
        return r

    r = py("from ailocal import install, policy; install.provision(install.distribution_source(), "
           "policy.config_root(), policy.state_root()); print(policy.data_root()); print('ok')")
    data = Path(r.stdout.splitlines()[0]) if r.returncode == 0 else box / "missing"
    s.check(r.returncode == 0 and str(venv) in str(data), "assets provision from the installed package, not a checkout",
            r.stderr[-400:] or str(data))
    missing = [str(p) for p in (cfg / "profiles/64gb.toml", data / "deploy/litellm/hooks/tool_gateway.py",
                                data / "integrations/local-research/server.py") if not p.is_file()]
    s.check(not missing, "the wheel ships profiles, proxy hooks and the research MCP", ", ".join(missing))
    # `install` would migrate and generate these; this suite never runs it. BRAVE_API is
    # a placeholder: no search is issued, so no quota is spent.
    (state / "active-profile").write_text("64gb\n")
    for path, body in ((state / "env", "LITELLM_MASTER_KEY=sk-integration-test-only\nSEARXNG_SECRET=integration-test-only\n"),
                       (cfg / ".env.local", "BRAVE_API=integration-test-placeholder\n"
                                            "OLLAMA_URL=http://host.docker.internal:11434\n")):
        path.write_text(body)
        path.chmod(0o600)

    shutil.rmtree(source)
    py("import hashlib; from ailocal import policy; s = policy.ollama_patch_spec(); "
       "assert all(len(s[k]) == 64 for k in ('native_archive_sha256', 'native_cli_sha256')); "
       "assert '/v' + s['base_version'] + '/' in s['native_archive_url']; "
       "assert all(hashlib.sha256(s[k].read_bytes()).hexdigest() == s[k.replace('_file', '_sha256')] for k in "
       "('patch_file', 'security_patch_file', 'router_patch_file', 'stop_patch_file', 'qwen35_patch_file')); "
       "print('ok')", "native runtime patches and their pins ship without the checkout")
    py("from ailocal import mcp; e = mcp.launch_environment(); "
       "assert (e['LOCAL_RESEARCH_MODEL'], e['LOCAL_RESEARCH_ENABLE_ANSWER'], e['LOCAL_RESEARCH_BRAVE_BUDGET'], "
       "e['LOCAL_RESEARCH_BRAVE_KEY']) == ('ailocal-fast', '0', '1000', 'integration-test-placeholder'); "
       "print('ok')", "the research launcher resolves installed policy")

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
    r = cli("profile", "guide", "ailocal-cyber", "--tier", "16gb", "--json")
    try:
        guide = json.loads(r.stdout)["models"]["ailocal-cyber"]
    except (ValueError, KeyError):
        guide = {"model": None, "evidence": ""}
    shipped = load_profile("16gb", RESOURCES)["cyber"]["active"]
    s.check(guide["model"] == shipped and "[REAL]" in guide["evidence"],
            "profile model knowledge ships and resolves to the shipped selection",
            f"installed={guide['model']} shipped={shipped}")

    if want_stack:
        s.section("THE STACK COMES UP FROM MANAGED ASSETS")
        codex = roots["AILOCAL_CLIENTS"] / "codex"
        codex.mkdir(exist_ok=True)
        authored = codex / "review.config.toml"
        authored.write_text('model = "operator-choice"\n')
        cli("start", must_pass=False, timeout=600)
        s.check(authored.read_text() == 'model = "operator-choice"\n', "start preserves an authored native profile")
        container = f"{PROJECT}-litellm"
        healthy, detail = False, ""
        for _ in range(40):
            if run(["docker", "inspect", container, "--format", "{{.State.Health.Status}}"]).stdout.strip() == "healthy":
                healthy = True
                break
            time.sleep(3)
        if not healthy:  # capture the reason before stop removes the container
            logs = run(["docker", "logs", "--tail", "25", container])
            detail = (run(["docker", "inspect", container, "--format",
                           "{{.State.Status}} {{range .State.Health.Log}}{{.Output}}{{end}}"]).stdout
                      + logs.stdout + logs.stderr)[-1200:]
        s.check(healthy, "the isolated proxy becomes healthy", detail)
        cli("status", must_pass=False)
        cli("stop", must_pass=False)
        s.check(container not in run(["docker", "ps", "-a", "--format", "{{.Names}}"]).stdout,
                "the isolated stack is removed")
    s.check(not broken, "every command run here works without the checkout or a missing-file error",
            repr(broken)[:600])
    return s.report()


if __name__ == "__main__":
    sys.exit(main())
