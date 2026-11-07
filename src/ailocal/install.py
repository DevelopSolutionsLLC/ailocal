"""Install: prerequisites, policy, tier, env, models, login agents, audit.

Shipped assets are read in place, never copied; a config-root file is
replaced only while it matches the digest recorded at install.
"""
from __future__ import annotations

import copy
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import policy, runtime
from .primitives import command_output
from .report import bad, dim, ok, step, warn

LA_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "ailocal"
MODEL_STORE = str(policy.ollama_model_store())
#: The login agents a current install writes; teardown removes exactly these.
AGENTS = ("com.ailocal.ollama", "com.ailocal.ollama-env", "com.ailocal.ollama-watch")
#: Pre-2026-11-03 agents, still booted out on teardown. Remove in 0.18.
RETIRED_AGENTS = ("com.ailocal.preload",)

#: Ollama's runtime env. MAX_LOADED caps count, not size (Ollama refuses a
#: model that will not fit).
OLLAMA_ENV = {
    "OLLAMA_HOST": "127.0.0.1:11434",
    "OLLAMA_MODELS": MODEL_STORE,
    # Default only for loads w/o keep_alive (LiteLLM sends per-role); -1 would
    # pin e.g. nomic-embed-text forever. Callers may still ask for -1.
    "OLLAMA_KEEP_ALIVE": "6h",
    # primary (+ fast, same weights), cyber, completion, and external
    # nomic-embed-text; 3 would evict a large model to load completion.
    "OLLAMA_MAX_LOADED_MODELS": "4",
    # Conservative: 2-slot memory on 16/32 GB and the patched runtime is
    # unmeasured (0.32.14 split one window across slots); operators may pick 2.
    "OLLAMA_NUM_PARALLEL": "1",
    # llama_cpp only (all shipped GGUF roles since #93); MLX ignores both and
    # runs an unquantized fp16 KV cache.
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
}


def _ollama_env() -> dict[str, str]:
    """Apply the operator's measured parallelism to both Ollama agents."""
    from . import environment
    parallel = environment.value("AILOCAL_OLLAMA_NUM_PARALLEL") or "1"
    if parallel not in ("1", "2"):
        raise ValueError("AILOCAL_OLLAMA_NUM_PARALLEL must be 1 or 2")
    return dict(OLLAMA_ENV, OLLAMA_NUM_PARALLEL=parallel)


def _run(*args: str | Path, check: bool = False, capture: bool = False,
         **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          capture_output=capture, **kw)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _ask(prompt: str, default: str = "") -> str:
    try:
        return input(f"  {prompt}: ").strip() or default
    except EOFError:
        return default


def _yes(prompt: str) -> bool:
    return _ask(f"{prompt} [y/N]").lower().startswith("y")


# provisioning

CONFIG_COMPONENTS = ("profiles",)
MANIFEST_NAME = "install-manifest.json"


def digest(path: Path) -> str:
    from .primitives import sha256_file
    return sha256_file(path)


