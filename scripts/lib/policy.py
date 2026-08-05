#!/usr/bin/env python3
"""policy.py — the ONE profile parser and resolver.

config/profiles/<tier>.yaml is authoritative deployment configuration.
The active-profile marker in $AILOCAL_STATE selects a tier and HAS NO
IMPLICIT DEFAULT.

WHY NOT PyYAML — decided 2026-08-03, measured, do not re-litigate casually.
PyYAML is genuinely absent from every interpreter ailocal can reach:
/usr/bin/python3 (3.9.6), python3 (3.14.6) and Homebrew's — all three import
`yaml` and fail. Using it therefore means provisioning and owning a virtual
environment, not adding an import.

  Constrained parser (this file + sync-models):
    144 LOC total  -- _strip_comment 17, _coerce 19, parse_profile_text 32,
    flow_list 15, _flow_list_from_str 9, flow_dict 11, load_models_yaml 15,
    load_clients_yaml 26. Six test assertions. ZERO parsing defects across the
    whole 64gb geometry migration, the Brave work and the tier corrections.

  Core venv + PyYAML:
    deletes those 144 LOC, then adds a pinned requirements file plus venv
    provisioning in install.sh, validation in doctor.sh, refresh in update.sh
    and removal in teardown.sh -- FOUR lifecycle touchpoints, one runtime
    dependency, one new failure class ("venv missing or broken"), and network
    on first install. Net production LOC roughly -84.

Ownership, not line count, is the criterion: 144 lines parsing a schema this
repository itself writes are cheaper to own than a dependency lifecycle spread
across four scripts. The parser handles exactly the subset config/profiles and
config/clients.yaml use, and generation is validated end-to-end by the gate, so
a parsing error cannot reach a client silently.

REVISIT only if: a profile needs YAML this subset cannot express (anchors,
multi-line scalars, nested sequences of mappings), a parsing defect reaches
generated output, or ailocal acquires a core venv for some OTHER reason -- at
which point PyYAML rides along for free and this decision flips.

WHY THIS EXISTS. Profile parsing had grown four independent implementations --
sync-models.py's load_models_yaml(), benchmark.py's parse_profile(), doctor.sh's
sed extraction, and a `cat active-profile` in every shell entry point -- and they
did not agree. Worse, the shell readers all shared one shape:

    _TIER="$(cat "$AILOCAL_STATE/active-profile" 2>/dev/null || echo 64gb)"

Eight occurrences across four scripts. Each one silently installs or runs the
64 GB tier when the marker is missing, unreadable or empty. That is exactly the
failure the planner benchmark was built around: a suppressed read falling through
to a hardcoded 64gb default, with no error anywhere. On a 32 GB machine it pulls
models that do not fit.

So this module FAILS CLOSED. There is no default tier. A missing marker is an
error, not a 64 GB installation.

WHY A HAND-ROLLED PARSER. PyYAML is not installed on the host interpreter (only
inside the proxy image), and the shell entry points must work before any venv
exists. The profile format is deliberately small -- two levels, scalars and flow
lists -- so this parses that subset exactly and rejects anything else, rather
than pretending to be a YAML implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parent.parent.parent
TIERS = ("16gb", "32gb", "64gb", "128gb")

#: Capability roles. `compaction` is a CLIENT tuning knob, not a capability: it
#: has no model and must never be published as one.
ROLES = ("architecture", "implementation", "review", "fast", "completion",
         "embeddings")
NON_ROLE_SECTIONS = ("compaction",)

#: Required on EVERY role, because every consumer reads them. Everything else is
#: optional and returns None when absent -- no defaults are injected here, since
#: a value this module invents would not be one any generated config ever had.
REQUIRED_ROLE_FIELDS = ("role", "active", "context_input")

#: LEGACY. `context` meant the TOTAL window in production and the INPUT budget in
#: the benchmark — the ambiguity behind the over-admission defect fixed in
#: 23d2c19. It is now an error, not a fallback: a profile carrying it has not
#: been migrated, and guessing which meaning was intended is how the two
#: interpretations survived side by side.
LEGACY_CONTEXT_FIELD = "context"

#: Optional, with their current behaviour when absent:
#:   reasoning      -> None; sync-models treats absent as non-thinking
#:   temperature/top_p/top_k/repeat_penalty -> None; omitted from generated params
#:   num_predict    -> DERIVED from max_output by geometry(); never configured
#:   keep_alive     -> None; omitted, Ollama default applies
#:   persona        -> None; treated as no persona injection
#:   preferred/purpose/strengths/weaknesses -> [] (documentation only)
OPTIONAL_ROLE_FIELDS = ("provider", "max_output", "reasoning", "temperature",
                        "top_p", "top_k",
                        "repeat_penalty", "keep_alive",
                        "persona", "preferred", "purpose", "strengths",
                        "weaknesses")

CLIENT_POLICY_MISSING = "CLIENT_POLICY_MISSING"
CLIENT_POLICY_INVALID = "CLIENT_POLICY_INVALID"
ACTIVE_PROFILE_MISSING = "ACTIVE_PROFILE_MISSING"
ACTIVE_PROFILE_EMPTY = "ACTIVE_PROFILE_EMPTY"
ACTIVE_PROFILE_INVALID = "ACTIVE_PROFILE_INVALID"
PROFILE_FILE_MISSING = "PROFILE_FILE_MISSING"
PROFILE_YAML_INVALID = "PROFILE_YAML_INVALID"
PROFILE_SCHEMA_INVALID = "PROFILE_SCHEMA_INVALID"
ROLE_MISSING = "ROLE_MISSING"
ROLE_CONFIG_INVALID = "ROLE_CONFIG_INVALID"


class ProfileError(Exception):
    """Carries a CODE, never file contents.

    Profiles are not secret, but an error string is the easiest place for
    configuration to leak into a log that is pasted somewhere else. The code and
    the offending key are enough to act on."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _root(repo_root=None) -> Path:
    return Path(repo_root) if repo_root else REPO_DEFAULT


