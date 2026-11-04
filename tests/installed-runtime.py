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
from harness import REPO, Suite, run  # noqa: E402

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
    s.check(r.returncode == 0, "the package installs", r.stderr[-400:])
    s.check(ailocal.is_file(), "the console script is installed")

    env = {k: v for k, v in os.environ.items() if not k.startswith(ISOLATED)}
    env.update({name: str(path) for name, path in roots.items()})
    # AILOCAL_DATA stays unset: the data root must be the installed package itself.
    env.update(PATH=f"{venv / 'bin'}:{env.get('PATH', '')}", COMPOSE_PROJECT_NAME=PROJECT,
               AILOCAL_LITELLM_CONTAINER=f"{PROJECT}-litellm", AILOCAL_SEARXNG_CONTAINER=f"{PROJECT}-searxng",
               AILOCAL_LITELLM_PORT="4111", AILOCAL_SEARXNG_PORT="8111")

    def py(code, label):
        r = run([python, "-c", code], timeout=300, env=env)
        s.check(r.returncode == 0 and "ok" in r.stdout, label, r.stderr[-400:])
        return r

    r = py("from ailocal import install, policy; install.provision(install.distribution_source(), "
           "policy.config_root(), policy.state_root()); print(policy.data_root()); print('ok')",
           "assets provision into the managed roots from the wheel")
    data = Path(r.stdout.splitlines()[0]) if r.returncode == 0 else box / "missing"
    s.check(str(venv) in str(data), "the data root IS the installed package, not a checkout")
    for rel, label in ((cfg / "profiles/64gb.toml", "profiles land in the config root"),
                       (data / "deploy/litellm/hooks/tool_gateway.py", "proxy hooks are read from the wheel"),
                       (data / "integrations/local-research/server.py", "research MCP source ships in the wheel")):
        s.check(rel.is_file(), label)
    # `install` would migrate and generate these; this suite never runs it. BRAVE_API is
    # a placeholder: no search is issued, so no quota is spent.
    (state / "active-profile").write_text("64gb\n")
    for path, body in ((state / "env", "LITELLM_MASTER_KEY=sk-integration-test-only\nSEARXNG_SECRET=integration-test-only\n"),
                       (cfg / ".env.local", "BRAVE_API=integration-test-placeholder\n"
                                            "OLLAMA_URL=http://host.docker.internal:11434\n")):
        path.write_text(body)
        path.chmod(0o600)

    shutil.rmtree(source)
    s.check(not source.exists(), "the source checkout is gone")
    py("from ailocal import policy, model_sources; assert all(policy.resolve_role(t, 'cyber')['model'] "
       "in policy.required_models(t) for t in policy.TIERS); print('ok')",
       "profiles and the model importer resolve without a checkout")
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

    def cli(*args, must_pass=True, timeout=300):
        r = run([str(ailocal), *args], timeout=timeout, env=env)
        blob, name = r.stdout + r.stderr, " ".join(args[:2])
        s.check(str(source) not in blob and "Traceback" not in blob and "No such file or directory" not in blob,
                f"`ailocal {name}` runs without the checkout or a missing-file error", blob[-400:])
        if must_pass:
            s.check(r.returncode == 0, f"`ailocal {name}` exits 0", blob[-400:])
        return r

    s.check("local model runtime" in cli("help").stdout, "help renders from the installed parser")
    s.check(cli("profile", "active-tier").stdout.strip() == "64gb", "the active tier reads back from managed state")
    r = cli("profile", "guide", "ailocal-cyber", "--tier", "16gb", "--json")
    try:
        guide = json.loads(r.stdout)["models"]["ailocal-cyber"]
    except (ValueError, KeyError):
        guide = {"model": None, "evidence": ""}
    s.check(guide["model"] == "qwen3.5:4b-unrestricted-mtp-q4_K_M"
            and all(t in guide["evidence"] for t in ("[REAL]", "[APPROX]",
                    "0fe4f588e328513e7b55158a699fa68bc55be226ca338714e633569d4d2e5007")),
            "profile model knowledge ships and resolves")
    cli("check", must_pass=False)

    if want_stack:
        s.section("THE STACK COMES UP FROM MANAGED ASSETS")
        codex = roots["AILOCAL_CLIENTS"] / "codex"
        codex.mkdir(exist_ok=True)
        for name in ("plan", "cyber"):
            (codex / f"{name}.config.toml").write_text('# Generated by ailocal. Do not edit.\nmodel = "old"\n')
        authored = codex / "review.config.toml"
        authored.write_text('model = "operator-choice"\n')
        cli("start", must_pass=False, timeout=600)
        s.check(not any((codex / f"{n}.config.toml").exists() for n in ("plan", "cyber")),
                "start retires generated launch presets")
        s.check(authored.read_text() == 'model = "operator-choice"\n', "start preserves an authored native profile")
        s.check((state / "litellm/config.yaml").is_file(), "start generates into the managed state root")
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
    return s.report()


if __name__ == "__main__":
    sys.exit(main())
