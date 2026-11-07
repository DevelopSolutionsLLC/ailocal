"""Host-machine checks that only doctor renders.

Mostly WARN: a misplaced store or a cold model is expensive, not broken.
"""

from __future__ import annotations

import os
import pathlib
import shutil

from . import PASS, WARN, CheckResult
from ailocal import policy as P
from ailocal.primitives import command_output
from .services import INSPECT_TIMEOUT, ollama_listener_pid, ollama_loaded



def check_env_file() -> CheckResult:
    """Check both env files; the hand-edited user file is the one loosened."""
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
    return CheckResult("env", PASS, "environment present (generated + user)")


def check_cli_tools() -> list[CheckResult]:
    out = []
    # Required tools only; jq use always falls back. rg: generated instructions
    # use it, macOS ships none, Claude Code falls back to PATH's (#64).
    for tool, fix in (("docker", "brew install --cask docker-desktop"),
                      ("ollama", "brew install --cask ollama-binary"),
                      ("rg", "brew install ripgrep")):
        found = shutil.which(tool)
        out.append(CheckResult(f"cli:{tool}", PASS if found else WARN,
                               f"{tool} present" if found else f"{tool} CLI not found",
                               remediation=None if found else fix))
    return out


def _models_dir() -> str:
    """Return the running daemon's OLLAMA_MODELS, else the launchctl value."""
    pid = ollama_listener_pid()
    if pid:
        for tok in command_output("ps", "eww", "-p", pid, timeout=INSPECT_TIMEOUT).split():
            if tok.startswith("OLLAMA_MODELS="):
                return tok.split("=", 1)[1]
    return command_output("launchctl", "getenv", "OLLAMA_MODELS", timeout=INSPECT_TIMEOUT)


def check_model_store() -> list[CheckResult]:
    """Check the model store; unset OLLAMA_MODELS silently uses ~/.ollama."""
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
        size = command_output("du", "-sh", target,
                              timeout=INSPECT_TIMEOUT).split("\t")[0] or "?"
        out.append(CheckResult("model-store", PASS, f"OLLAMA_MODELS={target} ({size})"))

    if (home_store.is_dir() and any(home_store.iterdir())
            and str(home_store) != target):
        size = command_output("du", "-sh", str(home_store),
                              timeout=INSPECT_TIMEOUT).split("\t")[0] or "?"
        out.append(CheckResult(
            "orphan-store", WARN,
            f"{home_store} holds {size} that Ollama cannot see",
            remediation="ailocal install"))
    return out


def check_residency(model: str) -> CheckResult:
    """Check the model is loaded; a cold one pays a load and a cold eval."""
    loaded = {m.split(":", 1)[0] for m in ollama_loaded()}
    if model.split(":", 1)[0] in loaded:
        return CheckResult("residency", PASS, f"model loaded: {model}")
    return CheckResult("residency", WARN,
                       f"model NOT loaded: {model} — the next request pays a cold load",
                       remediation="send one small request to warm it")


def check_parallelism(context: int) -> CheckResult:
    """Report parallelism with context; KV cache is allocated per slot."""
    npar = os.environ.get("OLLAMA_NUM_PARALLEL") or command_output(
        "launchctl", "getenv", "OLLAMA_NUM_PARALLEL", timeout=INSPECT_TIMEOUT) or "default"
    return CheckResult("parallelism", PASS,
                       f"OLLAMA_NUM_PARALLEL={npar}, primary context={context} "
                       f"(KV is allocated per slot)")


def doctor_only_checks(primary_model: str, primary_context: int) -> list[CheckResult]:
    results = [check_env_file()]
    results += check_cli_tools()
    results += check_model_store()
    results += [check_residency(primary_model),
                check_parallelism(primary_context)]
    return results
