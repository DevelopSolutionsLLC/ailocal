#!/usr/bin/env python3
"""profile_config.py — the ONE profile parser and resolver.

config/profiles/<tier>.yaml is authoritative deployment configuration.
config/active-profile selects a tier and HAS NO IMPLICIT DEFAULT.

WHY THIS EXISTS. Profile parsing had grown four independent implementations --
sync-models.py's load_models_yaml(), benchmark.py's parse_profile(), doctor.sh's
sed extraction, and a `cat active-profile` in every shell entry point -- and they
did not agree. Worse, the shell readers all shared one shape:

    _TIER="$(cat config/active-profile 2>/dev/null || echo 64gb)"

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
REQUIRED_ROLE_FIELDS = ("role", "active", "context")

#: Optional, with their current behaviour when absent:
#:   reasoning      -> None; sync-models treats absent as non-thinking
#:   temperature/top_p/top_k/repeat_penalty -> None; omitted from generated params
#:   num_predict    -> None; omitted, backend default applies
#:   keep_alive     -> None; omitted, Ollama default applies
#:   persona        -> None; treated as no persona injection
#:   preferred/purpose/strengths/weaknesses -> [] (documentation only)
OPTIONAL_ROLE_FIELDS = ("reasoning", "temperature", "top_p", "top_k",
                        "repeat_penalty", "num_predict", "keep_alive",
                        "persona", "preferred", "purpose", "strengths",
                        "weaknesses")

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
            out.setdefault(current, {})
            continue
        m = _FIELD.match(line)
        if m:
            if current is None:
                raise ProfileError(PROFILE_YAML_INVALID,
                                   f"indented key before any section (line {n})")
            out[current][m.group(1)] = _coerce(m.group(2))
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
def resolve_active_tier(repo_root=None) -> str:
    """The active tier. NEVER defaults -- a missing marker is an error.

    An installation that silently picks a tier is an installation that can pull
    models the machine cannot hold."""
    marker = _root(repo_root) / "config" / "active-profile"
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
    path = _root(repo_root) / "config" / "profiles" / f"{tier}.yaml"
    if not path.exists():
        raise ProfileError(PROFILE_FILE_MISSING, str(path))
    data = parse_profile_text(path.read_text())
    present = [r for r in ROLES if r in data]
    if not present:
        raise ProfileError(PROFILE_SCHEMA_INVALID, "no capability roles found")
    for r in present:
        _validate_role(r, data[r])
    return data


def _validate_role(role: str, cfg) -> None:
    if not isinstance(cfg, dict):
        raise ProfileError(ROLE_CONFIG_INVALID, role)
    missing = [f for f in REQUIRED_ROLE_FIELDS if cfg.get(f) in (None, "")]
    if missing:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} missing {', '.join(missing)}")
    if not isinstance(cfg.get("context"), int):
        raise ProfileError(ROLE_CONFIG_INVALID, f"{role}.context is not an integer")


def resolve_role(tier: str, role: str, repo_root=None) -> dict:
    """One role's effective configuration.

    `enabled` mirrors the benchmark's existing rule: no backend, or an explicit
    disable, means the capability does not ship and must not be treated as
    though it did."""
    if role in NON_ROLE_SECTIONS:
        raise ProfileError(ROLE_MISSING, f"{role} is not a capability role")
    data = load_profile(tier, repo_root)
    if role not in data:
        raise ProfileError(ROLE_MISSING, role)
    cfg = data[role]
    active = str(cfg.get("active", ""))
    out = {"tier": tier, "role": role, "name": cfg.get("role"),
           "model": active, "active": active,
           "enabled": bool(active)
                      and active.lower() not in ("none", "false", "disabled"),
           "context": cfg.get("context")}
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
        "disk_gb": data.get("disk_gb"),
        "compaction": data.get("compaction", {}),
        "roles": {r: resolve_role(tier, r, repo_root)
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

SUPPORTED_SCHEMA_VERSIONS = (1,)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def load_effective(repo_root=None) -> dict:
    """The generated effective profile, validated against its own inputs.

    Staleness is DETECTED, not assumed away: the artifact records the hashes of
    the profile and the active marker it was generated from, so editing either
    without re-running sync is an error rather than a silently wrong runtime."""
    root = _root(repo_root)
    path = root / "config" / "effective-profile.json"
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

    marker = root / "config" / "active-profile"
    if not marker.exists():
        raise ProfileError(ACTIVE_PROFILE_MISSING, str(marker))
    if marker.read_text().strip() != data["tier"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER,
                           "active-profile no longer names the generated tier")
    if data.get("active_profile_sha256") and \
            _sha(marker) != data["active_profile_sha256"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_TIER, "marker changed")
    src = root / data["source_profile"]
    if data.get("source_profile_sha256") and _sha(src) != data["source_profile_sha256"]:
        raise ProfileError(EFFECTIVE_PROFILE_STALE_SOURCE,
                           "profile edited since generation — run `ailocal sync`")
    return data


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
