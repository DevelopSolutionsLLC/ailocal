#!/usr/bin/env python3
"""policy.py — the ONE policy reader and resolver.

profiles/<tier>.toml is authoritative deployment configuration; the
active-profile marker in $AILOCAL_STATE selects a tier and HAS NO IMPLICIT
DEFAULT. Policy is TOML because tomllib is in the standard library and rejects
duplicate keys and duplicate tables outright (ADR 010).

ONE READER, NO FALLBACK: this module fails closed, because a tier guessed on a
32 GB machine pulls models that do not fit.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

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

#: `context` was ambiguous — TOTAL window in one reader, INPUT budget in
#: another. It is an error, not a fallback: guessing which was meant is how both
#: interpretations survived side by side.
LEGACY_CONTEXT_FIELD = "context"

#: Optional, with their current behaviour when absent:
#:   reasoning      -> None; generation treats absent as non-thinking
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
PROFILE_INVALID = "PROFILE_INVALID"
PROFILE_SCHEMA_INVALID = "PROFILE_SCHEMA_INVALID"
ROLE_MISSING = "ROLE_MISSING"
ROLE_CONFIG_INVALID = "ROLE_CONFIG_INVALID"


class ProfileError(Exception):
    """Carries a CODE and the offending key, never file contents: an error
    string is the easiest place for configuration to leak into a pasted log."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ── public interface ────────────────────────────────────────────────────────
CLIENTS = ("claude", "codex", "continue", "compat")


# ── the three roots (ADR 009) ───────────────────────────────────────────────
# User-editable configuration, installed data assets and generated state have
# separate homes, XDG throughout with an AILOCAL_* override per root. These
# functions are the ONLY implementations of that resolution; nothing else may
# compute a root.

def _xdg(var: str, *fallback: str) -> Path:
    return Path(os.environ.get(var) or os.path.join(
        os.path.expanduser("~"), *fallback))


def state_root(override_path=None) -> Path:
    """Generated and machine-selected state, OUTSIDE the checkout.

    One artifact -- the rendered SearXNG settings -- carries the Brave API key,
    and living outside Git's tree makes committing it impossible rather than
    merely discouraged.
    """
    if override_path is not None:
        return Path(override_path)
    override = os.environ.get("AILOCAL_STATE")
    return Path(override) if override else _xdg(
        "XDG_STATE_HOME", ".local", "state") / "ailocal"


def config_root(repo_root=None) -> Path:
    """User-editable policy: profiles/, clients.toml, .env.

    ailocal never overwrites anything under this root without an explicit
    manifest-digest match proving the file is still exactly what was shipped.
    """
    if repo_root is not None:
        return Path(repo_root)
    override = os.environ.get("AILOCAL_CONFIG")
    return Path(override) if override else _xdg("XDG_CONFIG_HOME", ".config") / "ailocal"


def data_root(repo_root=None) -> Path:
    """Installed static assets: deploy/, clients/. Replaced wholesale on
    upgrade, so nothing user-authored may live here."""
    if repo_root is not None:
        return Path(repo_root)
    override = os.environ.get("AILOCAL_DATA")
    return Path(override) if override else _xdg("XDG_DATA_HOME", ".local", "share") / "ailocal"


def deployed_client_root() -> Path:
    """Where generated client configuration is installed for clients to read.

    The shell surfaces spell this path themselves, so changing it means changing
    them together."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "ailocal"


def benchmark_tooling_root() -> Path:
    """Third-party benchmark tooling: the lm-eval venv and the RULER checkout.

    XDG data rather than state: installed artifacts, not machine state."""
    return _xdg("XDG_DATA_HOME", ".local", "share") / "ailocal" / "benchmark"


def profiles_dir(repo_root=None) -> Path:
    return config_root(repo_root) / "profiles"


def profile_path(tier: str, repo_root=None) -> Path:
    return profiles_dir(repo_root) / f"{tier}.toml"


def active_profile_path(state=None) -> Path:
    """Where the selected tier is recorded. The only place this is spelled."""
    return state_root(state) / "active-profile"


def effective_profile_path(state=None) -> Path:
    """The generated, resolved profile every consumer reads."""
    return state_root(state) / "litellm" / "effective-profile.json"


def client_policy_path(repo_root=None) -> Path:
    return config_root(repo_root) / "profiles" / "clients.toml"


def _read_toml(path: Path, missing: str, invalid: str) -> dict:
    """Parse a policy file. tomllib rejects duplicate keys and duplicate tables,
    so a merge artefact cannot silently change the deployed model."""
    if not path.exists():
        raise ProfileError(missing, str(path))
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(invalid, str(e)) from None


def load_profile_file(path: Path) -> dict:
    """A profile document, unvalidated. load_profile() adds the schema checks."""
    return _read_toml(Path(path), PROFILE_FILE_MISSING, PROFILE_INVALID)


def load_client_policy(repo_root=None) -> dict:
    """Which capability each client surface uses. Fails closed like profiles."""
    data = _read_toml(client_policy_path(repo_root),
                      CLIENT_POLICY_MISSING, CLIENT_POLICY_INVALID)
    for section, mapping in data.items():
        if section not in CLIENTS:
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"unknown client {section!r}; expected one of "
                               f"{', '.join(CLIENTS)}")
        if not isinstance(mapping, dict):
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"{section!r} must be a table")
    # An UNQUOTED key containing a dot is a dotted key in TOML: `gpt-5.5 = "x"`
    # nests silently into {"gpt-5": {"5": "x"}} instead of naming the model.
    # Every compat entry maps one client-sent model ID to one capability.
    for name, target in (data.get("compat") or {}).items():
        if not isinstance(target, str):
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"compat.{name} must name one capability; quote a "
                               "model ID that contains a dot")
    return data


def slot_problems(tier=None, repo_root=None) -> list:
    """Client slot assignments that violate profile geometry.

    Returns (severity, message) pairs; "error" must fail generation, "warning"
    is a papercut. The rule lives here because it compares client policy against
    profile geometry, and policy owns both.
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
                    f"Conversational slots must not use it. Fix clients.toml."))

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


