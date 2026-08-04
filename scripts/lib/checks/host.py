"""Host-machine checks that only doctor renders.

Neither repository configuration nor running-service state: these describe the
developer's machine and always carry remediation. validate and smoke do not
consult them, which is why they are separate from config.py and services.py.

Most results here are WARN. A misplaced model store or a cold model is
expensive, not broken, and must not fail a runtime check.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

from . import PASS, WARN, CheckResult
from .services import INSPECT_TIMEOUT, ollama_loaded

REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent


def _run(cmd: list[str], timeout: int = INSPECT_TIMEOUT) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_env_file() -> CheckResult:
    env = REPO / ".env"
    if not env.is_file():
        return CheckResult("env", WARN, ".env not found",
                           remediation="./scripts/install.sh")
    mode = env.stat().st_mode & 0o077
    if mode:
        return CheckResult("env", WARN, ".env is readable by other users",
                           f"mode {oct(env.stat().st_mode & 0o777)}",
                           "chmod 600 .env")
    return CheckResult("env", PASS, ".env present")


def check_cli_tools() -> list[CheckResult]:
    out = []
    for tool, fix in (("docker", "install Docker Desktop"),
                      ("ollama", "brew install ollama"),
                      ("jq", "brew install jq")):
        found = shutil.which(tool)
        out.append(CheckResult(f"cli:{tool}", PASS if found else WARN,
                               f"{tool} present" if found else f"{tool} CLI not found",
                               remediation=None if found else fix))
    return out


def _models_dir() -> str:
    """Where the running daemon actually stores models.

    Two valid configurations exist: the autostart LaunchAgent bakes
    OLLAMA_MODELS into its own environment and never calls `launchctl setenv`,
    while the env-only path does. Asking the running process is correct under
    both; setenv is only a fallback when no daemon is up.
    """
    pid = _run(["lsof", "-ti", ":11434"]).split("\n")[0]
    if pid:
        for tok in _run(["ps", "eww", "-p", pid]).split():
            if tok.startswith("OLLAMA_MODELS="):
                return tok.split("=", 1)[1]
    return _run(["launchctl", "getenv", "OLLAMA_MODELS"])


def check_model_store() -> list[CheckResult]:
    """An unset OLLAMA_MODELS silently uses ~/.ollama, so a second account
    re-downloads tens of gigabytes while the shared store still looks populated.
    """
    out = []
    target = _models_dir()
    home_store = pathlib.Path.home() / ".ollama" / "models"
    if not target:
        out.append(CheckResult(
            "model-store", WARN,
            "OLLAMA_MODELS unset — models go to ~/.ollama, not the shared store",
            remediation="bash scripts/setup-startup.sh (autostart) or "
                        "scripts/setup-ollama-env.sh, then restart Ollama"))
    elif not os.path.isdir(target):
        out.append(CheckResult("model-store", WARN,
                               f"OLLAMA_MODELS={target} does not exist"))
    elif not os.access(target, os.W_OK):
        out.append(CheckResult("model-store", WARN,
                               f"OLLAMA_MODELS={target} is not writable — pulls will fail"))
    else:
        size = _run(["du", "-sh", target]).split("\t")[0] or "?"
        out.append(CheckResult("model-store", PASS, f"OLLAMA_MODELS={target} ({size})"))

    if (home_store.is_dir() and any(home_store.iterdir())
            and str(home_store) != target):
        size = _run(["du", "-sh", str(home_store)]).split("\t")[0] or "?"
        out.append(CheckResult(
            "orphan-store", WARN,
            f"{home_store} holds {size} that Ollama cannot see",
            remediation="bash scripts/setup-ollama-env.sh"))
    return out


def check_residency(model: str) -> CheckResult:
    """A cold model pays both a load and a cold prompt evaluation."""
    loaded = {m.split(":", 1)[0] for m in ollama_loaded()}
    if model.split(":", 1)[0] in loaded:
        return CheckResult("residency", PASS, f"model loaded: {model}")
    return CheckResult("residency", WARN,
                       f"model NOT loaded: {model} — the next request pays a cold load",
                       remediation="send one small request to warm it")


def check_parallelism(context: int) -> CheckResult:
    """KV cache is allocated per parallel slot, so both numbers matter together."""
    npar = os.environ.get("OLLAMA_NUM_PARALLEL") or _run(
        ["launchctl", "getenv", "OLLAMA_NUM_PARALLEL"]) or "default"
    return CheckResult("parallelism", PASS,
                       f"OLLAMA_NUM_PARALLEL={npar}, architecture context={context} "
                       f"(KV is allocated per slot)")


def doctor_only_checks(architecture_model: str, architecture_context: int) -> list[CheckResult]:
    results = [check_env_file()]
    results += check_cli_tools()
    results += check_model_store()
    results += [check_residency(architecture_model),
                check_parallelism(architecture_context)]
    return results
