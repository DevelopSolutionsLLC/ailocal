"""Benchmark orchestrator. ailocal owns orchestration; external tools own scoring.

ailocal does exactly six things here: pick models from a hardware profile, stand
up temporary authenticated LiteLLM aliases carrying vendor presets, invoke an
external benchmark engine against them, capture telemetry, restore the production
runtime, and write a report.

It deliberately does NOT own datasets, prompting, scoring, tokenization or task
definitions. lm-evaluation-harness already does those, correctly, and a previous
attempt to reimplement them grew to 42 files before producing a single ranking.

Every scored request goes through authenticated LiteLLM. Direct Ollama is used
only for residency and metadata, and can never produce a score.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ── moved modules ───────────────────────────────────────────────────────────
# The split below is BEHAVIOUR-PRESERVING. Every name these modules own is
# re-exported here, so `import suite as B; B.run_client_turn(...)` keeps
# working for benchmarks/models.py, lib/ruler.py,
# tests/benchmark.py. (The A/B/C repro script that first
# established this was deleted 2026-08-03: the hypothesis was FALSIFIED and
# the contract is now enforced by that test, so the script only duplicated it.)
from evidence import (  # noqa: E402,F401
    EVIDENCE_COMPLETE,
    EVIDENCE_MISSING,
    EVIDENCE_PARTIAL,
    REPO,
    _REDACT,
    capture_litellm_log,
    container_id,
    evidence_dir,
    evidence_state,
    new_run_id,
    redact,
    run_dir,
    runtime_dir,
    state_dir,
)
from runtime import (  # noqa: E402,F401
    ALIAS_PREFIX,
    LITELLM,
    OLLAMA,
    PRECALL_MARGIN,
    _compose,
    _emit,
    _json,
    _key_from,
    _wait_healthy,
    alias_name,
    aliases,
    api_key,
    apply_aliases,
    build_alias,
    installed,
    litellm_healthy,
    model_info,
    resident,
    restore,
    telemetry,
    unload,
)
from clients import (  # noqa: E402,F401
    CONFINEMENT_ESCAPE_BLOCKED,
    CONFINEMENT_INVALID,
    CONFINEMENT_UNAVAILABLE,
    CONFINEMENT_VERIFIED,
    INVALID_CONFINEMENT_BREACH,
    INVALID_CONFINEMENT_CONFIGURATION,
    INVALID_PERMISSIONS,
    PROBE_OUTPUT_INVALID,
    confinement_args,
    confinement_manifest,
    detect_escape,
    plant_canary,
    CONFINEMENT_WORKTREE_INSIDE_DENIED_ROOT,
    DENIED_ROOTS,
    CONFINABLE,
    INSIDE_DENIED_ROOT,
    OUTSIDE_OWNED_TEMP_ROOT,
    ROOT_NOT_PRIVATE,
    SYMLINK_TARGET_DENIED,
    WORKTREE_CLEANUP_FAILED,
    benchmark_worktree_root,
    confinement_settings,
    sweep_worktree_root,
    worktree_is_confinable,
    verify_confinement,
    CLIENTS,
    CLIENT_ENV,
    IMPLICIT_RESUME,
    PERMISSION_PREFLIGHT,
    SessionLost,
    _CLAUDE_RESULT_KEYS,
    _OUTPUT_LIMIT_MARKERS,
    _shq,
    classify_client_outcome,
    client_version,
    disposable_worktree,
    parse_client_result,
    permission_args,
    permission_manifest_hash,
    remove_worktree,
    run_client_scenario,
    run_client_turn,
    served_models_since,
    verify_permissions,
    verify_routing,
)
from engines import (  # noqa: E402,F401
    IMPLAUSIBLE_INPUT_RATE,
    _flatten,
    _stats,
    _stream_timed,
    audit,
    cold_load_seconds,
    run_lm_eval,
    throughput_probe,
    venv_bin,
)

#: The memory-tier ladder. install.sh owns the canonical thresholds;
#: tests/benchmark.py parses install.sh and asserts these match, so
#: there is one source of truth enforced by a test rather than by convention.
#:
#: NEVER ROUND UP. Selecting at 75% of a tier's name gave a 24 GB machine the
#: 32gb profile and models it could not hold.
TIERS = ("16gb", "32gb", "64gb", "128gb")


def ram_gb() -> int:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                         text=True, timeout=10).stdout.strip()
    return int(out) // 1024 // 1024 // 1024 if out else 0


def tier_for_gb(gb: int):
    if gb >= 128:
        return "128gb"
    if gb >= 64:
        return "64gb"
    if gb >= 32:
        return "32gb"
    if gb >= 16:
        return "16gb"
    return None
CONFIG = REPO / "benchmarks" / "benchmark.yaml"
PROFILE, EXPLICIT, PROFILE_PLUS = "PROFILE", "EXPLICIT", "PROFILE_PLUS_EXPLICIT"


# ── tiny config reader ──────────────────────────────────────────────────────
# The repo has no pyyaml dependency and the benchmark is not the place to add
# one. This reads the shape benchmarks/benchmark.yaml actually uses and RAISES on
# anything else rather than guessing.


def load_config(path: Path = CONFIG) -> dict:
    root, stack = {}, [(-1, {})]
    stack[0] = (-1, root)
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        body = raw.strip()
        if "#" in body and body.count('"') % 2 == 0:
            body = body.split("#", 1)[0].strip()
        # Block list item: `- "text"`. Client scenarios are ordered turn lists,
        # which is the only place these appear.
        if body.startswith("- "):
            parent = stack[-1][1]
            if not isinstance(parent, list):
                raise ValueError(f"{path}:{lineno}: list item outside a list")
            parent.append(_scalar(body[2:]))
            continue
        if body[0] in "\"'":
            close = body.index(body[0], 1)
            key, rest = body[1:close], body[close + 1:].lstrip()
            if not rest.startswith(":"):
                raise ValueError(f"{path}:{lineno}: quoted key without ':'")
            value = rest[1:].strip()
        elif ":" in body:
            key, _, value = body.partition(":")
            key, value = key.strip(), value.strip()
        else:
            raise ValueError(f"{path}:{lineno}: not a mapping line: {raw!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            # A key whose first child is `- ` opens a list, not a mapping.
            nxt = _peek_child(path, lineno, indent)
            child = [] if nxt == "list" else {}
            parent[key] = child
            stack.append((indent, child))
        elif value.startswith("{"):
            parent[key] = _flow(value, f"{path}:{lineno}")
        else:
            parent[key] = _scalar(value)
    return root


def _peek_child(path: Path, lineno: int, indent: int):
    """Is the next more-indented line a list item or a mapping key?"""
    for raw in path.read_text().splitlines()[lineno:]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if len(raw) - len(raw.lstrip()) <= indent:
            return "map"
        return "list" if raw.lstrip().startswith("- ") else "map"
    return "map"


def _flow(raw: str, where: str) -> dict:
    inner = raw.strip()[1:-1]
    if "{" in inner:
        raise ValueError(f"{where}: nested flow maps unsupported")
    out = {}
    # Split on commas OUTSIDE quotes: `modes: "off,on"` is one value, not two.
    parts, buf, q = [], [], None
    for ch in inner:
        if q:
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch == ",":
            parts.append("".join(buf)); buf = []; continue
        buf.append(ch)
    parts.append("".join(buf))
    for pair in parts:
        if not pair.strip():
            continue
        k, sep, v = pair.partition(":")
        if not sep:
            raise ValueError(f"{where}: flow entry without ':': {pair!r}")
        out[k.strip()] = _scalar(v)
    return out


def _scalar(v):
    v = v.strip().strip('"').strip("'")
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


# ── profile / model selection ───────────────────────────────────────────────


def parse_profile(tier: str) -> dict:
    """{capability: {active, context, enabled}} for ANY tier, from generated data.

    This used to parse profile YAML for non-active tiers, which was the last
    runtime path contradicting "sync-models is the sole YAML consumer" — and it
    kept a second parser reachable from live code. Cross-tier benchmark planning
    now reads the normalized all-tier artifact, and a stale or missing tier
    fails closed instead of silently parsing whatever is on disk."""
    import policy as _pc
    roles = _pc.effective_tiers()[tier]["roles"] if tier in _pc.effective_tiers() \
        else None
    if roles is None:
        raise _pc.ProfileError(_pc.EFFECTIVE_PROFILE_SCHEMA_INVALID,
                               f"tier {tier!r} not in generated data")
    return {r: {"active": c["model"], "context": c["context"],
                "enabled": c["enabled"]} for r, c in roles.items()}


def select(profile: str = None, explicit=None) -> dict:
    """Resolve models. Selection MODE is recorded because a run of candidate
    tags on this machine is not a benchmark of a profile."""
    explicit = [t for t in dict.fromkeys(explicit or []) if t]
    gb = ram_gb()
    detected = tier_for_gb(gb)
    cfg = load_config()
    resolve = cfg.get("resolve", {})

    if profile is None and explicit:
        mode, tier, caps = EXPLICIT, None, {}
    else:
        tier = detected if profile in (None, "auto") else profile
        if tier not in TIERS:
            raise ValueError(f"unknown profile {tier!r}")
        caps = parse_profile(tier)
        mode = PROFILE_PLUS if explicit else PROFILE

    models = {}
    for cap, spec in caps.items():
        if not spec["enabled"] or cap == "embeddings":
            continue
        tag = resolve.get(spec["active"], spec["active"])
        # Deduplicate: profiles deliberately point several roles at one resident
        # model so Ollama keeps a single copy.
        e = models.setdefault(tag, {"capabilities": [], "in_profile": True,
                                    "profile_context": spec["context"]})
        e["capabilities"].append(cap)
    for tag in explicit:
        tag = resolve.get(tag, tag)
        models.setdefault(tag, {"capabilities": [], "in_profile": False,
                                "profile_context": 0})

    have = installed()
    for tag, e in models.items():
        e["installed"] = tag in have or f"{tag}:latest" in have
        e["digest"] = (have.get(tag, {}).get("digest") or "")[:12]
        e["size"] = int(have.get(tag, {}).get("size") or 0)
        e.update(model_info(tag) if e["installed"] else {"context_length": 0})
    return {"mode": mode, "ram_gb": gb, "detected_tier": detected,
            "tier": tier, "models": models}


def expected_wall_seconds_per_correct_sample(batch_wall: float, samples: int,
                                             success_rate: float):
    """Wall seconds spent per CORRECT solution.

    An earlier report divided the whole batch's wall time by the success rate,
    which yields "seconds to run 40 samples, inflated" — not a per-sample figure,
    and roughly 40x too large. It made qwen3.5:4b look like 127 s per correct
    answer when the true value is 3.18 s.

    Equivalent to batch_wall / (samples * success_rate), i.e. batch wall divided
    by the number of samples that actually passed.
    """
    if not samples or not success_rate:
        return None
    return batch_wall / (samples * success_rate)
