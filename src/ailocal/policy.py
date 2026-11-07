#!/usr/bin/env python3
"""Read and resolve policy: the one reader, failing closed.

profiles/<tier>.toml is authoritative; the active-profile marker selects a tier
with no implicit default, since a guessed tier pulls models that do not fit.
TOML because tomllib rejects duplicate keys and tables (ADR 010).
"""
from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

TIERS = ("16gb", "32gb", "64gb", "128gb")


def ollama_model_store() -> Path:
    """Return the model store; daemon and importer must share it."""
    return Path(os.environ.get("OLLAMA_MODELS", "/Users/Shared/ollama/models"))


def ollama_runtime_root() -> Path:
    """Return the binary root; it survives deletion of generated state."""
    return (Path(os.environ["AILOCAL_RUNTIMES"]) if os.environ.get("AILOCAL_RUNTIMES")
            else _xdg("XDG_DATA_HOME", ".local", "share") / "ailocal" / "runtimes")


def ollama_patch_spec() -> dict:
    """Return the reviewed source and patch declaration shipped with ailocal."""
    root = data_root() / "deploy" / "ollama"
    with (root / "patch.toml").open("rb") as stream:
        spec = tomllib.load(stream)
    spec["patch_file"] = root / spec["patch_file"]
    spec["security_patch_file"] = root / spec["security_patch_file"]
    spec["router_patch_file"] = root / spec["router_patch_file"]
    spec["stop_patch_file"] = root / spec["stop_patch_file"]
    spec["qwen35_patch_file"] = root / spec["qwen35_patch_file"]
    return spec


def ollama_patch_selection() -> str:
    from . import environment
    return environment.value("AILOCAL_OLLAMA_PATCH")


def stock_ollama_binary() -> str:
    """Return `ollama` on PATH unresolved; the cask realpath dies on upgrade."""
    binary = shutil.which("ollama")
    if not binary:
        raise SystemExit("Ollama CLI not found on PATH; brew install --cask ollama-binary")
    return binary


def ollama_binary() -> str:
    """Return the one binary for the native CLI and its managed service."""
    selected = ollama_patch_selection()
    if not selected:
        return stock_ollama_binary()
    spec = ollama_patch_spec()
    if selected != spec["id"]:
        raise SystemExit("Unknown AILOCAL_OLLAMA_PATCH; no alternate runtime was selected.")
    binary = ollama_runtime_root() / spec["version"] / "ollama"
    from .ollama_patch import verify
    if not verify(binary):
        raise SystemExit("Selected patched Ollama is absent or modified. "
                         "Run ailocal install to rebuild it; no alternate runtime was selected.")
    return str(binary)


def client_mcp_path(client: str, *, local: bool) -> Path:
    """Return native user-scope MCP paths, honouring hosted client overrides."""
    if client == "copilot" and not local:
        return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "mcp-config.json"
    if client not in ("claude", "codex"):
        raise ValueError(f"no native MCP location for {client}")
    if local:
        root = deployed_client_root() / client
    elif client == "codex":
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    elif os.environ.get("CLAUDE_CONFIG_DIR"):
        root = Path(os.environ["CLAUDE_CONFIG_DIR"])
    else:
        return Path.home() / ".claude.json"
    return root / ("config.toml" if client == "codex" else ".claude.json")

#: Capability roles in client-surface order. Embeddings belong to the indexer.
#: `fast` = primary w/o thinking, declared only where primary thinks (ADR 018).
ROLES = ("primary", "fast", "cyber", "completion")

#: Omittable roles → stand-in; resolved only by resolve_capability().
OPTIONAL_ROLES = {"fast": "primary"}

#: Roles retired in 0.16 → successor; LiteLLM aliases only, removed in 0.17.
RETIRED_ROLES = {"architecture": "primary", "implementation": "primary", "review": "primary"}