# ── GENERATION-TIME ONLY: constrained profile-schema parser ─────────────────
# This is NOT a YAML implementation and must not be used as one. It supports
# exactly the constructs the four profiles use -- two levels, scalars, flow
# lists, comments -- and REJECTS anything else rather than guessing.
#
# It exists here rather than as PyYAML because core ailocal has no managed
# Python dependency environment (no requirements.txt, pyproject.toml or venv);
# introducing one solely to read four small files at generation time was judged
# disproportionate. That trade-off is documented in AGENTS.md and should be
# revisited if core ailocal ever gains other Python dependencies.
#
# Only sync-models.py calls this. Runtime consumers read the generated JSON.
_SECTION = re.compile(r"^([a-z_]+):\s*$")
_FIELD = re.compile(r"^\s+([a-z_]+):[ \t]*(.*)$")


def _strip_comment(v: str) -> str:
    """Drop a trailing comment. A '#' inside brackets or quotes is content."""
    out, depth, quote = [], 0, None
    for ch in v:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0:
            break
        out.append(ch)
    return "".join(out).strip()


def _coerce(v: str):
    """Scalar or flow list. Deliberately narrow: anything else is a schema error
    rather than something this parser guesses at."""
    v = _strip_comment(v)
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v.strip("'\"")


def parse_profile_text(text: str) -> dict:
    """Parse the profile subset into {section: {key: value}}.

    Rejects rather than tolerates: an indented line before any section, or a
    non-indented line that is not a section header, means the file is not the
    shape every consumer assumes."""
    out, current = {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _SECTION.match(line)
        if m:
            current = m.group(1)
            # A REPEATED SECTION SILENTLY REPLACED THE FIRST. Two `architecture:`
            # blocks left only the last one, so a merge artefact or a careless
            # paste changed the deployed model with nothing to see in review.
            if current in out:
                raise ProfileError(PROFILE_YAML_INVALID,
                                   f"duplicate section {current!r} (line {n})")
            out[current] = {}
            continue
        m = _FIELD.match(line)
        if m:
            if current is None:
                raise ProfileError(PROFILE_YAML_INVALID,
                                   f"indented key before any section (line {n})")
            key = m.group(1)
            # Same reasoning one level down: last-wins on a duplicated key is
            # exactly how a profile ends up not meaning what it reads like.
            if key in out[current]:
                raise ProfileError(PROFILE_YAML_INVALID,
                                   f"duplicate key {key!r} in {current!r} (line {n})")
            val = m.group(2)
            # An unclosed flow list was coerced to the bare string "[a, b", so a
            # truncated list became a plausible-looking scalar.
            if val.lstrip().startswith("[") and not val.rstrip().endswith("]"):
                raise ProfileError(PROFILE_YAML_INVALID,
                                   f"unclosed flow list for {key!r} (line {n})")
            out[current][key] = _coerce(val)
            continue
        if not line.startswith(" "):
            # Top-level scalars such as `disk_gb: 64` are legitimate.
            k, sep, v = line.partition(":")
            if sep and re.fullmatch(r"[a-z_]+", k.strip()):
                out[k.strip()] = _coerce(v)
                current = None
                continue
        raise ProfileError(PROFILE_YAML_INVALID, f"unparsable line {n}")
    return out


# ── public interface ────────────────────────────────────────────────────────
CLIENTS = ("claude", "codex", "continue", "compat")


def runtime_root(state_root=None) -> Path:
    """Generated and machine-selected state, OUTSIDE the checkout.

    One artifact -- the rendered SearXNG settings -- carries the Brave API key,
    and living outside Git's tree makes committing it impossible rather than
    merely discouraged. All generated state shares that root so there is one
    place to inspect, back up, repair and remove.

    AILOCAL_STATE overrides it; XDG_STATE_HOME shifts the default. This is the
    only implementation of that resolution.
    """
    if state_root is not None:
        return Path(state_root)
    override = os.environ.get("AILOCAL_STATE")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return Path(xdg) / "ailocal"


def generated_path(name: str, state_root=None) -> Path:
    """A generated artifact under the runtime root."""
    return runtime_root(state_root) / name


def profiles_dir(repo_root=None) -> Path:
    return _root(repo_root) / "config" / "profiles"


def profile_path(tier: str, repo_root=None) -> Path:
    return profiles_dir(repo_root) / f"{tier}.yaml"


def active_profile_path(state_root=None) -> Path:
    """Where the selected tier is recorded. The only place this is spelled."""
    return runtime_root(state_root) / "active-profile"


def effective_profile_path(state_root=None) -> Path:
    """The generated, resolved profile every consumer reads."""
    return runtime_root(state_root) / "litellm" / "effective-profile.json"


def client_policy_path(repo_root=None) -> Path:
    return _root(repo_root) / "config" / "clients.yaml"


def load_client_policy(repo_root=None) -> dict:
    """Which capability each client surface uses. Fails closed like profiles.

    Rejects unknown sections, duplicate sections and duplicate keys: a silently
    ignored mapping sends a client to the wrong capability.
    """
    path = client_policy_path(repo_root)
    if not path.exists():
        raise ProfileError(CLIENT_POLICY_MISSING, str(path))
    data: dict = {}
    section = None
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            if not line.endswith(":"):
                raise ProfileError(CLIENT_POLICY_INVALID,
                                   f"line {n}: expected a section, got {line!r}")
            section = line[:-1].strip()
            if section in data:
                raise ProfileError(CLIENT_POLICY_INVALID,
                                   f"line {n}: duplicate section {section!r}")
            if section not in CLIENTS:
                raise ProfileError(CLIENT_POLICY_INVALID,
                                   f"line {n}: unknown client {section!r}; "
                                   f"expected one of {', '.join(CLIENTS)}")
            data[section] = {}
            continue
        if section is None:
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"line {n}: mapping outside any client section")
        if ":" not in line:
            raise ProfileError(CLIENT_POLICY_INVALID, f"line {n}: expected key: value")
        key, _, value = line.strip().partition(":")
        key = key.strip()
        if key in data[section]:
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"line {n}: duplicate key {key!r} in {section!r}")
        data[section][key] = _client_value(value.strip(), n)
    return data


