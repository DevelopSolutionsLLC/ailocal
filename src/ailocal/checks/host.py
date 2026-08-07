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
    env = P.config_root() / ".env"
    if not env.is_file():
        return CheckResult("env", WARN, ".env not found",
                           remediation="./ailocal install")
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
            remediation="ailocal autostart, then restart Ollama"))
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
            remediation="ailocal autostart --env-only"))
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


# ── session verification ────────────────────────────────────────────────────
# Pairs a ledger from deploy/litellm/hooks/session_observer.py with the git
# delta, on the host because the container cannot see the repository. It targets
# a confident report of work that did not happen, and claims no causation: a
# delta proves the tree changed while the session ran, not that it caused it.

#: Which tools mutate the tree is a registry fact (registry.yaml:mutating_tools).
#: PyYAML is absent on the host, so the registry may be unreadable even when
#: correct; the source is always reported so drift is visible.
_DEFINITE = {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"}
_AMBIGUOUS = {"Bash", "exec_command", "write_stdin"}

#: verdict -> exit status. UNVERIFIED is deliberately non-zero: "could not
#: check" must never be scriptable as success.
VERDICT_EXIT = {"VERIFIED": 0, "PARTIALLY_VERIFIED": 0,
                "SUSPICIOUS": 2, "UNVERIFIED": 3}


def _mutating_sets() -> tuple[set, set, str]:
    try:
        import yaml
        spec = (yaml.safe_load(
            (P.data_root() / "deploy/litellm/registry.yaml").read_text()
        ) or {}).get("mutating_tools") or {}
        if spec.get("definite"):
            return set(spec["definite"]), set(spec.get("ambiguous") or []), \
                "registry.yaml"
    except Exception:  # noqa: BLE001 - any failure degrades to the fallback
        pass
    return _DEFINITE, _AMBIGUOUS, "builtin fallback (registry unreadable on host)"


def _git_state(repo: pathlib.Path) -> dict | None:
    """Working-tree delta, or None when `repo` is not a git repository: "not a
    repo" and "no changes" must not look alike."""
    def git(*args) -> str:
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                           text=True, timeout=120)
        return r.stdout.strip() if r.returncode == 0 else ""

    if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=repo,
                      capture_output=True).returncode:
        return None
    lines = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    stat, staged = git("diff", "--stat"), git("diff", "--cached", "--stat")
    return {
        "changed_paths": len(lines),
        "paths": [l.split(None, 1)[-1] for l in lines[:15]],
        "untracked": sum(1 for l in lines if l.startswith("??")),
        "diff_stat": stat.splitlines()[-1] if stat else "",
        "staged_stat": staged.splitlines()[-1] if staged else "",
    }


def classify(ledger: dict, git: dict | None, test_rc: int | None,
             repo: str) -> tuple[str, str, list]:
    """(verdict, why, evidence). Ordered most-severe first; first rule decides."""
    called = set(ledger.get("tool_calls_by_name") or {})
    definite_set, ambiguous_set, source = _mutating_sets()
    definite = called & definite_set
    ambiguous_only = (called & (definite_set | ambiguous_set)) - definite_set
    errored = ledger.get("tool_results_errored") or 0
    unknown = ledger.get("tool_results_unknown_status") or 0
    total = ledger.get("tool_calls_total") or 0
    changed = bool(git and git["changed_paths"] > 0)

    evidence = [
        ("filesystem", "unavailable" if git is None
         else "changed" if changed else "unchanged"),
        ("mutating tools",
         "definite: " + ", ".join(sorted(definite)) if definite
         else "ambiguous only: " + ", ".join(sorted(ambiguous_only))
         if ambiguous_only else "none (read-only session)" if total
         else "no tool calls at all"),
        ("tool errors", f"{errored} errored" if errored
         else f"unknown for {unknown} result(s) — this route carries no error flag"
         if unknown else "none reported"),
        ("tests", "not run" if test_rc is None
         else "passed" if test_rc == 0 else f"FAILED (exit {test_rc})"),
        ("mutating source", source),
    ]

    if definite and not changed and git is not None:
        return ("SUSPICIOUS",
                f"{sorted(definite)} ran but the tree is identical to HEAD. The "
                f"edits were reverted, targeted a path outside {repo}, were "
                "blocked by a permission layer, or did not happen. The last two "
                "are indistinguishable from here — this is not proof of "
                "fabrication.", evidence)
    if total == 0:
        return ("UNVERIFIED", "No tool calls were recorded, so there is nothing "
                "to verify. For a task that needed tools, the model answered "
                "from assumption.", evidence)
    if git is None:
        return ("UNVERIFIED", f"{repo} is not a git repository, so no delta is "
                "available. Cannot verify is NOT the same as verified clean.",
                evidence)
    if test_rc not in (None, 0):
        return ("PARTIALLY_VERIFIED", f"Work reached the filesystem, but the "
                f"test command exited {test_rc}. Something happened; it does "
                "not yet hold.", evidence)
    if errored:
        return ("PARTIALLY_VERIFIED", f"{errored} tool call(s) errored. Check "
                "whether the model noticed or narrated success over them.",
                evidence)
    if changed and test_rc == 0:
        return ("VERIFIED", "Mutating tools ran, the tree changed, and the "
                "supplied test passed. This is the only combination that earns "
                "VERIFIED.", evidence)
    if changed:
        return ("PARTIALLY_VERIFIED", "The tree changed and no tool errored, but "
                "no test was run, so correctness is unestablished. Pass --test "
                "to reach VERIFIED.", evidence)
    if unknown:
        return ("UNVERIFIED", f"{unknown} result(s) have no success/failure "
                "signal on this route, and the tree did not change. Absence of "
                "an error flag is not success.", evidence)
    return ("UNVERIFIED", "A read-only session with no tree change. Nothing was "
            "claimed to change, so there is nothing to confirm.", evidence)