#: Fail closed: a leftover holds operator edits `[primary]` would ignore (#89).
RETIRED_ROLE_SECTIONS = tuple(RETIRED_ROLES)
#: Non-capability sections. Retired ones stay valid: installed user profiles
#: keep them and are never rewritten, so inert must not mean invalid.
NON_ROLE_SECTIONS = ("compaction", "embeddings", "model_guidance")

#: Retired fields, accepted and ignored: unknown fields are errors, and
#: installed user profiles still carry these.
RETIRED_ROLE_FIELDS = ("persona",)

#: Required on every role; all else is None when absent, never defaulted.
REQUIRED_ROLE_FIELDS = ("role", "active", "context_input")

#: Selection names: `<family>:<size>[-<variant>…]`, variants in this order,
#: each once. No quantization token: the source hash pins it.
SELECTION_VARIANTS = ("unrestricted", "calibrated", "it", "base", "qat", "mtp")
SELECTION_NAME = re.compile(r"^(?P<family>[a-z0-9][a-z0-9.-]*):(?P<size>\d+(?:\.\d+)?[bm]|e\d+b)"
                            r"(?P<variants>(?:-[a-z0-9_]+)*)$")


def selection_name_error(name) -> str | None:
    """Return why `name` is not a structured selection name, or None."""
    m = SELECTION_NAME.fullmatch(str(name)) if isinstance(name, str) else None
    if m is None:
        return (f"{name!r} is not <family>:<size>[-variant…] (lowercase family, size such as "
                "4b, 1.5b, 270m or e4b; never :latest or a repository URL)")
    tokens = [t for t in m.group("variants").split("-") if t]
    unknown = [t for t in tokens if t not in SELECTION_VARIANTS]
    if unknown:
        return (f"{name!r} carries {', '.join(unknown)}: the variant tokens are "
                f"{', '.join(SELECTION_VARIANTS)}; a quantization is pinned by the source hash, never named")
    order = [SELECTION_VARIANTS.index(t) for t in tokens]
    if order != sorted(set(order)):
        return f"{name!r}: variant tokens must appear once each, in the order {'-'.join(SELECTION_VARIANTS)}"
    return None


#: Ambiguous (total vs input) → an error, never guessed.
LEGACY_CONTEXT_FIELD = "context"