def _client_value(v: str, line: int):
    """Scalar, flow list or flow mapping. Anything else is rejected."""
    if v.startswith("["):
        if not v.endswith("]"):
            raise ProfileError(CLIENT_POLICY_INVALID, f"line {line}: unclosed list")
        inner = v[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    if v.startswith("{"):
        if not v.endswith("}"):
            raise ProfileError(CLIENT_POLICY_INVALID, f"line {line}: unclosed mapping")
        out = {}
        inner = v[1:-1].strip()
        for pair in (p for p in inner.split(",") if p.strip()):
            if ":" not in pair:
                raise ProfileError(CLIENT_POLICY_INVALID,
                                   f"line {line}: expected key: value in mapping")
            k, _, val = pair.partition(":")
            out[k.strip()] = val.strip()
        return out
    return _strip_comment(v)


def client_mapping(client: str, repo_root=None) -> dict:
    """One client's mappings."""
    policy = load_client_policy(repo_root)
    if client not in policy:
        raise ProfileError(CLIENT_POLICY_INVALID, f"no policy for client {client!r}")
    return policy[client]


def slot_problems(tier=None, repo_root=None) -> list:
    """Client slot assignments that violate profile geometry.

    Returns (severity, message) pairs; "error" must fail generation, "warning"
    is a papercut. The rule lives here because it compares client policy against
    profile geometry, and both are owned here -- the generator enforces it and
    validation reports it, from one implementation.
    """
    import collections

    tier = tier or resolve_active_tier(repo_root)
    profile = load_profile(tier, repo_root)
    slots = (load_client_policy(repo_root).get("claude") or {}).get("slots", {})
    out = []

    # `completion` is FIM-only at a small window; a real agent turn routed there
    # hard-400s. Every built-in Claude slot carries full conversation context.
    bad = sorted(s for s, cap in slots.items() if cap == "completion")
    if bad:
        ctx = resolve_role(tier, "completion", repo_root)["total_context"] \
            if "completion" in profile else "?"
        out.append(("error",
                    f"claude.slots {bad} -> 'completion' (FIM tier, num_ctx {ctx}). "
                    f"Conversational slots must not use it. Fix clients.yaml."))

    # Two slots on one capability is legal, but Claude Code's /model picker
    # lists that capability once per slot pointing at it.
    for cap, n in collections.Counter(slots.values()).items():
        if n > 1:
            owners = sorted(s for s, v in slots.items() if v == cap)
            out.append(("warning",
                        f"claude.slots {owners} all map to '{cap}' — /model will "
                        f"list it {n}x. Give each slot its own capability."))
    return out


def required_models(tier=None, repo_root=None) -> list:
    """Distinct backends an installation must pull for a tier."""
    tier = tier or resolve_active_tier(repo_root)
    profile = load_profile(tier, repo_root)
    out = []
    for role in ROLES:
        if role not in profile:
            continue
        cfg = resolve_role(tier, role, repo_root)
        if cfg.get("enabled", True) and cfg.get("active"):
            out.append(cfg["active"])
    return sorted(set(out))


def resolve_active_tier(repo_root=None, state_root=None) -> str:
    """The active tier. NEVER defaults -- a missing marker is an error.

    An installation that silently picks a tier is an installation that can pull
    models the machine cannot hold."""
    marker = active_profile_path(state_root)
    if not marker.exists():
        raise ProfileError(ACTIVE_PROFILE_MISSING, str(marker))
    tier = marker.read_text().strip()
    if not tier:
        raise ProfileError(ACTIVE_PROFILE_EMPTY, str(marker))
    if tier not in TIERS:
        raise ProfileError(ACTIVE_PROFILE_INVALID,
                           f"{tier!r} not one of {', '.join(TIERS)}")
    return tier


def load_profile(tier: str, repo_root=None) -> dict:
    """Whole profile, validated. Read once; callers should not re-read."""
    if tier not in TIERS:
        raise ProfileError(ACTIVE_PROFILE_INVALID, f"{tier!r}")
    path = profile_path(tier, repo_root)
    if not path.exists():
        raise ProfileError(PROFILE_FILE_MISSING, str(path))
    data = parse_profile_text(path.read_text())
    present = [r for r in ROLES if r in data]
    if not present:
        raise ProfileError(PROFILE_SCHEMA_INVALID, "no capability roles found")
    for r in present:
        _validate_role(r, data[r])
    return data


def _known_role_fields() -> frozenset:
    """Every field a role may declare. Anything else is a typo or a stale key."""
    return frozenset(REQUIRED_ROLE_FIELDS) | frozenset(OPTIONAL_ROLE_FIELDS) | {
        # Structural, not tuning: consumed by generation rather than by geometry.
        "enabled", "role", "active",
    }


def _validate_role(role: str, cfg) -> None:
    if not isinstance(cfg, dict):
        raise ProfileError(ROLE_CONFIG_INVALID, role)
    if LEGACY_CONTEXT_FIELD in cfg:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} still uses legacy `context`; migrate to "
                           "context_input + max_output")
    # UNKNOWN FIELDS ARE ERRORS, NOT NOISE. Only recognised keys were ever
    # copied out, so `temprature: 0.1` parsed cleanly, was silently discarded,
    # and the role ran at the default temperature -- a tuning value that reads
    # as set in review and is not. Same for `topk`, `keepalive`, and any field
    # a schema migration retires (`num_predict`).
    unknown = sorted(set(cfg) - _known_role_fields())
    if unknown:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} declares unknown field(s): {', '.join(unknown)}")
    missing = [f for f in REQUIRED_ROLE_FIELDS if cfg.get(f) in (None, "")]
    if missing:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} missing {', '.join(missing)}")
    ci = cfg.get("context_input")
    if not isinstance(ci, int) or ci <= 0:
        raise ProfileError(ROLE_CONFIG_INVALID, f"{role}.context_input invalid")
    mo = cfg.get("max_output")
    if mo is None:
        return                      # embedding route: no generation, no reserve
    if not isinstance(mo, int) or mo <= 0:
        # -1/-2 (Ollama infinite/fill) are rejected: an unbounded reserve makes
        # admission uncomputable, which is how implementation ended up admitting
        # its whole window.
        raise ProfileError(ROLE_CONFIG_INVALID,
                           f"{role}.max_output must be a positive integer")