def verify_session(argv: list[str]) -> int:
    import json
    import textwrap

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    repo = pathlib.Path(opt("--repo", "."))
    directory = pathlib.Path(opt("--ledger-dir",
                                 P.state_root() / "captures" / "sessions"))
    ledgers = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime,
                     reverse=True)

    if "--list" in argv:
        if not ledgers:
            print(f"No ledgers in {directory}. Set AILOCAL_SESSION_LEDGER on the "
                  "proxy and run a session.")
            return 1
        for f in ledgers:
            d = json.loads(f.read_text())
            asked = " ".join((d.get("requested_change") or "").split())[:60]
            print(f"{f.name:22} {d.get('tool_calls_total'):3} calls  {asked}")
        return 0

    path = pathlib.Path(opt("--ledger")) if "--ledger" in argv else \
        (ledgers[0] if ledgers else None)
    if path is None or not path.exists():
        print(f"No ledger found. Looked in {directory}. The observer is off "
              "unless AILOCAL_SESSION_LEDGER is set — this is not evidence that "
              "a session did nothing.")
        return 1

    ledger = json.loads(path.read_text())
    git = _git_state(repo)
    asked = " ".join((ledger.get("requested_change") or "").split())
    print("=" * 70)
    print(f"LEDGER  {path.name}\nmodel   {ledger.get('model')}\nasked   {asked[:200]}")
    print(f"\nEXECUTED  {ledger.get('tool_calls_total')} tool calls, "
          f"{ledger.get('tool_results_total')} results, "
          f"{ledger.get('tool_results_errored')} errored, "
          f"{ledger.get('tool_results_unknown_status')} unknown status")
    for name, n in sorted((ledger.get("tool_calls_by_name") or {}).items(),
                          key=lambda kv: -kv[1]):
        print(f"    {name:32} x{n}")

    print()
    if git is None:
        print(f"FILESYSTEM  {repo} is not a git repository — no delta available.")
    else:
        print(f"FILESYSTEM  {git['changed_paths']} paths differ from HEAD "
              f"({git['untracked']} untracked)")
        for label, key in (("unstaged", "diff_stat"), ("staged", "staged_stat")):
            if git[key]:
                print(f"    {label}: {git[key]}")
        for p in git["paths"]:
            print(f"    {p}")

    test_rc = None
    if "--test" in argv:
        command = opt("--test")
        print(f"\nTESTS  $ {command}")
        # shell=True: --test is an operator-supplied command line, and splitting
        # it naively mangles quoting.
        r = subprocess.run(command, cwd=repo, capture_output=True, text=True,
                           timeout=120, shell=True)
        test_rc = r.returncode
        for line in (r.stdout or r.stderr).splitlines()[-8:]:
            print(f"    {line}")
        print(f"    exit {test_rc}")

    verdict, why, evidence = classify(ledger, git, test_rc, str(repo))
    print("\nEVIDENCE")
    for name, outcome in evidence:
        print(f"    {name:16} {outcome}")
    print(f"\nCLASSIFICATION  {verdict}")
    for line in textwrap.wrap(why, 68):
        print(f"    {line}")
    print("\nThis compares a claim against a tree state. It does not establish "
          "that this\nsession caused the delta, nor that the change was correct.")
    return VERDICT_EXIT[verdict]