def resolve_active_tier(repo_root=None, state=None) -> str:
    """The active tier. NEVER defaults -- a missing marker is an error.

    An installation that silently picks a tier is an installation that can pull
    models the machine cannot hold."""
    marker = active_profile_path(state)
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
    data = _read_toml(path, PROFILE_FILE_MISSING, PROFILE_INVALID)
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
    # UNKNOWN FIELDS ARE ERRORS, NOT NOISE: only recognised keys are copied
    # out, so `temprature = 0.1` would otherwise read as set in review while the
    # role ran at the default.
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
        # admission uncomputable.
        raise ProfileError(ROLE_CONFIG_INVALID,
                           f"{role}.max_output must be a positive integer")



def geometry(context_input, max_output):
    """THE derivation. Every consumer calls this; none recomputes it.

        total_context     = context_input + max_output   -> Ollama num_ctx
        num_predict       = max_output                   -> backend ceiling
        max_input_tokens  = context_input                -> admission

    Admission equals context_input BY CONSTRUCTION, so there is no second place
    to get over-admission wrong. max_output is the only ceiling that binds:
    [REAL] a per-request max_tokens of 512 against an alias declaring
    num_predict 32768 returned 4,199 tokens (LiteLLM 1.93.0, ollama_chat).
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

    `enabled` is false when there is no backend or an explicit disable: the
    capability does not ship and must not be treated as though it did.

    `_data` lets a caller that has already parsed the profile pass it in, so
    profile_summary() parses the file once rather than once per role."""
    if role in NON_ROLE_SECTIONS:
        raise ProfileError(ROLE_MISSING, f"{role} is not a capability role")
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
    # `context` is an alias for total_context, read by benchmark cross-tier
    # planning and status. Derived, never configured.
    out["context"] = out["total_context"]
    for f in OPTIONAL_ROLE_FIELDS:
        out[f] = cfg.get(f)
    return out


def profile_summary(tier: str, repo_root=None) -> dict:
    """Serializable view for shells, doctor and status."""
    data = load_profile(tier, repo_root)
    return {
        "tier": tier,
        "status": data.get("status") or "unknown",
        "disk_gb": data.get("disk_gb"),
        "compaction": data.get("compaction", {}),
        "roles": {r: resolve_role(tier, r, repo_root, _data=data)
                  for r in ROLES if r in data},
    }


# ── runtime: read the GENERATED artifact, never the profile ─────────────────
# Everything after generation reads this. No consumer falls back to parsing a
# profile: that would resurrect a second parser and mask a stale generation
# instead of reporting it.
EFFECTIVE_PROFILE_MISSING = "EFFECTIVE_PROFILE_MISSING"
EFFECTIVE_PROFILE_STALE_TIER = "EFFECTIVE_PROFILE_STALE_TIER"
EFFECTIVE_PROFILE_STALE_SOURCE = "EFFECTIVE_PROFILE_STALE_SOURCE"
EFFECTIVE_PROFILE_HASH_INVALID = "EFFECTIVE_PROFILE_HASH_INVALID"
EFFECTIVE_PROFILE_SCHEMA_INVALID = "EFFECTIVE_PROFILE_SCHEMA_INVALID"

SUPPORTED_SCHEMA_VERSIONS = (2,)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def load_effective(repo_root=None, state=None) -> dict:
    """The generated effective profile, validated against its own inputs.

    Staleness is DETECTED: the artifact records the hashes of the profile and
    the marker it came from, so editing either without re-running sync is an
    error rather than a silently wrong runtime."""
    root = config_root(repo_root)
    path = effective_profile_path(state)
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

    marker = active_profile_path(state)
    if not marker.exists():
        raise ProfileError(ACTIVE_PROFILE_MISSING, str(marker))
    if marker.read_text().strip() != data["tier"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER,
                           "active-profile no longer names the generated tier")
    if data.get("active_profile_sha256") and \
            _sha(marker) != data["active_profile_sha256"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER, "marker changed")
    # EVERY normalized tier is checked, not just the active one: cross-tier
    # planning reads them all.
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