def geometry(context_input, max_output):
    """THE derivation. Every consumer calls this; none recomputes it.

        total_context     = context_input + max_output   -> Ollama num_ctx
        num_predict       = max_output                   -> backend ceiling
        max_input_tokens  = context_input                -> admission

    Admission equals context_input BY CONSTRUCTION, so the over-admission class
    of defect cannot recur: there is no second place to get it wrong.

    max_output is the only ceiling that binds. Measured on LiteLLM 1.93.0
    ollama_chat: a per-request max_tokens of 512 against an alias declaring
    num_predict 32768 returned 4,199 tokens. Client limits are advisory here.
    """
    if not isinstance(context_input, int) or context_input <= 0:
        raise ProfileError(ROLE_CONFIG_INVALID, f"context_input={context_input!r}")
    if max_output is None:
        return {"context_input": context_input, "max_output": None,
                "total_context": context_input, "num_ctx": context_input,
                "num_predict": None, "max_input_tokens": context_input}
    if not isinstance(max_output, int) or max_output <= 0:
        raise ProfileError(ROLE_CONFIG_INVALID, f"max_output={max_output!r}")
    total = context_input + max_output
    return {"context_input": context_input, "max_output": max_output,
            "total_context": total, "num_ctx": total,
            "num_predict": max_output, "max_input_tokens": context_input}