#: Optional, with their current behaviour when absent:
#:   reasoning      -> None; generation treats absent as non-thinking
#:   temperature/top_p/top_k/repeat_penalty -> None; omitted from params
#:   num_predict    -> DERIVED from max_output by geometry(); never configured
#:   keep_alive     -> None; omitted, Ollama default applies
#:   preferred/purpose/strengths/weaknesses -> [] (documentation only)
OPTIONAL_ROLE_FIELDS = ("provider", "source", "runtime_patch", "stop", "max_output", "reasoning", "temperature",
                        "top_p", "top_k", "min_p", "presence_penalty",
                        "repeat_penalty", "keep_alive", "draft_num_predict",
                        "preferred", "purpose", "strengths",
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
    """Policy error carrying a code and key, never file contents (log leaks)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


CLIENTS = ("claude", "codex", "compat")


# Config, data and state roots: XDG + AILOCAL_* overrides; resolved only here.

def _xdg(var: str, *fallback: str) -> Path:
    return Path(os.environ.get(var) or os.path.join(
        os.path.expanduser("~"), *fallback))


def ollama_url() -> str:
    """Return the Ollama base URL from OLLAMA_HOST, always with a scheme.

    OLLAMA_HOST's documented form is bare `host:port`, which urllib misparses
    as a scheme; normalised here so there is one reading.
    """
    raw = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return "http://127.0.0.1:11434"
    return raw if "://" in raw else f"http://{raw}"


def state_root(override_path=None) -> Path:
    """Return the generated-state root, outside the checkout.

    Rendered SearXNG settings can hold a token; outside Git it cannot be
    committed.
    """
    if override_path is not None:
        return Path(override_path)
    override = os.environ.get("AILOCAL_STATE")
    return Path(override) if override else _xdg(
        "XDG_STATE_HOME", ".local", "state") / "ailocal"


def config_root(repo_root=None) -> Path:
    """Return the user policy root; overwritten only on shipped-digest match."""
    if repo_root is not None:
        return Path(repo_root)
    override = os.environ.get("AILOCAL_CONFIG")
    return Path(override) if override else _xdg("XDG_CONFIG_HOME", ".config") / "ailocal"


def data_root(repo_root=None) -> Path:
    """Return the package's own resources (deploy/, clients/), never a copy.

    Compose bind-mounts deploy/; a copy could change under a live container.
    """
    if repo_root is not None:
        return Path(repo_root)
    override = os.environ.get("AILOCAL_DATA")
    if override:
        return Path(override)
    import importlib.resources
    return Path(str(importlib.resources.files("ailocal"))) / "resources"


def deployed_client_root() -> Path:
    """Return where generated client config is written.

    Not config_root(): authored policy and disposable output differ in owner and
    lifecycle, and overriding the policy root must not relocate output.
    """
    override = os.environ.get("AILOCAL_CLIENTS")
    return Path(override) if override else _xdg(
        "XDG_CONFIG_HOME", ".config") / "ailocal"


def profiles_dir(repo_root=None) -> Path:
    return config_root(repo_root) / "profiles"


def profile_path(tier: str, repo_root=None) -> Path:
    return profiles_dir(repo_root) / f"{tier}.toml"


def active_profile_path(state=None) -> Path:
    """Return the path recording the selected tier; spelled only here."""
    return state_root(state) / "active-profile"


def client_policy_path(repo_root=None) -> Path:
    return config_root(repo_root) / "profiles" / "clients.toml"


def _read_toml(path: Path, missing: str, invalid: str) -> dict:
    """Parse a policy file; duplicate keys or tables fail, never merge."""
    if not path.exists():
        raise ProfileError(missing, str(path))
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(invalid, str(e)) from None


def load_profile_file(path: Path) -> dict:
    """Load a profile unvalidated; load_profile() adds schema checks."""
    data = _read_toml(Path(path), PROFILE_FILE_MISSING, PROFILE_INVALID)
    _reject_retired_sections(data)
    _validate_guidance(data)
    return data


def _reject_retired_sections(data: dict) -> None:
    retired = [r for r in RETIRED_ROLE_SECTIONS if r in data]
    if retired:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"[{'], ['.join(retired)}] retired in 0.16: the chat roles are "
                           "[primary] and, where the primary thinks, [fast]. `ailocal install` "
                           "migrates an edited profile (#89)")


def load_client_policy(repo_root=None) -> dict:
    """Load per-client capability policy; fails closed like profiles."""
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
    # Unquoted `gpt-5.5 = "x"` is a dotted key and nests; each entry must map
    # one model ID to one capability.
    for name, target in (data.get("compat") or {}).items():
        if not isinstance(target, str):
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"compat.{name} must name one capability; quote a "
                               "model ID that contains a dot")
    return data


#: Delegate modes, weakest first; `full` lifts the worker's sandbox and prompts.
DELEGATE_MODES = ("text", "research", "edit", "full")
DELEGATE_DEFAULT_CEILING = "edit"


def delegate_ceilings(repo_root=None) -> dict:
    """Return the strongest mode `delegate` may grant each worker CLI.

    Absent `delegate_max_mode` keeps the sandboxed default; invalid fails.
    """
    data = load_client_policy(repo_root)
    out = {}
    for client in ("claude", "codex"):
        mode = (data.get(client) or {}).get("delegate_max_mode", DELEGATE_DEFAULT_CEILING)
        if mode not in DELEGATE_MODES:
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"{client}.delegate_max_mode must be one of "
                               f"{', '.join(DELEGATE_MODES)}")
        out[client] = mode
    return out


#: Max local instruction file; 17 KB derailed a 9B model in 2/6 runs (#39).
INSTRUCTION_DEFAULT_MAX_BYTES = 4096


def instruction_budgets(repo_root=None, tier=None) -> dict:
    """Return per-client instruction-file budgets (`instruction_max_bytes`).

    Published for co-installed composers. Absent → measured default; invalid
    fails closed. `_by_tier` overrides apply only when `tier` is given (#47).
    """
    data = load_client_policy(repo_root)
    out = {}
    for client in ("claude", "codex"):
        mapping = data.get(client) or {}
        value = mapping.get("instruction_max_bytes", INSTRUCTION_DEFAULT_MAX_BYTES)
        by_tier = mapping.get("instruction_max_bytes_by_tier", {})
        if not isinstance(by_tier, dict) or not set(by_tier) <= set(TIERS):
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"{client}.instruction_max_bytes_by_tier must map tiers "
                               f"({', '.join(TIERS)}) to byte counts")
        for v in (value, *by_tier.values()):
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ProfileError(CLIENT_POLICY_INVALID,
                                   f"{client}.instruction_max_bytes must be a positive integer")
        out[client] = by_tier.get(tier, value)
    return out


def client_capability_targets(repo_root=None) -> dict:
    """Return model-routing targets per client; worker modes are excluded."""
    data = load_client_policy(repo_root)
    delegate_ceilings(repo_root)
    instruction_budgets(repo_root)
    out = {}
    for client, mapping in data.items():
        if client == "compat":
            values = list(mapping.values())
        elif client == "claude":
            slots = mapping.get("slots", {})
            if not isinstance(slots, dict):
                raise ProfileError(CLIENT_POLICY_INVALID, "claude.slots must be a table")
            values = list(slots.values())
            if "launch_default" in mapping:
                values.append(mapping["launch_default"])
        else:
            values = [mapping["default"]] if "default" in mapping else []
        if any(not isinstance(value, str) for value in values):
            raise ProfileError(CLIENT_POLICY_INVALID,
                               f"{client} routing targets must name capabilities")
        out[client] = set(values)
    return out


def resolve_capability(target: str, roles) -> str:
    """Return the enabled role a client target names.

    The one rule for absent optional roles; other targets pass through unchanged
    so the consumer rejects unknown ones rather than this guessing.
    """
    if target in roles:
        return target
    stand_in = OPTIONAL_ROLES.get(target)
    return stand_in if stand_in is not None and stand_in in roles else target


def deprecated_aliases(roles) -> dict:
    """Return retired or omitted role names still served → their target role."""
    out = {old: new for old, new in RETIRED_ROLES.items() if new in roles}
    out.update({opt: stand for opt, stand in OPTIONAL_ROLES.items()
                if opt not in roles and stand in roles})
    return out


def enabled_roles(tier: str, repo_root=None, _data=None) -> tuple:
    """Return the roles a tier serves, in ROLES order."""
    data = load_profile(tier, repo_root) if _data is None else _data
    return tuple(r for r in ROLES if r in data
                 and resolve_role(tier, r, repo_root, _data=data)["enabled"])


def effective_capability(target: str, repo_root=None) -> str:
    """Resolve a capability against the active tier."""
    return resolve_capability(target, enabled_roles(resolve_active_tier(repo_root), repo_root))


def slot_problems(tier=None, repo_root=None) -> list:
    """Return client slot assignments that violate profile geometry.

    Returns:
        (severity, message) pairs; "error" must fail generation. Several slots
        on one capability is by design, not a problem.
    """
    tier = tier or resolve_active_tier(repo_root)
    profile = load_profile(tier, repo_root)
    roles = enabled_roles(tier, repo_root, _data=profile)
    slots = (load_client_policy(repo_root).get("claude") or {}).get("slots", {})
    out = []

    # `completion` is FIM-only, small window: an agent turn there hard-400s.
    bad = sorted(s for s, cap in slots.items() if resolve_capability(cap, roles) == "completion")
    if bad:
        ctx = resolve_role(tier, "completion", repo_root)["total_context"] \
            if "completion" in profile else "?"
        out.append(("error",
                    f"claude.slots {bad} -> 'completion' (FIM tier, num_ctx {ctx}). "
                    f"Conversational slots must not use it. Fix clients.toml."))
    return out


def resolve_active_tier(repo_root=None, state=None) -> str:
    """Return the active tier; never defaults, a missing marker is an error."""
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
    """Load and validate the whole profile; callers should not re-read."""
    if tier not in TIERS:
        raise ProfileError(ACTIVE_PROFILE_INVALID, f"{tier!r}")
    path = profile_path(tier, repo_root)
    if not path.exists():
        raise ProfileError(PROFILE_FILE_MISSING, str(path))
    data = _read_toml(path, PROFILE_FILE_MISSING, PROFILE_INVALID)
    _reject_retired_sections(data)
    present = [r for r in ROLES if r in data]
    if not present:
        raise ProfileError(PROFILE_SCHEMA_INVALID, "no capability roles found")
    for r in present:
        _validate_role(r, data[r])
    _validate_guidance(data)
    return data


def _validate_guidance(data: dict) -> None:
    guides = data.get("model_guidance", {})
    if not isinstance(guides, dict):
        raise ProfileError(PROFILE_SCHEMA_INVALID, "model_guidance must be a table")
    for model, guide in guides.items():
        why = selection_name_error(model)
        if why:
            raise ProfileError(PROFILE_SCHEMA_INVALID, f"model_guidance key: {why}")
        required = {"summary", "prompting", "limitations", "evidence", "sources"}
        if not isinstance(guide, dict) or not required <= set(guide) <= required | {"name", "input_modalities"}:
            raise ProfileError(PROFILE_SCHEMA_INVALID, "invalid model_guidance fields")
        if guide.get("input_modalities", ["text"]) not in (["text"], ["text", "image"]):
            raise ProfileError(PROFILE_SCHEMA_INVALID, "model_guidance.input_modalities must be text or text/image")
        if "name" in guide and (not isinstance(guide["name"], str) or not guide["name"].strip()):
            raise ProfileError(PROFILE_SCHEMA_INVALID, "model_guidance.name must be text")
        for field in ("summary", "evidence"):
            if not isinstance(guide[field], str) or not guide[field].strip():
                raise ProfileError(PROFILE_SCHEMA_INVALID, f"model_guidance.{field} must be text")
        for field in ("prompting", "limitations", "sources"):
            if not isinstance(guide[field], list) or not all(
                    isinstance(s, str) and s.strip() for s in guide[field]):
                raise ProfileError(PROFILE_SCHEMA_INVALID, f"model_guidance.{field} must be a text list")
        if any(not s.startswith("https://") for s in guide["sources"]):
            raise ProfileError(PROFILE_SCHEMA_INVALID, "model_guidance sources require HTTPS")


def capability_description(section: dict, guide: dict) -> str:
    """Return `<role> — <model name>, <N>K ctx` for every client surface."""
    model = guide.get("name") or str(section.get("active", ""))
    total = geometry(section.get("context_input"), section.get("max_output"))["total_context"]
    return f"{section.get('role')} — {model}, {total // 1024}K ctx"


def guidance_for(data: dict, role: str) -> dict:
    """Return guidance for an exact backend match; never another model's."""
    return data.get("model_guidance", {}).get(data[role]["active"], {
        "summary": "No model-specific guidance recorded for this backend.",
        "prompting": [], "limitations": [], "sources": [],
        "evidence": "[UNVERIFIED] No prompting evaluation recorded.",
    })


def _known_role_fields() -> frozenset:
    """Return every field a role may declare; others are typos or stale."""
    return frozenset(REQUIRED_ROLE_FIELDS) | frozenset(OPTIONAL_ROLE_FIELDS) | {
        # Structural, consumed by generation rather than geometry.
        "enabled", "role", "active",
    }


def _validate_role(role: str, cfg) -> None:
    if not isinstance(cfg, dict):
        raise ProfileError(ROLE_CONFIG_INVALID, role)
    if LEGACY_CONTEXT_FIELD in cfg:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} still uses legacy `context`; migrate to "
                           "context_input + max_output")
    # Unknown fields are errors: a dropped typo would read as set in review.
    unknown = sorted(set(cfg) - _known_role_fields() - set(RETIRED_ROLE_FIELDS))
    if unknown:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} declares unknown field(s): {', '.join(unknown)}")
    missing = [f for f in REQUIRED_ROLE_FIELDS if cfg.get(f) in (None, "")]
    if missing:
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role} missing {', '.join(missing)}")
    preferred = cfg.get("preferred")
    if preferred is not None and (not isinstance(preferred, list) or not all(isinstance(p, str) for p in preferred)):
        raise ProfileError(PROFILE_SCHEMA_INVALID, f"{role}.preferred must be a string list")
    for field, name in (("active", cfg["active"]), *(("preferred", p) for p in preferred or [])):
        why = selection_name_error(name)
        if why:
            raise ProfileError(PROFILE_SCHEMA_INVALID, f"{role}.{field}: {why}")
    source = cfg.get("source")
    draft = cfg.get("draft_num_predict")
    if draft is not None and (type(draft) is not int or draft < 0):
        raise ProfileError(PROFILE_SCHEMA_INVALID,
                           f"{role}.draft_num_predict must be a nonnegative integer")
    if cfg.get("runtime_patch") is not None and cfg["runtime_patch"] != ollama_patch_spec()["id"]:
        raise ProfileError(PROFILE_SCHEMA_INVALID, f"{role}.runtime_patch is unsupported")
    if cfg.get("stop") is not None and (
            not isinstance(cfg["stop"], list) or not cfg["stop"]
            or not all(isinstance(token, str) and token for token in cfg["stop"])):
        raise ProfileError(PROFILE_SCHEMA_INVALID, f"{role}.stop must be a nonempty string list")
    if source is not None:
        snapshot = (isinstance(source, dict) and set(source) == {"repository", "revision"}
                    and re.fullmatch(r"[\w.-]+/[\w.-]+", str(source.get("repository", "")))
                    and re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))))
        native_keys = {"model", "sha256", "renderer", "parser"}
        draft_keys = {"draft_model", "draft_sha256"}
        projector_keys = {"projector_model", "projector_sha256"}
        native_ref = r"(?:hf\.co/[\w.-]+/[\w.-]+|(?:[\w-]+/)?[\w.-]+):[\w.-]+"
        # Draft head / projector: pulled tag + weight SHA-256, both or neither.
        native = (isinstance(source, dict)
                  and set(source) in (native_keys, native_keys | draft_keys,
                                      native_keys | projector_keys,
                                      native_keys | draft_keys | projector_keys)
                  and isinstance(source["model"], str)
                  and re.fullmatch(native_ref, source["model"])
                  and re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"]))
                  and all(isinstance(source[k], str) and re.fullmatch(r"[\w.-]+", source[k])
                          for k in ("renderer", "parser"))
                  and all(isinstance(source[f"{kind}_model"], str)
                          and re.fullmatch(native_ref, source[f"{kind}_model"])
                          and re.fullmatch(r"[0-9a-f]{64}", str(source[f"{kind}_sha256"]))
                          for kind in ("draft", "projector") if f"{kind}_model" in source))
        if not (snapshot or native):
            raise ProfileError(PROFILE_SCHEMA_INVALID,
                               f"{role}.source requires a pinned snapshot or native model/hash/renderer/parser")
    ci = cfg.get("context_input")
    if not isinstance(ci, int) or ci <= 0:
        raise ProfileError(ROLE_CONFIG_INVALID, f"{role}.context_input invalid")
    mo = cfg.get("max_output")
    if mo is None:
        return                      # embedding route: no generation, no reserve
    if not isinstance(mo, int) or mo <= 0:
        # -1/-2 (Ollama unbounded) would make admission uncomputable.
        raise ProfileError(ROLE_CONFIG_INVALID,
                           f"{role}.max_output must be a positive integer")



