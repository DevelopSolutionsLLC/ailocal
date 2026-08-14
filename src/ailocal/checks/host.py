"""Host-machine checks that only doctor renders.

The developer's machine rather than the repository or the running stack, always
with remediation. Mostly WARN: a misplaced store or a cold model is expensive,
not broken, and must not fail a runtime check.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

from . import PASS, WARN, CheckResult
from ailocal import policy as P
from .services import INSPECT_TIMEOUT, ollama_loaded



def _run(cmd: list[str], timeout: int = INSPECT_TIMEOUT) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_env_file() -> CheckResult:
    """Both environment owners: the generated secrets and the user's file.

    This checked one path because there used to be one file. Reporting only the
    generated half would leave the user's provider keys unwatched — and they are
    the half a human edits, so they are the half whose mode gets loosened.
    """
    from .. import environment
    generated = environment.generated_file()
    if not generated.is_file():
        return CheckResult("env", WARN, f"{generated.name} not found",
                           remediation="ailocal install")
    loose = [p for p in (generated, environment.user_file())
             if p.is_file() and p.stat().st_mode & 0o077]
    if loose:
        return CheckResult("env", WARN, "environment file readable by other users",
                           ", ".join(f"{p}: {oct(p.stat().st_mode & 0o777)}"
                                     for p in loose),
                           "chmod 600 " + " ".join(str(p) for p in loose))
    if environment.needs_migration():
        return CheckResult("env", WARN,
                           "a legacy mixed .env is still present",
                           str(environment.legacy_file()),
                           "ailocal install   (migrates it into the two owners)")
    return CheckResult("env", PASS, "environment present (generated + user)")


def check_cli_tools() -> list[CheckResult]:
    out = []
    # Only tools ailocal cannot work without. jq is deliberately absent: every
    # use of it is guarded and falls back, so warning about it trained people to
    # install something nothing needs.
    for tool, fix in (("docker", "brew install --cask docker-desktop"),
                      ("ollama", "brew install --cask ollama-app")):
        found = shutil.which(tool)
        out.append(CheckResult(f"cli:{tool}", PASS if found else WARN,
                               f"{tool} present" if found else f"{tool} CLI not found",
                               remediation=None if found else fix))
    return out


def _models_dir() -> str:
    """Where the running daemon actually stores models.

    The autostart agent bakes OLLAMA_MODELS into its own environment; the
    env-only path uses `launchctl setenv`. Asking the running process is correct
    under both, with setenv as the no-daemon fallback."""
    pid = _run(["lsof", "-ti", ":11434"]).split("\n")[0]
    if pid:
        for tok in _run(["ps", "eww", "-p", pid]).split():
            if tok.startswith("OLLAMA_MODELS="):
                return tok.split("=", 1)[1]
    return _run(["launchctl", "getenv", "OLLAMA_MODELS"])


def check_model_store() -> list[CheckResult]:
    """An unset OLLAMA_MODELS silently uses ~/.ollama, so a second account
    re-downloads everything while the shared store still looks populated.
    """
    out = []
    target = _models_dir()
    home_store = pathlib.Path.home() / ".ollama" / "models"
    if not target:
        out.append(CheckResult(
            "model-store", WARN,
            "OLLAMA_MODELS unset — models go to ~/.ollama, not the shared store",
            remediation="ailocal install, then restart Ollama"))
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
            remediation="ailocal install"))
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