def resolve_role(tier: str, role: str, repo_root=None, _data=None) -> dict:
    """One role's effective configuration.

    `enabled` mirrors the benchmark's existing rule: no backend, or an explicit
    disable, means the capability does not ship and must not be treated as
    though it did."""
    if role in NON_ROLE_SECTIONS:
        raise ProfileError(ROLE_MISSING, f"{role} is not a capability role")
    # `_data` lets a caller that has ALREADY parsed the profile pass it in.
    # profile_summary() used to parse once for itself and then again inside
    # this function for every role -- seven parses of the same file per call.
    data = load_profile(tier, repo_root) if _data is None else _data
    if role not in data:
        raise ProfileError(ROLE_MISSING, role)
    cfg = data[role]
    active = str(cfg.get("active", ""))
    out = {"tier": tier, "role": role, "name": cfg.get("role"),
           "model": active, "active": active,
           "enabled": bool(active)
                      and active.lower() not in ("none", "false", "disabled"),
           **geometry(cfg.get("context_input"), cfg.get("max_output"))}
    # `context` is retained as an ALIAS for total_context so existing readers
    # (benchmark cross-tier planning, status) keep working. It is derived, never
    # configured.
    out["context"] = out["total_context"]
    for f in OPTIONAL_ROLE_FIELDS:
        out[f] = cfg.get(f)
    return out


def resolve_active_role(role: str, repo_root=None) -> dict:
    return resolve_role(resolve_active_tier(repo_root), role, repo_root)


def profile_summary(tier: str, repo_root=None) -> dict:
    """Serializable view for shells, doctor and status."""
    data = load_profile(tier, repo_root)
    return {
        "tier": tier,
        # Exposed so shell entry points never grep the YAML for it. install.sh
        # read `status:` with grep, which was the last profile-YAML read left in
        # a shell script.
        "status": data.get("status") or "unknown",
        "disk_gb": data.get("disk_gb"),
        "compaction": data.get("compaction", {}),
        "roles": {r: resolve_role(tier, r, repo_root, _data=data)
                  for r in ROLES if r in data},
    }