def _tree(root: Path, component: str) -> dict:
    base = root / component
    return {str(p.relative_to(root)): digest(p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts} if base.is_dir() else {}


def distribution_source() -> Path:
    """Return the tree installed from: the package's own resources."""
    root = policy.data_root()
    if all((root / c).is_dir() for c in ("deploy", "clients")):
        return root
    raise SystemExit(f"install: the package carries no resources at {root}; "
                     "the installation is incomplete.")


def provision(source: Path, config: Path, state: Path, *,
              reset: bool = False) -> dict:
    """Install the user-editable policy defaults; nothing else is copied.

    A file is replaced only while it matches its recorded install digest;
    else it is the operator's. `reset=True` takes every shipped default but
    first backs up each edited file to a timestamped dir beside the root.
    """
    for p in (config, state):
        p.mkdir(parents=True, exist_ok=True)
    for c in CONFIG_COMPONENTS:
        dest = config / c
        if dest.exists() and (source / c).resolve() == dest.resolve():
            raise SystemExit(
                f"install: {dest} is the shipped source itself; refusing to "
                "install a tree over itself.")

    try:
        manifest = json.loads((state / MANIFEST_NAME).read_text())
        shipped = manifest["config"]
        provenance = True
    except (OSError, ValueError, KeyError, TypeError):
        shipped, provenance = {}, False

    backup = None
    if provenance and reset:
        # Back up only what would have been preserved, then preserve nothing.
        edited = [rel for rel, want in sorted(shipped.items())
                  if (config / rel).is_file() and digest(config / rel) != want]
        if edited:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = config.parent / f"{config.name}-backup-{stamp}"
            for rel in edited:
                dest = backup / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(config / rel, dest)
        preserved = []
    elif provenance:
        preserved = [rel for rel, want in sorted(shipped.items())
                     if (config / rel).is_file() and digest(config / rel) != want]
    elif reset:
        # No provenance under reset: nothing provably edited → no backup.
        preserved = []
    else:
        # No provenance: every existing file counts as edited.
        preserved = sorted(str(p.relative_to(config))
                           for c in CONFIG_COMPONENTS
                           for p in (config / c).rglob("*") if p.is_file())

    installed, absent = [], []
    for c in CONFIG_COMPONENTS:
        for src in sorted((source / c).rglob("*")):
            if not src.is_file() or "__pycache__" in src.parts:
                continue
            rel = str(src.relative_to(source))
            if rel in preserved:
                continue
            dest = config / rel
            # A deleted shipped default is a decision: report, never resurrect.
            if not dest.exists() and rel in shipped and not reset:
                absent.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            installed.append(rel)

    # Record shipped digests, never on-disk ones (an edit would look pristine).
    # A no-longer-shipped file is removed only while it matches its digest.
    retired = []
    for rel, want in sorted(shipped.items()):
        live = config / rel
        if (source / rel).exists() or not live.is_file():
            continue
        if digest(live) == want:
            live.unlink()
            retired.append(rel)
        else:
            preserved.append(rel)

    record = {"config": dict(shipped)}
    for c in CONFIG_COMPONENTS:
        record["config"].update({rel: d for rel, d in _tree(config, c).items()
                                 if rel not in preserved})
    (state / MANIFEST_NAME).write_text(json.dumps(record, indent=1, sort_keys=True))
    for rel in retired:
        record["config"].pop(rel, None)
    return {"installed": installed, "preserved": sorted(set(preserved)),
            "absent": absent, "retired": retired, "backup": backup}


def upgrade_model_defaults(source: Path, config: Path) -> dict:
    """Adopt shipped model/sampling/features, keeping authored geometry.

    Every candidate is parsed before any live file changes; originals backed up.
    """
    fields = ("active", "preferred", "source", "runtime_patch", "draft_num_predict",
              "stop", "reasoning", "temperature", "top_p", "top_k", "min_p",
              "presence_penalty", "repeat_penalty", "enabled", "role", "purpose",
              "strengths", "weaknesses")

    def literal(value: object) -> str:
        if isinstance(value, dict):
            return "{" + ", ".join(json.dumps(k) + " = " + literal(v) for k, v in value.items()) + "}"
        return json.dumps(value, ensure_ascii=False)

    def value_end(body: str, start: int) -> int:
        # A value ends at the first line completing it as TOML; trailing
        # comments and blanks stay with the next row.
        end = start
        for line in body[start:].splitlines(keepends=True):
            end += len(line)
            try:
                tomllib.loads(body[start:end])
                return end
            except tomllib.TOMLDecodeError:
                continue
        raise ValueError("model upgrade could not delimit an authored profile value")

    def table(text: str, header: str | None, values: dict[str, object]) -> str:
        # header None is the top-level preamble before the first table.
        if header is None:
            start = 0
        else:
            match = re.search(r"(?m)^" + re.escape(header) + r"[ \t]*(?:#[^\n]*)?$", text)
            if not match:
                return text.rstrip() + "\n\n" + header + "\n" + "".join(
                    key + " = " + literal(value) + "\n" for key, value in values.items() if value is not None)
            start = match.end()
        following = re.search(r"(?m)^\[", text[start:])
        end = start + following.start() if following else len(text)
        body = text[start:end]
        for key, value in values.items():
            row = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=", body)
            replacement = key + " = " + literal(value) + "\n" if value is not None else ""
            if row:
                body = body[:row.start()] + replacement + body[value_end(body, row.start()):]
            elif value is not None:
                body = body.rstrip() + "\n" + replacement
        if header is None:
            return body.rstrip() + "\n\n" + text[end:]
        return text[:start] + "\n" + body.lstrip("\n").rstrip() + "\n\n" + text[end:]

    prepared = {}
    with tempfile.TemporaryDirectory(prefix="ailocal-model-policy-") as tmp:
        root = Path(tmp)
        for tier in policy.TIERS:
            path = policy.profile_path(tier, config)
            if not path.is_file():
                continue  # A deliberately removed profile is not resurrected.
            if path.resolve() == policy.profile_path(tier, source).resolve() or path.is_symlink():
                raise ValueError("model upgrade refuses shipped-source paths and profile symlinks")
            before = policy.load_profile(tier, config)
            shipped = policy.load_profile(tier, source)
            expected = copy.deepcopy(before)
            text = path.read_text()
            if "disk_gb" in shipped:  # download footprint follows the models
                expected["disk_gb"] = shipped["disk_gb"]
                text = table(text, None, {"disk_gb": shipped["disk_gb"]})
            for role in policy.ROLES:
                if role not in shipped:
                    continue
                if role not in expected:
                    expected[role] = copy.deepcopy(shipped[role])
                    text = table(text, "[" + role + "]", expected[role])
                else:
                    updates = {key: shipped[role].get(key) for key in fields}
                    for key, value in updates.items():
                        if value is None:
                            expected[role].pop(key, None)
                        else:
                            expected[role][key] = copy.deepcopy(value)
                    text = table(text, "[" + role + "]", updates)
                model = shipped[role]["active"]
                guide = copy.deepcopy(shipped["model_guidance"][model])
                expected.setdefault("model_guidance", {})[model] = guide
                text = table(text, "[model_guidance." + json.dumps(model) + "]", guide)
            # Shared GGUF aliases share one window: raise smaller input budgets
            # to its authored maximum, keeping every output reserve.
            groups = {}
            for role in policy.ROLES:
                cfg = expected.get(role, {})
                if cfg.get("enabled", True) and cfg.get("active"):
                    groups.setdefault(cfg["active"], []).append(role)
            for roles in groups.values():
                total = max(policy.geometry(expected[r]["context_input"], expected[r]["max_output"])["total_context"] for r in roles)
                for role in roles:
                    ci = total - expected[role]["max_output"]
                    if expected[role]["context_input"] != ci:
                        expected[role]["context_input"] = ci
                        text = table(text, "[" + role + "]", {"context_input": ci})
            candidate = policy.profile_path(tier, root)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(text)
            if policy.load_profile(tier, root) != expected:
                raise ValueError("model upgrade could not preserve unrelated profile policy")
            if before != expected:
                prepared[path] = text
    if not prepared:
        return {"updated": [], "backup": None}
    backup = config / "backups" / ("model-defaults-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    for path in prepared:
        shutil.copy2(path, backup / path.name)
    changed = []
    try:
        for path, text in prepared.items():
            temporary = path.with_suffix(".toml.upgrade-tmp")
            temporary.write_text(text)
            os.replace(temporary, path)
            changed.append(path)
    except BaseException:
        for path in reversed(changed):
            shutil.copy2(backup / path.name, path)
        raise
    finally:
        for path in prepared:
            path.with_suffix(".toml.upgrade-tmp").unlink(missing_ok=True)
    return {"updated": [path.stem for path in prepared], "backup": backup}


#: Fields 0.15 roles differed in by design; a difference is never a conflict.
ROLE_SPLIT_FIELDS = ("role", "purpose", "strengths", "weaknesses", "context_input", "max_output")

#: [primary]'s sources, first match wins. Remove the 0.15 migration in 0.17.
PRIMARY_SOURCES = ("primary", "architecture", "implementation", "review")

#: 0.15.1 shipped defaults that differ from [architecture] (16gb/32gb Qwen3.5
#: sampling), so they do not read as operator edits (#87).
RETIRED_ROLE_DEFAULTS_015 = {
    tier: {"implementation": {"temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5, "reasoning": False},
           "fast": {"temperature": 0.0, "top_p": 0.9, "presence_penalty": 1.5, "reasoning": False}}
    for tier in ("16gb", "32gb")}


def _section_span(text: str, header: str) -> tuple[int, int] | None:
    """Return (start, end) of `[header]`, leaving the next table's preamble."""
    match = re.search(r"(?m)^\[" + re.escape(header) + r"\][ \t]*(?:#[^\n]*)?$\n?", text)
    if not match:
        return None
    following = re.search(r"(?m)^\[", text[match.end():])
    end = match.end() + following.start() if following else len(text)
    body = text[match.end():end]
    kept = 0
    for line in reversed(body.splitlines(keepends=True)):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        kept += len(line)
    return match.start(), end - kept


def _span_or_raise(text: str, header: str) -> tuple[int, int]:
    # A dotted or inline table parses but has no [header] line to cut.
    span = _section_span(text, header)
    if span is None:
        raise ValueError(f"role migration could not delimit [{header}]")
    return span


def migrate_profile_roles(source: Path, config: Path) -> dict:
    """Collapse 0.15's architecture/implementation/review into [primary] (#89).

    [primary] copies the first PRIMARY_SOURCES section verbatim. Another
    retired section differing outside ROLE_SPLIT_FIELDS from both shipped
    defaults is a conflict: reported, nothing written. A default-equivalent
    [fast] on a tier w/o one is dropped, and primary thinks off (ADR 018).
    """
    prepared, conflicts, dropped, kept_fast, thinking_off = {}, {}, {}, [], []
    for tier in policy.TIERS:
        path = policy.profile_path(tier, config)
        if not path.is_file():
            continue
        if path.resolve() == policy.profile_path(tier, source).resolve() or path.is_symlink():
            raise ValueError("role migration refuses shipped-source paths and profile symlinks")
        text = path.read_text()
        data = tomllib.loads(text)
        retired = [r for r in policy.RETIRED_ROLE_SECTIONS if r in data]
        if not retired and "fast" not in data:
            continue
        shipped = policy.load_profile(tier, source)
        stray_fast = "fast" in data and "fast" not in shipped
        if not retired and not stray_fast:
            continue
        src = next((s for s in PRIMARY_SOURCES if s in data), None)
        if src is None:
            raise ValueError(f"{tier}.toml declares [fast] without a chat role to collapse into")
        primary = data[src]

        def other(section: dict) -> dict:
            return {k: v for k, v in section.items() if k not in ROLE_SPLIT_FIELDS}

        expected = {k: copy.deepcopy(v) for k, v in data.items() if k not in retired}
        expected["primary"] = copy.deepcopy(primary)
        no_thinking = "fast" not in shipped and primary.get("reasoning") is True
        if no_thinking:
            expected["primary"]["reasoning"] = False
        remove, found, default = [r for r in retired if r != src], [], shipped["primary"]
        legacy = RETIRED_ROLE_DEFAULTS_015.get(tier, {})
        for section in remove:
            shipped_015 = legacy.get(section, {})
            for field in sorted(set(other(data[section])) | set(other(primary))):
                value = data[section].get(field)
                if value != primary.get(field) and value != default.get(field) \
                        and not (field in shipped_015 and value == shipped_015[field]):
                    found.append((field, {src: primary.get(field), section: value}))
        if stray_fast:
            if other(data["fast"]) in (other(primary), other(default), other(primary) | legacy.get("fast", {})):
                remove.append("fast")
                expected.pop("fast")
            else:
                kept_fast.append(tier)
        if found:
            conflicts[tier] = found
            continue
        if src == "primary" and not remove:
            continue  # a kept [fast] is the operator's policy
        if src != "primary":
            text = re.sub(r"(?m)^\[" + src + r"\]", "[primary]", text, count=1)
        if no_thinking:
            start, end = _span_or_raise(text, "primary")
            text = text[:start] + re.sub(r"(?m)^(reasoning[ \t]*=[ \t]*)true\b", r"\1false",
                                         text[start:end], count=1) + text[end:]
            thinking_off.append(tier)
        for section in remove:
            start, end = _span_or_raise(text, section)
            rest = text[end:].lstrip("\n")
            text = text[:start].rstrip("\n") + ("\n\n" + rest if rest else "\n")
        if tomllib.loads(text) != expected:
            raise ValueError("role migration could not preserve unrelated profile policy")
        prepared[path] = text
        dropped[tier] = remove
    if conflicts or not prepared:
        return {"migrated": [], "backup": None, "conflicts": conflicts,
                "dropped": {}, "kept_fast": [], "thinking_off": []}
    backup = config / "backups" / ("roles-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    for path in prepared:
        shutil.copy2(path, backup / path.name)
    changed = []
    try:
        for path, text in prepared.items():
            temporary = path.with_suffix(".toml.upgrade-tmp")
            temporary.write_text(text)
            os.replace(temporary, path)
            changed.append(path)
    except BaseException:
        for path in reversed(changed):
            shutil.copy2(backup / path.name, path)
        raise
    finally:
        for path in prepared:
            path.with_suffix(".toml.upgrade-tmp").unlink(missing_ok=True)
    return {"migrated": [path.stem for path in prepared], "backup": backup, "conflicts": {},
            "dropped": dropped, "kept_fast": kept_fast, "thinking_off": thinking_off}


#: 0.15 trailing quant token (`-q4_K_M`); structured names carry none (#93).
QUANT_SUFFIX = re.compile(r"-(?:i?q\d[\w]*|f16|bf16|fp16|f32)$", re.IGNORECASE)


def migrate_selection_names(config: Path) -> dict:
    """Strip a trailing quant token from selection names (#93) before loading.

    Else `load_profile` fails closed before any migration runs. Covers
    `active`, `preferred` and `model_guidance` keys; originals backed up.
    """
    prepared, renamed = {}, {}
    for tier in policy.TIERS:
        path = policy.profile_path(tier, config)
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text()
        data = tomllib.loads(text)
        names = set()
        for role in policy.ROLES:
            cfg = data.get(role)
            if isinstance(cfg, dict):
                names.update([cfg.get("active"), *(cfg.get("preferred") or [])])
        names.update((data.get("model_guidance") or {}).keys())
        pairs = {}
        for name in sorted(n for n in names if isinstance(n, str)):
            if policy.selection_name_error(name) is None:
                continue
            new = QUANT_SUFFIX.sub("", name)
            if new != name and policy.selection_name_error(new) is None:
                pairs[name] = new
        if not pairs:
            continue
        for old, new in pairs.items():
            text = text.replace(json.dumps(old), json.dumps(new))
        after = tomllib.loads(text)
        expected = json.loads(json.dumps(data))
        for old, new in pairs.items():
            expected = json.loads(json.dumps(expected).replace(json.dumps(old), json.dumps(new)))
        if after != expected:
            raise ValueError(f"{tier}.toml: selection rename could not preserve unrelated policy")
        prepared[path] = text
        renamed[tier] = sorted(pairs.items())
    if not prepared:
        return {"renamed": {}, "backup": None}
    backup = config / "backups" / ("names-" + str(time.time_ns()))
    backup.mkdir(parents=True)
    for path in prepared:
        shutil.copy2(path, backup / path.name)
    for path, text in prepared.items():
        temporary = path.with_suffix(".toml.upgrade-tmp")
        temporary.write_text(text)
        os.replace(temporary, path)
    return {"renamed": renamed, "backup": backup}


def role_conflict_report(conflicts: dict) -> str:
    """Return what the operator must decide before install can continue."""
    lines = ["Profile role migration stopped: retired role sections carry different values, and "
             "ailocal does not choose between them."]
    for tier, found in sorted(conflicts.items()):
        for field, values in found:
            lines.append(f"  {tier}.toml  {field}: " + ", ".join(
                f"[{section}] = {value!r}" for section, value in values.items()))
    lines += ["Edit the profile so the sections agree, delete the section you do not want, or run "
              "`ailocal install --reset-config` to take the shipped defaults (your files are backed up)."]
    return "\n".join(lines)


# host prerequisites

#: command → how to get it; ailocal names what is missing, never installs it.
#: Only tools w/o a fallback path qualify: jq does not (hook has a fallback).
PREREQUISITES = (
    ("docker", "Docker Desktop", "brew install --cask docker-desktop"),
    ("ollama", "Ollama", "brew install --cask ollama-binary"),
)
#: The menu-bar app holds :11434 and self-updates past tested versions (#74).
OLLAMA_APP = Path("/Applications/Ollama.app")


def require_prerequisites() -> None:
    """Refuse to install onto a machine that cannot run the result."""
    step("Prerequisites")
    missing = [(label, how) for cmd, label, how in PREREQUISITES if not _has(cmd)]
    if missing:
        print("  This machine is missing:")
        for label, how in missing:
            print(f"    - {label:16} {how}")
        raise SystemExit("\n  Install those, then re-run ailocal install.")
    if OLLAMA_APP.exists():
        raise SystemExit(f"  {OLLAMA_APP} competes with the managed server for :11434.\n"
                         "    brew uninstall --cask ollama-app   # or move it to the Trash; "
                         "then re-run ailocal install")
    if _run("docker", "ps", capture=True).returncode:
        raise SystemExit("  Docker is installed but the daemon is not responding.\n"
                         "    open -a Docker      # then re-run ailocal install")
    ok(f"{', '.join(label for _, label, _ in PREREQUISITES)} present, Docker running")


def _wait(predicate: Callable[[], bool], attempts: int, delay: float) -> bool:
    from .primitives import wait_until
    return wait_until(predicate, attempts, delay)


# Ollama environment and login agents

def _write_ollama_setenv_agent() -> None:
    """Write the login agent publishing the daemon's Ollama env to shells.

    Always rewritten with the server agent, else the two values diverge.
    """
    setenv = "; ".join(f"launchctl setenv {k} {v}" for k, v in _ollama_env().items())
    _write_agent("com.ailocal.ollama-env", ["/bin/sh", "-c", setenv])


def _write_agent(label: str, program: list[str], *, keep_alive: bool = False,
                 env: dict | None = None, calendar: dict | None = None,
                 watch: list[str] | None = None, interval: int | None = None) -> Path:
    LA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {"Label": label, "ProgramArguments": program, "RunAtLoad": True,
             "StandardOutPath": str(LOG_DIR / f"{label}.log"),
             "StandardErrorPath": str(LOG_DIR / f"{label}.err.log")}
    if env:
        plist["EnvironmentVariables"] = env
    if keep_alive:
        plist.update(KeepAlive=True, ThrottleInterval=10, ProcessType="Interactive")
    if watch:
        plist["WatchPaths"] = watch
    if interval:
        plist["StartInterval"] = interval
    if calendar:
        plist["StartCalendarInterval"] = calendar
        plist["RunAtLoad"] = False
        # launchd gives a job no interactive shell and a minimal PATH.
        plist["EnvironmentVariables"] = {"PATH": "/usr/local/bin:/opt/homebrew/bin:"
                                                 "/usr/bin:/bin:/usr/sbin:/sbin"}
    path = LA_DIR / f"{label}.plist"
    _bootout(label)
    path.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    loaded = _run("launchctl", "bootstrap", domain, path, capture=True, timeout=15)
    if loaded.returncode:
        raise OSError(f"launchd could not load {label}: {loaded.stderr.strip()[:300]}")
    if keep_alive:
        started = _run("launchctl", "kickstart", f"{domain}/{label}", capture=True, timeout=5)
        if started.returncode:
            detail = (started.stderr or started.stdout or "start pending").strip()[:300]
            warn(f"launchd start request for {label}: {detail}; verifying readiness")
    return path


#: Restart com.ailocal.ollama after a cask upgrade (#76). WatchPaths races
#: (launchd.plist(5)): wait for the binaries, restart only on a changed
#: target, rerun every 5 min. AILOCAL_LSOF/AILOCAL_LAUNCHCTL: tests only.
OLLAMA_WATCH = """\
bin=$1 lsof=${AILOCAL_LSOF:-/usr/sbin/lsof} launchctl=${AILOCAL_LAUNCHCTL:-/bin/launchctl}
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  want=$(readlink "$bin") || exit 0
  case $want in /*) ;; *) want=$(dirname "$bin")/$want ;; esac
  [ -x "$want" ] && [ -x "$(dirname "$want")/llama-server" ] && break
  sleep 5
done
[ -x "$want" ] || exit 0
pid=$("$lsof" -nP -tiTCP:11434 -sTCP:LISTEN | head -n 1)
[ -n "$pid" ] || exit 0
running=$("$lsof" -p "$pid" -a -d txt -Fn | sed -n 's/^n//p' | head -n 1)
[ "$running" = "$want" ] && exit 0
echo "restarting com.ailocal.ollama: $running -> $want"
exec "$launchctl" kickstart -k "gui/$(id -u)/com.ailocal.ollama"
"""


def _write_ollama_watch_agent(binary: str) -> None:
    """Watch the cask's Caskroom; other binaries need no watcher: remove it."""
    real = Path(os.path.realpath(binary))
    if len(real.parents) > 2 and real.parents[2].name == "Caskroom" and Path(binary).is_symlink():
        _write_agent("com.ailocal.ollama-watch", ["/bin/sh", "-c", OLLAMA_WATCH, "ollama-watch", binary],
                     watch=[str(real.parents[1])], interval=300)
        ok(f"com.ailocal.ollama-watch restarts the server after an upgrade of {real.parents[1]}")
    elif (path := LA_DIR / "com.ailocal.ollama-watch.plist").exists():
        _bootout("com.ailocal.ollama-watch")
        path.unlink()
        ok("removed com.ailocal.ollama-watch; the selected binary is not a cask link")


def _bootout(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    _run("launchctl", "bootout", target, capture=True, timeout=15)

    def removed() -> bool:
        status = _run("launchctl", "print", target, capture=True, timeout=5)
        if status.returncode == 0:
            return False
        if "Could not find service" in (status.stdout + status.stderr):
            return True
        raise OSError(f"Could not confirm that launch agent {label} stopped")

    # bootout returns before the job drains; bootstrap then may fail w/ EIO.
    if not _wait(removed, 120, 0.25):
        raise OSError(f"Could not confirm that launch agent {label} stopped")


def _migrate_model_store() -> None:
    """Move ~/.ollama/models into the shared store before repointing.

    Else Ollama re-downloads tens of GB. Per-file renames, so resumable.
    """
    src, dst = Path.home() / ".ollama" / "models", Path(MODEL_STORE)
    if not src.is_dir() or not any(src.iterdir()) or src.resolve() == dst.resolve():
        return
    entries = sorted(p for p in src.rglob("*") if p.is_file())
    if entries and all((dst / entry.relative_to(src)).exists() for entry in entries):
        return  # no migration work: do not kill a healthy managed daemon
    step("Migrating existing models to the shared store")
    if command_output("pgrep", "-x", "ollama"):
        # Stop the managed daemon only; ad-hoc `ollama serve` is the operator's.
        _bootout("com.ailocal.ollama")
        if not _wait(lambda: not command_output("pgrep", "-x", "ollama"), 12, 0.5):
            raise RuntimeError("an unmanaged ollama process is running; stop it, then re-run "
                               "ailocal install so the model store can move")
    moved = kept = 0
    for entry in entries:
        dest = dst / entry.relative_to(src)
        if dest.exists():
            kept += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry.rename(dest)
        moved += 1
    ok(f"moved {moved} file(s)" + (f", {kept} already present" if kept else ""))
    for d in sorted((p for p in src.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    if not any(src.iterdir()):
        src.rmdir()


def _prepare_model_store() -> None:
    """Make the store group-writable and setgid so other accounts can pull."""
    store = Path(MODEL_STORE)
    store.mkdir(parents=True, exist_ok=True)
    if os.access(store, os.W_OK):
        _run("chmod", "g+rwxs", store, capture=True)
    else:
        warn(f"{store} is not writable by {command_output('id', '-un')} — models will not pull here.")


def remove_login_agents() -> None:
    """Remove the agents, their session env, logs and owned runtimes.

    Reached from `ailocal teardown`; each is something install created.
    """
    step("Removing ailocal startup LaunchAgents")
    for label in AGENTS + RETIRED_AGENTS:
        _bootout(label)
        (LA_DIR / f"{label}.plist").unlink(missing_ok=True)
        ok(f"removed {label}")
    for key in sorted(set(OLLAMA_ENV) | {"OLLAMA_NUM_PARALLEL"}):
        _run("launchctl", "unsetenv", key, capture=True, timeout=5)
    ok("cleared the OLLAMA_* session environment com.ailocal.ollama-env published")
    if LOG_DIR.exists():
        shutil.rmtree(LOG_DIR, ignore_errors=True)
        ok(f"removed {LOG_DIR}")
    _clear_legacy_app_override()
    for line in prune_owned_runtimes():
        print(f"  {line}")


def prune_owned_runtimes() -> list[str]:
    """Remove every intact owned patched runtime; return one line per path."""
    from .ollama_patch import remove
    removed, preserved = remove()
    return ([f"✓ removed owned Ollama runtime {p}" for p in removed]
            + [f"⚠ preserved modified or unrecognized runtime {p}" for p in preserved])


def _clear_legacy_app_override() -> None:
    """Clear the old com.ollama.ollama disable override (#74 shim).

    Remove with the next release; the cask has no such agent.
    """
    receipt = LA_DIR / "com.ailocal.ollama.restore.json"
    if not (receipt.exists() or receipt.is_symlink()):
        return
    result = _run("launchctl", "enable", f"gui/{os.getuid()}/com.ollama.ollama",
                  capture=True, timeout=5)
    if result.returncode:
        warn("could not clear the legacy com.ollama.ollama override; receipt kept for a retry")
        return
    receipt.unlink()
    ok("cleared the legacy com.ollama.ollama startup override")


def _restore_ollama_agent(previous: bytes | None) -> None:
    _bootout("com.ailocal.ollama")
    path = LA_DIR / "com.ailocal.ollama.plist"
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(previous)
    if _run("launchctl", "bootstrap", f"gui/{os.getuid()}", path, capture=True, timeout=15).returncode:
        raise RuntimeError("Ollama rollback failed; restart the prior launch agent")


def _ollama_binary_version(binary: str) -> str:
    result = _run(binary, "--version", capture=True, timeout=10,
                  env=dict(os.environ, OLLAMA_HOST="127.0.0.1:0"))
    match = re.search(r"client version is\s+(\S+)", result.stdout)
    if result.returncode or not match:
        raise RuntimeError("Cannot establish the selected Ollama binary's version")
    return match.group(1)


def _ollama_is_current(binary: str) -> bool:
    """Return whether the running service already has the desired identity."""
    from .checks.services import http_json, Unreachable
    path = LA_DIR / "com.ailocal.ollama.plist"
    try:
        with path.open("rb") as stream:
            actual = plistlib.load(stream)
        # Same version ≠ same file: app and cask both shipped 0.40.14 (#74).
        if (not Path(binary).is_file()
                or actual.get("ProgramArguments") != [binary, "serve"]
                or actual.get("EnvironmentVariables") != _ollama_env()
                or actual.get("KeepAlive") is not True):
            return False
        job = _run("launchctl", "print", f"gui/{os.getuid()}/com.ailocal.ollama", capture=True)
        if job.returncode or not re.search(r"(?m)^\s*state = running\s*$", job.stdout):
            return False
        return http_json(f"{policy.ollama_url()}/api/version", timeout=2).get("version") == _ollama_binary_version(binary)
    except (OSError, ValueError, RuntimeError, Unreachable, subprocess.SubprocessError):
        return False


def _activate_ollama(binary: str) -> None:
    """Activate one native binary; restore the prior service on failure."""
    expected = _ollama_binary_version(binary)
    path = LA_DIR / "com.ailocal.ollama.plist"
    previous = path.read_bytes() if path.is_file() else None
    try:
        _write_agent("com.ailocal.ollama", [binary, "serve"], keep_alive=True,
                     env=_ollama_env())
        from .checks.services import http_json, Unreachable
        observed = "no response"

        def ready() -> bool:
            nonlocal observed
            try:
                version = http_json(f"{policy.ollama_url()}/api/version", timeout=1).get("version")
                observed = str(version)[:80]
                return version == expected
            except Unreachable:
                observed = "unreachable"
                return False

        if not _wait(ready, 120, 0.5):
            job = _run("launchctl", "print", f"gui/{os.getuid()}/com.ailocal.ollama",
                       capture=True, timeout=5)
            # Actionable failure w/o dumping the service env or logs.
            state = "; ".join(line.strip() for line in job.stdout.splitlines()
                              if re.match(r"\s*(state|runs|last exit code) =", line))[:300]
            state = state or ("not registered" if job.returncode else "unreported")
            raise RuntimeError(f"Ollama {expected} did not become ready; "
                               f"last observed: {observed}; launchd: {state}")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        _restore_ollama_agent(previous)
        raise
    ok(f"Ollama {expected} is responding through the selected native runtime")


def _port_owner() -> tuple[str, str]:
    """Return (pid on :11434, com.ailocal.ollama pid); either may be ""/"-"."""
    from .checks.services import ollama_listener_pid
    holder = ollama_listener_pid(timeout=None)
    agent = next((line.split()[0] for line in command_output("launchctl", "list").splitlines()
                  if line.endswith("\tcom.ailocal.ollama")), "")
    return holder, agent


def configure_ollama_services(*, binary: str | None = None) -> int:
    """The launchd-managed Ollama server — a step of `ailocal install`.

    launchd is the only mode: the `ollama-binary` cask has no app to read a
    `launchctl setenv` environment, so an env-only install left no server (#74).
    """
    _ollama_env()  # Reject an invalid operator override before changing a service.
    _prepare_model_store()
    _migrate_model_store()

    binary = binary or policy.ollama_binary()
    _clear_legacy_app_override()
    if _ollama_is_current(binary):
        ok("managed Ollama already matches the selected binary and environment")
    else:
        holder, agent = _port_owner()
        if holder and holder != agent:
            raise RuntimeError(f"port 11434 is held by pid {holder}, which com.ailocal.ollama "
                               "does not run; stop it, then re-run ailocal install")
        step("Installing com.ailocal.ollama")
        _activate_ollama(binary)
    ok(f"ollama serve managed by launchd (env baked in, logs in {LOG_DIR})")

    # Same dict, so the daemon and user shells cannot disagree.
    _write_ollama_setenv_agent()
    for k, v in _ollama_env().items():
        _run("launchctl", "setenv", k, v)
    ok("session env matches the server's (com.ailocal.ollama-env refreshed)")
    _write_ollama_watch_agent(binary)
    return 0


def _retire_model_warmup() -> None:
    """Retire the former login preload without affecting the Ollama server."""
    path = LA_DIR / "com.ailocal.preload.plist"
    if path.exists():
        _bootout("com.ailocal.preload")
        path.unlink()
        ok("retired model preload at login")
    legacy = policy.state_root() / "agents" / "preload.sh"
    try:
        if legacy.is_file() and not legacy.is_symlink():
            with legacy.open("rb") as stream:
                owned = stream.read(256).startswith(b"#!/bin/sh\n# Generated by ailocal.")
            if owned:
                legacy.unlink()
    except OSError:
        warn("preserved an unreadable legacy model-warmup script")


# the model set

SERVICES_GB, HEADROOM_GB = 3, 5


def _installed_models() -> dict:
    sizes = {}
    for line in command_output(policy.ollama_binary(), "list").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            try:
                gb = float(parts[2])
            except ValueError:
                continue
            sizes[parts[0]] = gb / 1000 if parts[3].upper().startswith("MB") else gb
    return sizes


def pull_models() -> int:
    """Pull the active tier's enabled models, deduped, minus what is on disk."""
    binary = policy.ollama_binary()
    if not command_output(binary, "list"):
        raise SystemExit("  Ollama daemon is not responding.\n"
                         f"    launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")

    roles = policy.effective_summary()["roles"]
    wanted: dict[str, list[str]] = {}
    sources = {}
    for cap, cfg in sorted(roles.items()):
        if cfg.get("enabled", True):
            wanted.setdefault(cfg["model"], []).append(cap)
            if cfg.get("source"):
                if cfg["model"] in sources and sources[cfg["model"]] != cfg["source"]:
                    raise SystemExit("conflicting source declarations for one model")
                sources[cfg["model"]] = cfg["source"]

    from .model_sources import current
    have = _installed_models()
    def size_of(tag: str) -> tuple[float, bool]:
        for name, gb in have.items():
            # Only an omitted :latest matches; a shared prefix ≠ same quant.
            if name == tag or name == f"{tag}:latest" or f"{name}:latest" == tag:
                # An imported tag is present only as the pinned revision.
                return gb, tag not in sources or current(tag, sources[tag])
        return 0.0, False

    step("Model set for this profile")
    present_gb = need_gb = 0.0
    for tag, users in sorted(wanted.items()):
        gb, present = size_of(tag)
        label = f"{gb:.1f} GB" if gb else "size unknown until pulled"
        print(f"    {tag}  ({', '.join(users)})  {label}, "
              f"{'present' if present else 'to download'}")
        present_gb += gb if present else 0
        need_gb += 0 if present else gb
    required = round(need_gb + SERVICES_GB + HEADROOM_GB)
    free = shutil.disk_usage(Path.home()).free // 1024 ** 3
    ok(f"{len(wanted)} unique models; {present_gb:.1f} GB present, "
       f"{need_gb:.1f} GB to download")
    ok(f"free space required now: {required} GB (incl. {SERVICES_GB} GB services "
       f"+ {HEADROOM_GB} GB headroom)   available: {free} GB")
    if free < required:
        warn(f"Only {free} GB free; this profile needs ~{required} GB")

    step("Installing/updating Ollama models")
    from .model_sources import verify_load
    failed = False
    for tag in sorted(wanted):
        if size_of(tag)[1]:
            succeeded = verify_load(tag)
            if succeeded:
                ok(f"{tag}  (already installed; load checked)")
        else:
            if tag in sources:
                from .model_sources import install as import_model
                succeeded = import_model(tag, sources[tag])
            else:
                succeeded = (_run(binary, "pull", tag).returncode == 0
                             and verify_load(tag))
            if succeeded:
                ok(f"{tag}  installed; load checked")
        if not succeeded:
            warn(f"{tag}  installation incomplete")
            failed = True
    return int(failed)


# tier and .env

def _memory_gb() -> int:
    return int(command_output("sysctl", "-n", "hw.memsize") or 0) // 1024 ** 3


def tier_for_memory(gb: int) -> str | None:
    """Return the largest tier the memory covers; never round up."""
    return next((t for t in ("128gb", "64gb", "32gb", "16gb")
                 if gb >= int(t[:-2])), None)


def select_tier(override: str, assume_yes: bool) -> str:
    """The one writer of the active-profile marker."""
    gb = _memory_gb()
    tier = tier_for_memory(gb)
    if tier is None:
        raise SystemExit(f"  {gb} GB of unified memory — ailocal requires at least 16 GB.")
    if override:
        if override not in policy.TIERS:
            raise SystemExit(f"  unknown profile {override!r} "
                             f"(expected {', '.join(policy.TIERS)})")
        if int(override[:-2]) > gb:
            warn(f"--profile {override} exceeds detected memory ({gb} GB); models "
                 "sized for it will swap or fail to load.")
            if assume_yes:
                raise SystemExit("  Refusing an unsafe override under --yes.")
            if not _yes(f"Use {override} anyway?"):
                raise SystemExit("  aborted")
        tier = override
    ok(f"Detected {gb} GB RAM → profile: {tier}")
    marker = policy.active_profile_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    current = marker.read_text().strip() if marker.is_file() else ""
    if current != tier:
        if current:
            warn(f"active profile was {current!r} — switching to {tier}")
        marker.write_text(f"{tier}\n")
    return tier


def _print_plan(tier: str, ram_note: str) -> None:
    summary = policy.effective_summary()
    roles = summary["roles"]
    models = [c["model"] for c in roles.values()]
    primary = max(set(models), key=models.count)
    shared = sorted(r for r, c in roles.items() if c["model"] == primary)
    first = roles[shared[0]]
    print(f"  architecture:        {os.uname().machine}")
    print(f"  {ram_note}")
    print(f"  selected profile:    {tier}  ({policy.profile_summary(tier)['status']})")
    print(f"  primary model:       {primary}")
    print(f"  shared across:       {', '.join(shared)}")
    for role, cfg in sorted(roles.items()):
        if cfg["model"] != primary:
            print(f"  {role + ' model:':<21}{cfg['model']}")
    print(f"  context_input:       {first['context_input']} "
          "(a maximum, not a per-request reservation)")
    print(f"  max_output:          {first['max_output']}")
    print(f"  total_context:       {first['context_input'] + first['max_output']}")
    print(f"  unique models:       {len(set(models))}")


def _write_env(assume_yes: bool) -> None:
    """Establish both env owners, migrating a legacy mixed file first."""
    from . import environment
    stale = policy.config_root() / ".env"
    if stale.is_file():
        warn(f"{stale} is a pre-0.12 file nothing reads any more: the master key in use is the one in "
             f"{environment.generated_file()}; copy provider keys you still need into "
             f"{environment.user_file()} and delete it")
    environment.ensure()
    ok(f"generated secrets in {environment.generated_file()} (chmod 600)")
    ok(f"your keys and overrides in {environment.user_file()} (chmod 600)")


# the audit / cleanup pair
# Cloud and local roots coexist by design, so findings are classified.
# Out of scope: ~/.claude, ~/.codex, SecretStorage, git, non-ailocal-* ctrs.

#: The one manual step; kept by its check so it cannot drift from README.
VSCODE_KEY_STEPS = ("grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env\n"
                    "then in VS Code: Copilot Chat → model picker → "
                    "'Manage Language Models' → ailocal → paste the key")


def _vscode_provider_group() -> dict | None:
    """Return ailocal's provider entry in chatLanguageModels.json, or None.

    Read-only: must never create the user directory it inspects.
    """
    from . import clients

    user_dir = clients._vscode_user_dir()
    if user_dir is None:
        return None
    try:
        groups = json.loads((user_dir / "chatLanguageModels.json")
                            .read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        return None
    return next((g for g in groups if isinstance(g, dict)
                 and g.get("vendor") == clients.VSCODE_VENDOR
                 and g.get("name") == clients.VSCODE_PROVIDER_NAME), None)


def _vscode_findings() -> list:
    """Return VS Code findings; silent when VS Code is not installed.

    Checks the provider matches the profile, the key reference exists, and
    nothing routes Copilot utility calls to the local model (concurrent 50k
    requests queue and die as ECONNRESET).
    """
    from . import clients
    from .checks import CheckResult, SKIP, WARN, passed

    user_dir = clients._vscode_user_dir()
    if user_dir is None:
        return [CheckResult("vscode", SKIP, "vscode   not installed")]

    out: list = []
    models_json = user_dir / "chatLanguageModels.json"
    try:
        groups = json.loads(models_json.read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        groups = []
    group = next((g for g in groups if isinstance(g, dict)
                  and g.get("vendor") == clients.VSCODE_VENDOR
                  and g.get("name") == clients.VSCODE_PROVIDER_NAME), None)
    want = runtime.proxy_url().rstrip("/")
    if group is None:
        out.append(CheckResult("vscode-provider", WARN,
                               "MISSING no LiteLLM provider group",
                               str(models_json), "run ailocal clients vscode"))
    else:
        from . import generation
        models = generation.load_models_yaml(policy.profile_path(policy.active_tier()))
        expected = clients.vscode_provider_document([], models)[-1]
        matches = group.get("models") == expected["models"] and group.get("apiType") == expected["apiType"]
        out.append(passed("vscode-provider", "vscode   native aliases, geometry and sampling match the active profile")
                   if matches else
                   CheckResult("vscode-provider", WARN,
                               f"STALE native model configuration; proxy is {want}",
                               str(models_json), "run ailocal clients vscode"))
        # SecretStorage is unwritable from outside: hand over the manual step.
        out.append(passed("vscode-key", "vscode   API key initialized")
                   if group.get("apiKey") else
                   CheckResult("vscode-key", WARN,
                               "vscode   API key not initialized; chat will 401",
                               str(models_json), VSCODE_KEY_STEPS))

    return out


def client_audit() -> list:
    """Return one row per optional client, its own `ailocal check` section.

    Kept out of audit(): a missing client is the user's choice, not a defect.
    """
    from . import clients as _clients
    from .checks import CheckResult, SKIP, WARN, passed

    out: list = []
    # deployed_client_root, not config_root: equal only by default.
    cfg = policy.deployed_client_root()
    probes = {"claude": cfg / "claude" / ".claude.json",
              "codex": cfg / "codex" / "config.toml",
              "vscode": None}  # VS Code's own state is read below instead
    # Never-opened VS Code: one row, decided before the probe, since
    # `code --list-extensions` creates the user directory.
    unlaunched = (_clients.present("vscode")
                  and _clients._vscode_user_dir() is None)

    for name in _clients.TARGETS:
        label, how = _clients.CLIENTS[name]
        probe = probes[name]
        if not _clients.present(name):
            out.append(CheckResult("client", SKIP,
                                   f"{label:14} not installed (optional)",
                                   remediation=how))
        elif name == "vscode" and unlaunched:
            out.append(CheckResult("client", WARN,
                                   f"{label:14} installed but never opened",
                                   "VS Code creates its settings folder on first launch",
                                   "open VS Code once, then: ailocal clients vscode"))
        elif name == "vscode" and not (_vscode_provider_group() or {}).get("apiKey"):
            # Not "configured": no supported way seeds SecretStorage, and a
            # literal key in chatLanguageModels.json would be a 0644 secret.
            group = _vscode_provider_group()
            out.append(CheckResult(
                "client", WARN,
                f"{label:14} " + ("provider configured; API key not initialized"
                                  if group else "installed; provider not configured"),
                None if group else "run ailocal clients vscode first",
                VSCODE_KEY_STEPS if group else "ailocal clients vscode"))
        elif probe is None or probe.is_file():
            out.append(passed("client", f"{label:14} configured"))
        else:
            out.append(CheckResult("client", WARN,
                                   f"{label:14} installed but not configured",
                                   str(probe), f"ailocal clients {name}"))

    if unlaunched or not _clients.present("vscode"):
        return out
    return out + _vscode_findings()


def audit() -> list:
    """Return read-only CheckResults; `detail` is the path cleanup acts on."""
    from .checks import CheckResult, SKIP, WARN, passed

    results: list = []

    def flag(klass: str, item: str, location: str | Path, action: str) -> None:
        results.append(CheckResult(klass.lower(), WARN, f"{klass} {item}",
                                   str(location), action))

    def fine(summary: str) -> None:
        results.append(passed("install", summary))

    def note(summary: str) -> None:
        results.append(CheckResult("install", SKIP, summary))

    cfg = policy.deployed_client_root()
    configure = cfg / "configure.zsh"
    if configure.is_file() and "CLAUDE_CONFIG_DIR" in configure.read_text():
        fine("isolation  configure.zsh sets CLAUDE_CONFIG_DIR")
    else:
        flag("DUPLICATE", "claude-local may share the cloud config root", configure,
             "re-run ailocal clients claude")
    for label in ("com.ailocal.ollama", "com.ailocal.ollama-env", "com.ailocal.ollama-watch"):
        plist = LA_DIR / f"{label}.plist"
        if not plist.is_file():
            note(f"{label} not installed")
        elif label.endswith(("-env", "-watch")):
            fine(f"{label} installed (on demand: not-running is correct)")
        elif "state = running" in command_output("launchctl", "print", f"gui/{os.getuid()}/{label}"):
            fine(f"{label} running")
        else:
            flag("STALE", f"{label} is installed but not running", plist,
                 f"launchctl bootstrap gui/{os.getuid()} '{plist}'")

    # A process count does not prove ownership of the port: an ad-hoc
    # `ollama serve` can hold :11434 while the agent is unloaded.
    holder, agent = _port_owner()
    if not holder:
        flag("MISSING", "nothing is listening on 11434", "launchd",
             f"launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")
    elif agent in ("", "-"):
        flag("UNMANAGED", f"port 11434 held by pid {holder}, com.ailocal.ollama not loaded",
             "an ad-hoc ollama serve", f"kill {holder}, then ailocal install")
    elif agent != holder:
        flag("UNMANAGED", f"port 11434 held by pid {holder}, not agent pid {agent}",
             "a competing Ollama instance", f"kill {holder}, then ailocal install")
    else:
        fine(f"port 11434 owned by the managed LaunchAgent (pid {holder})")

    # A removed log dir is invisible elsewhere: the agent writes to an unlinked
    # inode until restarted, so the fix is mkdir and kickstart.
    if not LOG_DIR.is_dir():
        flag("MISSING", "log directory is gone; server output is unreadable",
             LOG_DIR, f"mkdir -p '{LOG_DIR}' && launchctl kickstart -k "
                      f"gui/{os.getuid()}/com.ailocal.ollama")
    elif holder and any(
            "(deleted)" in line and str(LOG_DIR) in line
            for line in command_output("lsof", "-p", holder).splitlines()):
        flag("STALE", "server is writing to a deleted log file",
             LOG_DIR, f"launchctl kickstart -k gui/{os.getuid()}/com.ailocal.ollama")
    else:
        fine(f"log destination writable ({LOG_DIR})")

    return results


# install

USAGE = """usage: ailocal install [--yes] [--profile <16gb|32gb|64gb|128gb>]
                       [--ollama-patch <gemma4-switch-glu|none>]

One-time host setup, then a normal start. What only install does: verify
prerequisites, install the policy defaults, take :11434 with a launchd agent,
prepare the shared model store, write the master key, pick the tier, fetch the
pinned images and pull the model set. Everything after that is `ailocal start`.
Idempotent.

  --yes              unattended: accept every default
  --profile <tier>   use this tier's profile (16gb|32gb|64gb|128gb) instead of
                     the one detected from installed memory
  --ollama-patch     select the reviewed Gemma MLX loader patch, or none to
                     restore stock Ollama. Requires Go; the signed native files
                     come from the pinned release archive. Builds once. Without
                     the flag, the profile decides: a role that declares the
                     patch keeps it, and a profile with none returns to stock.
  --reset-config     take the shipped policy defaults even for files you have
                     edited. Your versions are copied to a timestamped backup
                     directory first, and the path is printed. Use this after a
                     release changes a default you never chose to diverge from;
                     without it, an edited file is kept and never updated.
  --upgrade-models   explicitly adopt shipped models, sampling, enabled features,
                     role text, download size and guides in all existing tiers. Back up originals; retain
                     authored output/context budgets, aligning shared runners to
                     their largest authored window. Other client policy is kept.
"""


def _opt(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else default


def cmd_install(argv: list[str]) -> int:
    if {"-h", "--help"} & set(argv):
        print(USAGE)
        return 0
    assume_yes = "--yes" in argv
    reset = "--reset-config" in argv
    if "--ollama-patch" in argv and argv.index("--ollama-patch") + 1 == len(argv):
        raise SystemExit("--ollama-patch requires gemma4-switch-glu or none")
    requested_patch = _opt(argv, "--ollama-patch")
    if requested_patch not in (None, "none", policy.ollama_patch_spec()["id"]):
        raise SystemExit("--ollama-patch requires gemma4-switch-glu or none")
    if requested_patch is None and policy.ollama_patch_selection() not in (
            "", policy.ollama_patch_spec()["id"]):
        raise SystemExit("Unknown AILOCAL_OLLAMA_PATCH; no runtime or service was changed.")

    require_prerequisites()

    step("Installing assets")
    source = distribution_source()
    report = provision(source, policy.config_root(), policy.state_root(),
                       reset=reset)
    ok(f"shipped assets read in place from {source}")
    ok(f"config {policy.config_root()}: {len(report['installed'])} file(s)")
    if report.get("backup"):
        warn(f"--reset-config replaced your edited files; originals saved to "
             f"{report['backup']}")
    for rel in report["preserved"]:
        dim(f"kept {rel} (edited since install)")
    for rel in report["absent"]:
        dim(f"absent {rel} (shipped default you removed)")
    for rel in report["retired"]:
        dim(f"retired {rel} (no longer shipped)")
    migration = migrate_profile_roles(source, policy.config_root())
    if migration["conflicts"]:
        raise SystemExit(role_conflict_report(migration["conflicts"]))
    for tier in migration["migrated"]:
        ok(f"{tier}.toml: retired role sections collapsed into [primary]"
           + (f"; dropped [{'], ['.join(migration['dropped'][tier])}]" if migration["dropped"][tier] else ""))
    for tier in migration["kept_fast"]:
        dim(f"{tier}.toml keeps your edited [fast]; the shipped profile serves that tier without one")
    for tier in migration["thinking_off"]:
        ok(f"{tier}.toml: [primary] thinking off; the shipped tier serves no fast role (ADR 018)")
    if migration["backup"]:
        warn(f"original profiles saved to {migration['backup']}; "
             "`ailocal install --upgrade-models` adopts the shipped role descriptions")
    names = migrate_selection_names(policy.config_root())
    for tier, pairs in names["renamed"].items():
        ok(f"{tier}.toml: " + ", ".join(f"{o} → {n}" for o, n in pairs) + " (the structured name; #93)")
    if names["backup"]:
        dim(f"original profiles saved to {names['backup']}")
    if "--upgrade-models" in argv:
        upgrade = upgrade_model_defaults(source, policy.config_root())
        if upgrade["updated"]:
            ok("model policy upgraded: " + ", ".join(upgrade["updated"]))
            warn(f"original profiles saved to {upgrade['backup']}")
        else:
            ok("model policy already matches shipped defaults")

    step("Detecting hardware profile")
    ram = _memory_gb()
    tier = select_tier(_opt(argv, "--profile") or "", assume_yes)
    required_patch = policy.required_runtime_patch(tier)
    if required_patch and requested_patch == "none":
        raise SystemExit("The selected profile requires the patched Ollama runtime. "
                         "Choose a compatible model/profile before returning to stock Ollama.")
    if requested_patch is None:
        if required_patch:
            requested_patch = required_patch
        elif policy.ollama_patch_selection():
            # No enabled role needs the patch: w/o --ollama-patch, go stock.
            requested_patch = "none"
            ok("no enabled role needs the patched Ollama runtime; returning to stock Ollama")

    step("Configuring environment")
    _write_env(assume_yes)

    # Build/test first: a failed candidate never stops the running runtime.
    candidate_binary = None
    if requested_patch not in (None, "none"):
        step("Preparing the reviewed Ollama loader patch")
        from . import ollama_patch
        try:
            candidate_binary = str(ollama_patch.prepare())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            # Show the step's last lines: they name the failing test or check.
            detail = " ".join(str(error).strip().splitlines()[-3:])[-400:]
            warn(f"Patched Ollama preparation failed ({type(error).__name__}): {detail}; "
                 "the current service and runtime selection were preserved")
            return 1
    elif requested_patch == "none":
        candidate_binary = policy.stock_ollama_binary()

    step("Configuring Ollama")
    agent = LA_DIR / "com.ailocal.ollama.plist"
    previous_agent = None
    try:
        previous_agent = agent.read_bytes() if agent.is_file() else None
        configure_ollama_services(binary=candidate_binary)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, (OSError, RuntimeError, ValueError)) else type(error).__name__
        warn(f"Ollama activation failed ({detail}); "
             "the authored runtime selection was not changed")
        return 1
    try:
        _retire_model_warmup()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        warn(f"Could not retire the old login model preload ({error}); installation is incomplete")
        return 1
    stock = requested_patch == "none" or (requested_patch is None and not policy.ollama_patch_selection())
    if requested_patch is not None:
        from . import environment
        try:
            environment.set_ollama_patch("" if requested_patch == "none" else requested_patch)
        except OSError:
            try:
                _restore_ollama_agent(previous_agent)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                warn("Runtime selection could not be saved and service rollback failed. "
                     "Repair configuration permissions, then re-run ailocal install with the selected --ollama-patch option.")
                return 1
            warn("Runtime selection could not be saved; the prior service was restored. "
                 "Repair configuration permissions and retry.")
            return 1
    if stock:
        # Nothing selects a patched runtime; a leftover build has no owner.
        for line in prune_owned_runtimes():
            print(f"  {line}")

    # Generate before pulling: the plan renders from the generated artifact.
    step("Generating configuration")
    from . import generation
    if generation.main([]):
        raise SystemExit("  generation failed — nothing was pulled")
    print()
    _print_plan(tier, f"physical memory:     {ram} GB")

    # The only image fetch; digest pins → first fetch only. `start` is offline.
    step("Pulling pinned Docker images")
    try:
        runtime.compose("pull")
        runtime.cmd_start([])
    except subprocess.SubprocessError:
        warn("Docker rollout failed; installation is incomplete. "
             "Resolve the Docker error above, then re-run ailocal install.")
        return 1

    if pull_models():
        step("Model installation needs attention")
        return 1

    # --yes never edits user roots (~/.zshrc, VS Code); it provisions only
    # ailocal's own claude-local/codex-local roots and their agent MCP.
    from . import clients
    step("Client configs (optional)")
    detected = [n for n in clients.TARGETS if clients.present(n)]
    print("  Supported clients are optional consumers of the proxy — ailocal")
    print("  serves all three and requires none.")
    for name in clients.TARGETS:
        label = clients.CLIENTS[name][0]
        print(f"    {'✓' if name in detected else '✗'} {label:14}"
              f"{'installed' if name in detected else 'not installed'}")
    clients.report_missing(clients.TARGETS)
    if not detected:
        ok("Nothing to configure. Install a client, then: ailocal clients")
    elif assume_yes:
        # `ailocal check` requires these runtimes in the roots created above.
        local = [n for n in detected if n in ("claude", "codex")]
        for name in local:
            clients._TARGETS[name]()
        if local and not clients.provision_local_agent(local):
            step("Agent MCP registration needs attention")
            return 1
        ok(f"Detected {', '.join(detected)} — ailocal-owned roots provisioned; "
           "~/.zshrc and VS Code are not configured under --yes. "
           "Run: ailocal clients")
    else:
        print("\n  ⚠ Configuring backs up, then rewrites/merges existing client configs.")
        targets = _ask(f"Configure which? [Enter for the detected "
                       f"{', '.join(detected)} | a name | all | none]")
        if targets == "none":
            ok("Skipped — run later with: ailocal clients [claude|codex|vscode]")
        elif clients.main(targets.split()):
            bad("Client configuration failed; run: ailocal clients [claude|codex|vscode]")
            return 1

    from .checks import run as checks_run
    if checks_run.main(["check"]):
        step("Installation needs attention")
        print("  Setup finished, but validation failed. Resolve the findings above,")
        print("  then run ailocal check again.")
        return 1

    step("Done")
    print(f"  LiteLLM proxy is ready at {runtime.proxy_url()}")
    print("  Verify a real request:  ailocal check")
    # Offered, never applied: it restarts Docker Desktop.
    from . import docker_desktop
    current = docker_desktop.effective()
    if current is not None and docker_desktop.drift(current):
        print("  Recommended Docker Desktop settings are not all in effect: ailocal docker check")
    return 0


COMMANDS = {"install": cmd_install}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"usage: python -m ailocal.install <{'|'.join(COMMANDS)}>",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
