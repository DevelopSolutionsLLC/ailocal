#!/usr/bin/env python3
"""installed-runtime.py — the decisive proof that ailocal needs no checkout.

Passing the gate from inside a checkout cannot show this: the checkout is what
supplies the assets. So this builds a real installation somewhere else and then
DESTROYS the source it was built from, leaving a path that does not exist. Any
command that still reaches for the checkout fails with ENOENT rather than
quietly succeeding.

Isolation is total -- its own HOME, XDG roots, Compose project, container names
and host ports -- so it never contends with a production stack. Images are the
pinned production digests; nothing is pulled or upgraded.

Opt-in: it installs a package and starts containers.
    python3 tests/installed-runtime.py            # no Docker: path proof only
    python3 tests/installed-runtime.py --stack    # also start/status/stop
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Suite  # noqa: E402

_suite = Suite()
check = _suite.check

PORT_LITELLM = "4111"
PORT_SEARXNG = "8111"
PROJECT = "ailocal-it"


def run(argv, env, timeout=300):
    return subprocess.run(argv, env=env, capture_output=True, text=True,
                          timeout=timeout)


def main() -> None:
    want_stack = "--stack" in sys.argv
    box = Path(tempfile.mkdtemp(prefix="ailocal-it-"))
    source = box / "source"          # a throwaway copy, deleted before the proof
    home = box / "home"
    cfg, data, state = box / "config", box / "data", box / "state"
    for p in (home, cfg, data, state):
        p.mkdir(parents=True)

    # Copy the checkout, so the real one is never at risk when we delete.
    shutil.copytree(REPO, source, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".venv", "build", "dist", "*.egg-info"))

    _suite.section("BUILD AN INSTALLATION, THEN DESTROY ITS SOURCE")
    venv = box / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    pip, ailocal = venv / "bin" / "pip", venv / "bin" / "ailocal"
    r = run([str(pip), "install", "-q", str(source)], dict(os.environ))
    check(r.returncode == 0, "the package installs", r.stderr[-400:])
    check(ailocal.is_file(), "the console script is installed")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AILOCAL_", "XDG_", "COMPOSE_"))}
    env.update({
        "HOME": str(home), "PATH": f"{venv / 'bin'}:{env.get('PATH', '')}",
        # docker compose is a CLI PLUGIN discovered under $HOME/.docker, so an
        # isolated HOME hides it and `docker compose` degrades to plain
        # `docker`. Keep pointing at the real config; nothing is written there.
        "DOCKER_CONFIG": os.path.expanduser("~/.docker"),
        "XDG_CONFIG_HOME": str(box / "xdg-config"),
        "XDG_DATA_HOME": str(box / "xdg-data"),
        "XDG_STATE_HOME": str(box / "xdg-state"),
        "AILOCAL_CONFIG": str(cfg), "AILOCAL_DATA": str(data),
        "AILOCAL_STATE": str(state),
        "COMPOSE_PROJECT_NAME": PROJECT,
        "AILOCAL_LITELLM_CONTAINER": f"{PROJECT}-litellm",
        "AILOCAL_SEARXNG_CONTAINER": f"{PROJECT}-searxng",
        "AILOCAL_LITELLM_PORT": PORT_LITELLM,
        "AILOCAL_SEARXNG_PORT": PORT_SEARXNG,
    })

    # Provisioning is a step of `ailocal install`, not a command of its own, so
    # the installed package's API is what a bootstrap actually calls here. No
    # source argument: the assets travel INSIDE the wheel, which is the property
    # this suite exists to prove.
    r = run([str(venv / "bin" / "python3"), "-c",
             "from ailocal import install, policy;"
             "install.provision(install.distribution_source(),"
             " policy.config_root(), policy.data_root(), policy.state_root())"],
            env)
    check(r.returncode == 0, "assets provision into the managed roots",
          r.stderr[-400:])
    check((cfg / "profiles" / "64gb.toml").is_file(),
          "profiles ship in the wheel and land in the config root")
    check((data / "deploy" / "litellm" / "hooks" / "tool_gateway.py").is_file(),
          "the proxy hooks ship in the wheel and land in the data root")
    (state / "active-profile").write_text("64gb\n")
    # .env is user configuration and is never shipped; synthesize one so the
    # isolated stack has a key of its own rather than borrowing production's.
    # BRAVE_API is a placeholder: settings.yml configures the engine, so
    # rendering requires a value, but this suite never issues a search and so
    # spends no quota.
    (cfg / ".env").write_text("LITELLM_MASTER_KEY=sk-integration-test-only\n"
                              "SEARXNG_SECRET=integration-test-only\n"
                              "BRAVE_API=integration-test-placeholder\n"
                              "OLLAMA_URL=http://host.docker.internal:11434\n")
    (cfg / ".env").chmod(0o600)

    # THE POINT OF THIS SUITE. From here the source does not exist.
    shutil.rmtree(source)
    check(not source.exists(), "the source checkout is gone")

    _suite.section("ORDINARY COMMANDS RUN WITH NO CHECKOUT")

    def ran(name, *args, allow_nonzero=True, timeout=300):
        r = run([str(ailocal), *args], env, timeout=timeout)
        blob = r.stdout + r.stderr
        check(str(source) not in blob,
              f"`ailocal {name}` reads nothing from the checkout",
              blob[-300:] if str(source) in blob else "")
        check("Traceback" not in blob and "No such file or directory" not in blob,
              f"`ailocal {name}` runs without a missing-file error", blob[-400:])
        if not allow_nonzero:
            check(r.returncode == 0, f"`ailocal {name}` exits 0", blob[-400:])
        return r

    r = ran("help", "help", allow_nonzero=False)
    check("local model runtime" in r.stdout, "help renders from the installed parser")
    # sync before profile: the effective profile is generated state, and a fresh
    # installation has none until the generator has run once.
    ran("sync", "sync", allow_nonzero=False)
    check((state / "litellm" / "config.yaml").is_file(),
          "sync generates into the managed state root")
    check((state / "litellm" / "effective-profile.json").is_file(),
          "the effective profile is generated")
    r = ran("profile", "profile", "active-tier", allow_nonzero=False)
    check(r.stdout.strip() == "64gb", "the active tier reads back from managed state")
    ran("validate", "validate")
    ran("doctor", "doctor")

    if want_stack:
        _suite.section("THE STACK COMES UP FROM MANAGED ASSETS")
        ran("start", "start", timeout=600)
        import time
        ok = False
        for _ in range(40):
            probe = subprocess.run(
                ["docker", "inspect", f"{PROJECT}-litellm",
                 "--format", "{{.State.Health.Status}}"],
                capture_output=True, text=True)
            if probe.stdout.strip() == "healthy":
                ok = True
                break
            time.sleep(3)
        detail = ""
        if not ok:
            # A health failure is useless without the reason, and the container
            # is removed moments later by `stop`.
            state = subprocess.run(
                ["docker", "inspect", f"{PROJECT}-litellm", "--format",
                 "{{.State.Status}} {{.State.ExitCode}} "
                 "{{range .State.Health.Log}}{{.Output}}{{end}}"],
                capture_output=True, text=True).stdout
            logs = subprocess.run(["docker", "logs", "--tail", "25",
                                   f"{PROJECT}-litellm"],
                                  capture_output=True, text=True)
            detail = (state + "\n" + logs.stdout + logs.stderr)[-1200:]
        check(ok, "the isolated proxy becomes healthy", detail)
        ran("status", "status")
        ran("stop", "stop", timeout=300)
        gone = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                              capture_output=True, text=True).stdout
        check(f"{PROJECT}-litellm" not in gone, "the isolated stack is removed")

    shutil.rmtree(box, ignore_errors=True)
    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