def geometry(context_input, max_output):
    """Derive context geometry; every consumer calls this, none recomputes it.

        total_context     = context_input + max_output   -> Ollama num_ctx
        num_predict       = max_output                   -> backend ceiling
        max_input_tokens  = context_input                -> admission

    max_output is the only ceiling that binds; LiteLLM ignores a smaller
    per-request max_tokens (measured on 1.93.0 and 1.98.0).
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
    """Return one role's effective configuration.

    `enabled` is false w/o a backend or on explicit disable. `_data` passes an
    already-parsed profile so a summary parses once.
    """
    if role in NON_ROLE_SECTIONS:
        raise ProfileError(ROLE_MISSING, f"{role} is not a capability role")
    data = load_profile(tier, repo_root) if _data is None else _data
    if role not in data:
        raise ProfileError(ROLE_MISSING, role)
    cfg = data[role]
    active = str(cfg.get("active", ""))
    out = {"tier": tier, "role": role, "name": cfg.get("role"),
           "model": active, "active": active,
           "enabled": bool(active) and bool(cfg.get("enabled", True))
                      and active.lower() not in ("none", "false", "disabled"),
           **geometry(cfg.get("context_input"), cfg.get("max_output"))}
    # Derived alias for total_context, read by planning and status.
    out["context"] = out["total_context"]
    for f in OPTIONAL_ROLE_FIELDS:
        out[f] = cfg.get(f)
    out["guidance"] = guidance_for(data, role)
    return out


def profile_summary(tier: str, repo_root=None) -> dict:
    """Return a serializable view for shells, doctor and status."""
    data = load_profile(tier, repo_root)
    return {
        "tier": tier,
        "status": data.get("status") or "unknown",
        "disk_gb": data.get("disk_gb"),
        "roles": {r: resolve_role(tier, r, repo_root, _data=data)
                  for r in ROLES if r in data},
    }


def required_runtime_patch(tier: str, repo_root=None) -> str | None:
    """Return the runtime patch enabled models declare they require."""
    required = {cfg["runtime_patch"] for cfg in profile_summary(tier, repo_root)["roles"].values()
                if cfg["enabled"] and cfg.get("runtime_patch")}
    if len(required) > 1:
        raise ProfileError(PROFILE_SCHEMA_INVALID, "profile requires conflicting native runtime patches")
    return next(iter(required), None)


# Runtime views derive from the profile per call; a cached file could go stale.

def active_tier(repo_root=None) -> str:
    return resolve_active_tier(repo_root)


def effective_role(role: str, repo_root=None) -> dict:
    return resolve_role(resolve_active_tier(repo_root), role, repo_root)


def effective_summary(repo_root=None) -> dict:
    return profile_summary(resolve_active_tier(repo_root), repo_root)


def model_guides(tier=None, repo_root=None) -> dict:
    """Return the public model guide for direct clients and MCP callers."""
    summary = profile_summary(tier or active_tier(repo_root), repo_root)
    return {"tier": summary["tier"], "models": {
        "ailocal-" + role: {
            "model": cfg["model"], "context_input": cfg["context_input"],
            "max_output": cfg["max_output"], "reasoning": cfg["reasoning"],
            "source": cfg.get("source"),
            "sampling": {key: cfg[key] for key in (
                "temperature", "top_p", "top_k", "min_p", "presence_penalty",
                "repeat_penalty", "draft_num_predict") if cfg.get(key) is not None},
            **cfg["guidance"],
        } for role, cfg in summary["roles"].items() if cfg["enabled"]}}