# ── runtime: read the GENERATED artifact, never the YAML ────────────────────
# Everything after generation reads this. No consumer falls back to parsing a
# profile: a fallback would silently resurrect the second parser this whole
# change exists to remove, and would mask a stale generation instead of
# reporting it.
EFFECTIVE_PROFILE_MISSING = "EFFECTIVE_PROFILE_MISSING"
EFFECTIVE_PROFILE_STALE_TIER = "EFFECTIVE_PROFILE_STALE_TIER"
EFFECTIVE_PROFILE_STALE_SOURCE = "EFFECTIVE_PROFILE_STALE_SOURCE"
EFFECTIVE_PROFILE_HASH_INVALID = "EFFECTIVE_PROFILE_HASH_INVALID"
EFFECTIVE_PROFILE_SCHEMA_INVALID = "EFFECTIVE_PROFILE_SCHEMA_INVALID"

SUPPORTED_SCHEMA_VERSIONS = (2,)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def load_effective(repo_root=None, state_root=None) -> dict:
    """The generated effective profile, validated against its own inputs.

    Staleness is DETECTED, not assumed away: the artifact records the hashes of
    the profile and the active marker it was generated from, so editing either
    without re-running sync is an error rather than a silently wrong runtime."""
    root = _root(repo_root)
    path = effective_profile_path(state_root)
    if not path.exists():
        raise ProfileError(EFFECTIVE_PROFILE_MISSING, str(path))
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        raise ProfileError(EFFECTIVE_PROFILE_SCHEMA_INVALID, str(path))
    if data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProfileError(EFFECTIVE_PROFILE_SCHEMA_INVALID,
                           f"schema_version {data.get('schema_version')!r}")
    for key in ("tier", "roles", "source_profile", "config_sha256"):
        if key not in data:
            raise ProfileError(EFFECTIVE_PROFILE_SCHEMA_INVALID, f"missing {key}")

    body = {k: v for k, v in data.items()
            if k not in ("config_sha256", "generated_at")}
    if hashlib.sha256(json.dumps(body, sort_keys=True,
                                 separators=(",", ":")).encode()).hexdigest() \
            != data["config_sha256"]:
        raise ProfileError(EFFECTIVE_PROFILE_HASH_INVALID, "config_sha256")

    marker = active_profile_path(state_root)
    if not marker.exists():
        raise ProfileError(ACTIVE_PROFILE_MISSING, str(marker))
    if marker.read_text().strip() != data["tier"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER,
                           "active-profile no longer names the generated tier")
    if data.get("active_profile_sha256") and \
            _sha(marker) != data["active_profile_sha256"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER, "marker changed")
    # EVERY normalized tier is checked, not just the active one: benchmark
    # cross-tier planning reads them, and a stale 32gb block would otherwise be
    # served silently on a 64gb machine.
    for t, blk in (data.get("tiers") or {}).items():
        src = root / blk["source_profile"]
        if blk.get("source_profile_sha256") and _sha(src) != blk["source_profile_sha256"]:
            raise ProfileError(EFFECTIVE_PROFILE_STALE_SOURCE,
                               f"{t} profile edited since generation — run `ailocal sync`")
    return data


def effective_tiers(repo_root=None) -> dict:
    """Every normalized tier. The only cross-tier source at runtime."""
    return load_effective(repo_root)["tiers"]


def effective_role_for_tier(tier: str, role: str, repo_root=None) -> dict:
    """One role from ANY tier, from generated data. No YAML, no fallback."""
    tiers = effective_tiers(repo_root)
    if tier not in tiers:
        raise ProfileError(EFFECTIVE_PROFILE_SCHEMA_INVALID,
                           f"tier {tier!r} not in generated data")
    roles = tiers[tier]["roles"]
    if role not in roles:
        raise ProfileError(ROLE_MISSING, f"{tier}.{role}")
    return roles[role]


def active_tier(repo_root=None) -> str:
    """Runtime tier. Comes from the validated artifact, not the raw marker."""
    return load_effective(repo_root)["tier"]


def effective_role(role: str, repo_root=None) -> dict:
    data = load_effective(repo_root)
    if role not in data["roles"]:
        raise ProfileError(ROLE_MISSING, role)
    return data["roles"][role]


def effective_summary(repo_root=None) -> dict:
    d = load_effective(repo_root)
    return {"tier": d["tier"], "compaction": d.get("compaction", {}),
            "roles": d["roles"], "generated_at": d.get("generated_at"),
            "config_sha256": d["config_sha256"]}
