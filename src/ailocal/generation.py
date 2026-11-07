#!/usr/bin/env python3
"""Generate every derived artifact from the profiles; the one generator.

Reads profiles/<tier>.toml and clients.toml via policy and writes each output
straight into the home its consumer reads, swapped atomically in memory.
Never hand-edit an output: edit the profile; `ailocal start` regenerates.
"""

import json
import os
import re
import shutil
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from ailocal import policy as _pc
from ailocal.guidance import render as render_guidance
from ailocal.report import ok, step, warn  # noqa: F401

# Paths come from policy. No staging copy: a second generated file can be stale.
PROFILES_DIR   = _pc.profiles_dir()
ACTIVE_PROFILE = _pc.active_profile_path()       # machine-selected tier
CLIENTS_YAML   = _pc.client_policy_path()
_DATA          = _pc.data_root()
_CLIENTS       = _pc.deployed_client_root()   # generated client config
LITELLM_TEMPLATE = _DATA / "deploy/litellm/config.template.yaml"
_LITELLM_OUT   = _pc.state_root() / "litellm"
LITELLM_CONFIG   = _LITELLM_OUT / "config.yaml"
CAPS_JSON      = _LITELLM_OUT / "capabilities.json"
CLAUDE_SETTINGS_TPL = _DATA / "clients/claude/settings.template.json"
CLAUDE_SETTINGS = _CLIENTS / "claude/settings.json"

#: settings.json `env` keys ailocal once wrote; removed so dead flags don't
#: read as live (ENABLE_LSP_TOOL: LSP is built in since Claude Code 2.1.224).
RETIRED_CLAUDE_ENV = {
    "ENABLE_LSP_TOOL": "the LSP tool is built into Claude Code since 2.0.74",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW":
        "Claude Code compacts against CLAUDE_CODE_MAX_CONTEXT_TOKENS on its own",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE":
        "Claude Code compacts against CLAUDE_CODE_MAX_CONTEXT_TOKENS on its own",
}
CODEX_HOME     = _CLIENTS / "codex"
CODEX_TPL        = _DATA / "clients/codex/config.template.toml"
CODEX_CATALOG  = CODEX_HOME / "model_catalog.json"
CODEX_CONFIG   = CODEX_HOME / "config.toml"
CONFIGURE_ZSH_TPL = _DATA / "clients/configure.template.zsh"
CONFIGURE_ZSH  = _CLIENTS / "configure.zsh"

# Cadence's only view of this runtime; see write_integration_contract().
CONTRACT_JSON  = _CLIENTS / "integration-contract.json"
BASE_URL       = "http://localhost:4000"


ML_BEGIN = "  # >>> BEGIN GENERATED model_list — do not edit <<<"
ML_END   = "  # >>> END GENERATED model_list <<<"
AL_BEGIN = "  # >>> BEGIN GENERATED model_group_alias — do not edit <<<"
AL_END   = "  # >>> END GENERATED model_group_alias <<<"
CS_BEGIN = "  # >>> BEGIN GENERATED claude slots — do not edit <<<"
CS_END   = "  # >>> END GENERATED claude slots <<<"

# One model_list entry per capability; `ailocal-` prefix applied at emit only.
MODEL_PREFIX = "ailocal-"
def mn(cap):
    """Return the client-facing `ailocal-` model id LiteLLM serves for `cap`."""
    return f"{MODEL_PREFIX}{cap}"






def flow_list(v):
    """Return an optional list field; absent means empty."""
    return v or []






# profile resolution
def resolve_tier(explicit=None):
    """Return the active tier: --profile wins, else the marker; no default."""
    if explicit:
        return explicit
    return _pc.resolve_active_tier()


def profile_path(tier=None, explicit=None):
    p = PROFILES_DIR / f"{resolve_tier(explicit) if tier is None else tier}.toml"
    if not p.exists():
        print(f"Error: profile not found: {p}", file=sys.stderr)
        sys.exit(1)
    return p


# config loading
def load_models_yaml(path):
    """Read a profile through the one policy reader."""
    data = _pc.load_profile_file(Path(path))
    models = {}
    for section, fields in data.items():
        if not isinstance(fields, dict):
            continue          # top-level scalars such as disk_gb
        models[section] = dict(fields)
    models.pop("disk_gb", None)
    # Retired sections (e.g. `[embeddings]`) linger in old profiles: inert.
    for section in _pc.NON_ROLE_SECTIONS:
        models.pop(section, None)
    # policy.ROLES is the one capability order for every surface.
    models = {role: models[role] for role in _pc.ROLES if role in models
              and _pc.resolve_role(Path(path).stem, role, _data=data)["enabled"]}
    for role, info in models.items():
        info["guidance"] = _pc.guidance_for(data, role)
    return models


def load_clients_yaml():
    """Return client policy from the one policy owner."""
    return _pc.load_client_policy()


def backend_of(info):
    """Return the Ollama tag served; `active` is required, so no fallback."""
    return info.get("active") or ""


def norm_keep_alive(v):
    """Map forever/persistent → -1; pass durations (2h, 60m) or -1 through."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    return "-1" if s.lower() in ("forever", "persistent") else s


def _geom(info):
    """Return role geometry from policy; missing context_input is an error."""
    return _pc.geometry(info.get("context_input"), info.get("max_output"))


def ctx_of(info):
    """Return the total window (Ollama num_ctx); derived, never configured."""
    return _geom(info)["num_ctx"]


# LiteLLM model_list

def gen_role_block(role, info):
    num_ctx = ctx_of(info)
    backend = backend_of(info)
    ka = norm_keep_alive(info.get("keep_alive"))

    # Every capability is conversational; no profile declares `provider`.
    provider = "ollama_chat"

    reasoning = bool(info.get("reasoning"))
    tooling   = role != "completion"
    parallel  = tooling and not reasoning and role != "cyber"
    _g        = _geom(info)
    max_out   = _g["max_output"]
    desc      = info.get("role", "")
    # No max_output → admission uncomputable; a guess advertises a false window.
    if max_out is None:
        raise SystemExit(f"invalid geometry: {role} declares no max_output")

    params = [
        f"  - model_name: {mn(role)}",
        f"    litellm_params:",
        f"      model: {provider}/{backend}",
        f"      api_base: os.environ/OLLAMA_URL",
        f"      num_ctx: {num_ctx}",
    ]
    # Use Ollama's native option names. repeat_penalty=1 disables the penalty.
    for key in ("temperature", "top_p", "top_k", "min_p",
                "presence_penalty", "repeat_penalty", "draft_num_predict"):
        if info.get(key) not in (None, ""):
            params.append(f"      {key}: {info[key]}")
    if info.get("presence_penalty") is not None:
        # LiteLLM 1.106.0 omits this option; its documented allowlist passes it.
        params.append('      allowed_openai_params: ["presence_penalty"]')
    if info.get("stop"):
        params.append(f"      stop: {json.dumps(info['stop'])}")
    # num_predict is the only ceiling the backend honours; per-request
    # max_tokens does not bind (LiteLLM 1.93.0 and 1.98.0, ollama_chat).
    if _g["num_predict"] is not None:
        params.append(f"      num_predict: {_g['num_predict']}")
    if ka is not None:
        params.append(f"      keep_alive: {ka}")
    if not reasoning:
        # think:false only for non-reasoning models: they 400 on Claude Code's
        # `thinking`; reasoning ones would lose their main capability.
        params.append('      additional_drop_params: ["thinking", "reasoning_effort"]')
        params.append("      think: false")
    else:
        params.append("      think: true")

    mi = [
        f"    model_info:",
        f"      supports_function_calling: {'true' if tooling else 'false'}",
        f"      supports_tool_choice: {'true' if tooling else 'false'}",
        f"      supports_parallel_function_calling: {'true' if parallel else 'false'}",
        f"      supports_system_messages: true",
        f"      supports_native_streaming: true",
        f"      supports_reasoning: {'true' if reasoning else 'false'}",
        # Zero cost silences LiteLLM's "not in cost map" boot warning.
        f"      input_cost_per_token: 0",
        f"      output_cost_per_token: 0",
        f"      cache_creation_input_token_cost: 0",
        f"      cache_read_input_token_cost: 0",
    ]
    # Admission is policy.geometry's; re-deriving it lets windows disagree.
    mi += [
        f"      max_input_tokens: {_g['max_input_tokens']}",
        f"      max_output_tokens: {max_out}",
    ]
    header = (f"  # {mn(role)} — {_pc.capability_description(info, info.get('guidance', {}))}"
              f" ({backend})\n" if desc else "")
    return header + "\n".join(params) + "\n" + "\n".join(mi) + "\n"


def gen_model_list(models):
    blocks = [gen_role_block(r, i) for r, i in models.items()]
    return ML_BEGIN + "\n\n" + "\n".join(blocks) + "\n" + ML_END + "\n"


def resolve(cap, models):
    """Resolve a client target to the enabled capability it names."""
    return _pc.resolve_capability(cap, models)


def gen_alias_block(models, clients):
    """Return model_group_alias lines mapping client-compat names to groups.

    claude-*/gpt-* names that clients hard-code point at `ailocal-<cap>`. The
    deprecated block keeps 0.16's retired names (and `ailocal-fast` on tiers
    w/o it) routing for one release instead of 404ing.
    """
    lines = [AL_BEGIN, "  model_group_alias:"]
    for name, cap in clients.get("compat", {}).items():
        lines.append(f"    {name}: {mn(resolve(cap, models))}")
    deprecated = _pc.deprecated_aliases(models)
    if deprecated:
        lines.append("    # DEPRECATED since 0.16 (#89), removed in 0.17: retired role names.")
        for old, cap in deprecated.items():
            lines.append(f"    {mn(old)}: {mn(cap)}")
    lines.append(AL_END)
    return "\n".join(lines) + "\n"


def splice(text, begin, end, generated, label):
    """Replace a marked region; a missing marker is fatal, never empty."""
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    if not pat.search(text):
        sys.exit(f"  \u2717 markers not found for {label}; the template has drifted "
                 f"from generation.py. Expected:\n      {begin}")
    return pat.sub(lambda _m: generated, text, count=1)


def regen_litellm(models, clients):
    # Always from the template: reading config.yaml back keeps hand-edits.
    text = LITELLM_TEMPLATE.read_text()
    text = splice(text, ML_BEGIN, ML_END, gen_model_list(models), "model_list")
    text = splice(text, AL_BEGIN, AL_END, gen_alias_block(models, clients),
                  "model_group_alias")
    stage(LITELLM_CONFIG, text)
    return True


# capabilities.generated.json (for `ailocal status`)
def write_caps_json(models):
    caps = []
    for name, info in models.items():
        ka = norm_keep_alive(info.get("keep_alive"))
        caps.append({
            "name": name,
            "role": info.get("role", name),
            "backend": backend_of(info),
            "preferred": flow_list(info.get("preferred")),
            "context": ctx_of(info),
            "keep_alive": ka,
            "persistent": ka == "-1",
            "purpose": flow_list(info.get("purpose")),
            "strengths": flow_list(info.get("strengths")),
            "weaknesses": flow_list(info.get("weaknesses")),
            "guidance": info.get("guidance", {}),
        })
    # JSON has no comments, so ownership travels in a "//" key.
    stage(CAPS_JSON, json.dumps(
        {"//": ["Generated by ailocal. Do not edit.",
                "Source: profiles/<active tier>.toml"],
         "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "capabilities": caps}, indent=2) + "\n")
    return True


def _home_relative(path: Path) -> str:
    """Return `path` as `~/...` under home; an overridden root stays as is."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def write_integration_contract(models):
    """Write the one surface an external consumer reads about this runtime.

    Facts only, no policy or prose. Consumers fail closed on an unknown
    `schema_version`: bump it on any shape or semantics change. No timestamp,
    so idempotence is hash-checkable. The instruction budget does not subtract
    the guide hook (#50 left too little, #56); `ailocal check` warns instead.
    """
    budgets = _pc.instruction_budgets(tier=_pc.resolve_active_tier())
    contract = {
        "//": ["Generated by ailocal. Do not edit.",
               "Source: profiles/<active tier>.toml, profiles/clients.toml"],
        "schema_version": 3,
        "instructions": {
            client: {"max_bytes": budgets[client],
                     "file": "CLAUDE.md" if client == "claude" else "AGENTS.md"}
            for client in ("claude", "codex")
        },
        "producer": "ailocal",
        "audit": {
            "record": _home_relative(_pc.state_root() / "audit" / "local.json"),
            "command": "ailocal audit",
        },
        "client_roots": {
            # CLAUDE_CONFIG_DIR / CODEX_HOME, ~-relative.
            "claude": "~/.config/ailocal/claude",
            "codex": "~/.config/ailocal/codex",
        },
        "runtime": {
            "inference_endpoint": BASE_URL,
            "canonical_capabilities": [mn(c) for c in models],
        },
        # Measured deployed state of routes through the proxy; a stale value
        # makes Cadence advertise a broken tool.
        "compatibility": {
            "claude_native_lsp": {
                "configured": True,
                # Gateway names `LSP` in the `always` floor, not via fail-open.
                "schema_preserved": True,
                # Fixed vocabulary: working | failing | blocked |
                # blocked_namespace_dispatch; else "configured, not verified".
                "execution": "working",
                "verified_by": "tests/clients.py",
                "scope": "python",
            },
            # No Codex LSP bridge; external registrations are their registrar's.
            "codex_mcp_lsp": {
                "configured": False,
                "schema_preserved": False,
                "execution": "not_configured",
                "reason": "no_validated_lsp_bridge",
                "verified_by": "tests/clients.py",
            },
        },
    }
    stage(CONTRACT_JSON, json.dumps(contract, indent=2, sort_keys=True) + "\n")
    return True


# Codex model_catalog.json
CATALOG_PREAMBLE = (
    "Ground every claim in the actual code: open the files, search before claiming something "
    "does not exist, and read a value before quoting it — the repository is the source of truth, "
    "not prior assumptions. Understand before you change; keep edits scoped and in the codebase's "
    "existing style; produce complete, runnable code with no placeholder stubs. Be precise and "
    "honest — state what you did and did not do, and lead with any uncertainty. Respect explicit "
    "constraints, and ask one focused question when something essential is missing."
)
CATALOG_TOOLING = (
    "Use `rg` / `rg --files` for exact text and file names. Use `apply_patch` for edits — never write files with "
    "`cat` or heredocs. Preserve the repository's configured git identity. Format in GitHub-flavored "
    "Markdown; no emojis or em dashes.\n\n"
    "Make file edits by calling the apply_patch tool. A patch printed in an assistant message "
    "does not modify a file. Its content parameter contains the entire patch text. "
    "Wait for the tool result before reporting an edit as complete. Use this exact envelope, "
    "replacing the path and lines for the real edit:\n"
    "*** Begin Patch\n*** Update File: example.py\n@@\n def value():\n-    return 1\n+    return 2\n*** End Patch\n"
    "Do not put a blank line after Begin Patch. Use a plain @@ line, not a numbered unified-diff "
    "header. Context lines start with a space; added lines start with +; removed lines start with -. "
    "Whitespace after a diff marker is file content: preserve the source indentation. "
    "Finish every task with a non-empty final answer after the tool results. State what changed "
    "and what was checked, or report the blocking error; a tool result alone is not a final reply."
)
def regen_catalog(models):
    """Stage the Codex model catalog, one entry per capability in ROLES order.

    Window = admission (context_input); Codex 0.163.0 compacts at 90% of it on
    its own, so no separate trigger is configured.
    """
    entries = []
    for index, (name, info) in enumerate(models.items()):
        backend = backend_of(info)
        geometry = _geom(info)
        guide = info.get("guidance", _pc.guidance_for({name: info}, name))
        instr = "\n\n".join([CATALOG_PREAMBLE, CATALOG_TOOLING,
                             render_guidance(mn(name), {"model": backend, **geometry,
                                 "sampling": {key: info[key] for key in (
                                     "temperature", "top_p", "top_k", "min_p", "presence_penalty",
                                     "repeat_penalty", "draft_num_predict") if info.get(key) is not None},
                                 "reasoning": info.get("reasoning"), **guide})])
        entries.append({
            "slug": mn(name),
            "display_name": mn(name),
            "description": _pc.capability_description(info, guide),
            "base_instructions": instr,
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 10 + 5 * index,
            "context_window": geometry["context_input"],
            "max_context_window": geometry["context_input"],
            "input_modalities": guide.get("input_modalities", ["text"]) if name != "completion" else ["text"],
            "supports_parallel_tool_calls": name not in ("completion", "cyber") and not bool(info.get("reasoning")),
            "supports_search_tool": False,
            "supports_image_detail_original": False,
            "supports_reasoning_summaries": False,
            "support_verbosity": False,
            "apply_patch_tool_type": "freeform",
            "web_search_tool_type": "text",
            "experimental_supported_tools": [],
            "default_reasoning_level": "medium" if info.get("reasoning") else "none",
            "supported_reasoning_levels": [
                {"effort": "none", "description": "Disable native thinking"},
                {"effort": "low", "description": "Enable native thinking; depth depends on the backend"},
                {"effort": "medium", "description": "Enable native thinking; depth depends on the backend"},
                {"effort": "high", "description": "Enable native thinking; depth depends on the backend"},
            ],
            "service_tiers": [],
            "truncation_policy": {"mode": "tokens", "limit": 10000},
        })
    stage(CODEX_CATALOG, json.dumps(
        {"//": ["Generated by ailocal. Do not edit.",
                "Source: profiles/<active tier>.toml + profiles/clients.toml"],
         "models": entries}, indent=2) + "\n")
    return True


def regen_vscode(models):
    """Refresh an already configured ailocal provider/instruction block only.

    Returns None for an unusable editor file: preserved, reported, non-blocking.
    """
    from ailocal import clients
    user = clients._vscode_user_dir()
    if user is None:
        return False
    path = user / "chatLanguageModels.json"
    if not path.is_file():
        return False
    try:
        existing = json.loads(path.read_text())
        if not any(clients.vscode_owned(g) for g in existing if isinstance(existing, list)):
            return False
        document = clients.vscode_provider_document(existing, models)
    except ValueError as exc:
        warn(f"VS Code model configuration preserved, not regenerated: {exc}")
        return None
    stage(path, json.dumps(document, indent="\t") + "\n")
    instructions = clients.copilot_instructions_path()
    text = instructions.read_text() if instructions.is_file() else ""
    if clients.copilot_instructions_owned(text):
        # Whole, so an upgrade refreshes the head too (#108).
        stage(instructions, clients.copilot_instructions_text(models))
    elif clients.COPILOT_GUIDE_START in text:
        # An edited head is the operator's: only the generated block changes.
        stage(instructions, splice(text, clients.COPILOT_GUIDE_START,
              clients.COPILOT_GUIDE_END, clients.copilot_model_guidance(models), "Copilot model guidance"))
    return True


# configure.zsh: claude-local built-in-slot env block, from clients.toml.
SLOT_ENV = {
    "opus":   "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku":  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable":  "ANTHROPIC_DEFAULT_FABLE_MODEL",
}




def gen_slot_block(models, clients):
    lines = [CS_BEGIN, "  slots=("]
    for slot, cap in (clients.get("claude") or {}).get("slots", {}).items():
        var = SLOT_ENV.get(slot)
        if not var:
            warn(f"unknown claude slot '{slot}' — no ANTHROPIC_DEFAULT_* var; skipping")
            continue
        lines.append(f'    {var}="{mn(resolve(cap, models))}"')
    lines += ["  )", CS_END]
    return "\n".join(lines) + "\n"



def regen_configure_zsh(models, clients):
    if not CONFIGURE_ZSH_TPL.exists():
        return False
    text = CONFIGURE_ZSH_TPL.read_text()
    stage(CONFIGURE_ZSH, splice(text, CS_BEGIN, CS_END, gen_slot_block(models, clients),
                                "claude slots"))
    return True


def claude_picker_rows(models):
    """Return `modelPicker` rows: every enabled capability, in ROLES order.

    `replaceBuiltInOptions: true` hides the built-in and gateway-discovered
    rows (code.claude.com/docs/en/settings-reference#modelpicker, 2.1.242+).
    """
    return [{"model": mn(name), "label": mn(name),
             "description": _pc.capability_description(info, info.get("guidance", {}))}
            for name, info in models.items()]


def regen_claude_settings(models, clients):
    """Stage settings.json, co-owned with Claude Code: on-disk keys win.

    Only ailocal's keys are overwritten: `model` (reset: Claude saves the last
    pick, possibly a `claude-router-<hex>` id), `//`, `modelPicker`, geometry.
    """
    if not CLAUDE_SETTINGS_TPL.exists():
        return False
    data = json.loads(CLAUDE_SETTINGS_TPL.read_text())
    if CLAUDE_SETTINGS.exists():
        try:
            live = json.loads(CLAUDE_SETTINGS.read_text())
        except ValueError:
            live = {}
        if isinstance(live, dict):
            merged = {**data, **live}
            # Both sides add to `env`, so it merges rather than being replaced.
            if isinstance(data.get("env"), dict) or isinstance(live.get("env"), dict):
                merged["env"] = {**(data.get("env") or {}), **(live.get("env") or {})}
            data = merged
    # Deny hosted ~/.claude memory that loads under $HOME (13,335 B, #62);
    # absolute paths keep a repo's own .claude/CLAUDE.md. User values kept.
    hosted = Path.home() / ".claude"
    excludes = data.get("claudeMdExcludes") or []
    for path in (str(hosted / "CLAUDE.md"), str(hosted / "rules") + "/**"):
        if path not in excludes:
            excludes.append(path)
    data["claudeMdExcludes"] = excludes
    data.setdefault("disableBundledSkills", True)
    perms = data.setdefault("permissions", {})
    deny = perms.setdefault("deny", [])
    # Claude Code has no per-server filter: deny the 8 grepai tools Cadence
    # hides elsewhere (7.2 KB of the first request, #66).
    unused_grepai = tuple("mcp__grepai__grepai_" + t for t in (
        "refs_graph", "refs_readers", "refs_writers", "rpg_explore", "rpg_fetch",
        "rpg_search", "stats", "trace_graph"))
    for tool in ("ScheduleWakeup", "ReportFindings", "ListAgents") + unused_grepai:
        if tool not in deny:
            deny.append(tool)
    default = resolve((clients.get("claude") or {}).get("launch_default", next(iter(models))), models)
    caps = " | ".join(mn(k) for k in models.keys())
    slots = {slot: resolve(cap, models)
             for slot, cap in (clients.get("claude") or {}).get("slots", {}).items()}
    slot_txt = ", ".join(f"{k.title()}->{mn(v)}" for k, v in slots.items())
    data["model"] = mn(default)
    # Model selection is client-owned; native hooks deliver the profile facts.
    hooks = data.setdefault("hooks", {})
    command = "ailocal profile guide --hook"
    for event in ("SessionStart", "PostModelSwitch"):
        entries = hooks.setdefault(event, [])
        if not any(h.get("command") == command for entry in entries
                   for h in entry.get("hooks", [])):
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    data["//"] = [
        "Claude Code settings — deployed to ~/.config/ailocal/claude/settings.json",
        "(CLAUDE_CONFIG_DIR for the local variant; ~/.claude is never touched).",
        "",
        "Generated by ailocal. Source: profiles/clients.toml + profiles/<tier>.toml.",
        "Base URL + key are injected per-process by the claude-local() wrapper.",
        "",
        "All requests route through LiteLLM by ailocal-<capability> model id.",
        f"Models: {caps}",
        f"Launch default = {mn(default)}. /model lists exactly these models (modelPicker).",
        f"Built-in slots remap: {slot_txt}.",
        "Launch with: claude-local",
    ]
    out_geom = _geom(models[default])
    # Output ceiling = num_predict: else Claude asks for 32000, reads each
    # max_tokens stop as "continue" and loops for minutes. Derived, not cfg.
    if not out_geom["max_output"]:
        raise SystemExit(
            f"invalid geometry: claude launch_default '{default}' declares no "
            "max_output, so there is no knowable output ceiling to project")
    data.setdefault("env", {})["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(out_geom["max_output"])
    data["//"].append(
        f"Output ceiling: CLAUDE_CODE_MAX_OUTPUT_TOKENS = {out_geom['max_output']} "
        f"(= {default}.max_output, the same number LiteLLM gets as num_predict). "
        "Claude Code defaults to 32000, which the backend does not serve.")
    # Window Claude assumes for a non-Claude id (else 200000; see
    # code.claude.com/docs/en/model-config). Admission, not total: Claude
    # compacts 13000 + min(max_output, 20000) below it, before rejection.
    data["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(out_geom["context_input"])
    data["//"].append(
        f"Assumed window: CLAUDE_CODE_MAX_CONTEXT_TOKENS = {out_geom['context_input']} "
        f"(= {default}.context_input, the admission LiteLLM enforces). Claude Code "
        "compacts against it on its own; it otherwise assumes 200000 for an "
        "unrecognised gateway model id.")
    # Known gap: both vars are process-wide (launch default); a smaller slot
    # can be rejected by LiteLLM before Claude compacts.
    smaller = sorted(
        (mn(cap), g["max_input_tokens"])
        for cap, g in ((c, _geom(models[c])) for c in slots.values() if c in models)
        if g["max_input_tokens"] < out_geom["max_input_tokens"])
    if smaller:
        data["//"].append(
            "Slots Claude Code cannot declare separately (process-wide vars "
            f"describe {mn(default)} only): "
            + ", ".join(f"{n} admits {v}" for n, v in smaller))
    # Rewritten each run so a retired capability never lingers as a row.
    data["modelPicker"] = {"options": claude_picker_rows(models),
                           "replaceBuiltInOptions": True}
    data["//"].append(
        "Picker (modelPicker, replaceBuiltInOptions): "
        + ", ".join(mn(k) for k in models) + ". Same labels and descriptions as Codex.")
    # Merged `env` never drops a template-removed key, so retire it explicitly;
    # report on the console only, so the output stays a fixed point.
    for key, why in RETIRED_CLAUDE_ENV.items():
        if key in (data.get("env") or {}):
            data["env"].pop(key)
            print(f"  removed {key} from claude/settings.json: {why}")
    if data.get("env") == {}:
        data.pop("env")
    stage(CLAUDE_SETTINGS, json.dumps(data, indent=2) + "\n")
    return True


# Codex config.toml + profiles
#: Blocks Codex writes into config.toml itself.
_CODEX_PROJECTS = re.compile(
    r'(?ms)^\[(?:projects|hooks\.state)\.".*?"\]\n(?:(?!^\[).*?\n)*')
_CODEX_TUI_FLAG = re.compile(
    r'(?m)^\[tui\]\n(?:(?!\[)[^\n]*\n)*?(screen_reader_detection_done\s*=[^\n]*)$')
_CODEX_PLUGINS = re.compile(
    r'(?ms)^\[(?:marketplaces|plugins)\.[^\n]+\][ \t]*\n(?:(?!^\[).*?\n)*')
_CODEX_MCP = re.compile(
    r'(?ms)^\[mcp_servers\.[^\n]+\][ \t]*(?:#[^\n]*)?\n(?:(?!^\[).*?\n)*')


def _keep_codex_projects(text: str) -> str:
    """Return Codex-owned config.toml blocks to carry across regeneration.

    Kept: `[projects.*]` and `[hooks.state.*]` trust records (else every start
    un-trusts them), `[tui] screen_reader_detection_done` while the template
    has no `[tui]`, external MCP servers, `[marketplaces.*]`, `[plugins.*]`.
    """
    if not CODEX_CONFIG.exists():
        return text
    try:
        live = CODEX_CONFIG.read_text()
    except OSError:
        return text
    # Trim each block to its keys (it would carry the next header's comment)
    # and keep one per header, repairing a duplicated table.
    blocks, seen = [], set()
    owned_servers = tomllib.loads(text).get("mcp_servers", {})
    external_mcp = []
    for block in _CODEX_MCP.findall(live):
        header = block.split("\n", 1)[0]
        server = next(iter(tomllib.loads(header)["mcp_servers"]))
        if server not in owned_servers:
            external_mcp.append(block)
    for b in _CODEX_PROJECTS.findall(live) + _CODEX_PLUGINS.findall(live) + external_mcp:
        lines = b.rstrip("\n").split("\n")
        while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
            lines.pop()
        if not lines or lines[0] in seen or lines[0] in text:
            continue
        seen.add(lines[0])
        blocks.append("\n".join(lines) + "\n\n")
    flag = _CODEX_TUI_FLAG.search(live)
    if flag and not re.search(r"(?m)^\[tui\]$", text):
        blocks.append(f"[tui]\n{flag.group(1)}\n")
    if not blocks:
        return text
    return text.rstrip("\n") + "\n\n" + "".join(blocks).rstrip("\n") + "\n"


def regen_codex(models, clients):
    cx = clients.get("codex", {})
    default = resolve(cx.get("default", next(iter(models))), models)
    done = False
    if CODEX_TPL.exists():
        text = (MARKER_TOML + CODEX_TPL.read_text()
                ).replace("${CODEX_HOME}", str(CODEX_HOME))
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{mn(default)}"', text, count=1)
        text = re.sub(r'(?m)^# Valid models:.*$',
                      "# Valid models: " + " | ".join(mn(k) for k in models.keys()), text, count=1)
        stage(CODEX_CONFIG, text)
        done = True
        # Limits are per catalog entry; a global one leaks the default's window.
        text = _keep_codex_projects(text)
        stage(CODEX_CONFIG, text)
    return done



def parse_profile_flag(argv):
    """Pull `--profile <tier>` out of argv; return (tier or None, rest)."""
    tier, rest, i = None, [], 0
    while i < len(argv):
        if argv[i] == "--profile" and i + 1 < len(argv):
            tier = argv[i + 1]; i += 2
        else:
            rest.append(argv[i]); i += 1
    return tier, rest


# atomic generation
# Outputs are staged and replaced only after every generator succeeds: a
# partial run would split LiteLLM and clients across profiles silently.
_STAGE: dict = {}
_STAGED_TEXT: dict = {}

MARKER_TOML = ("# Generated by ailocal. Do not edit.\n"
               "# Source: profiles/<tier>.toml\n"
               "# Owner: DevelopSolutions, LLC\n")


def stage(path, text):
    """Record an output for flush_stage() to write."""
    _STAGE[Path(path)] = text
    _STAGED_TEXT[Path(path)] = text


def flush_stage():
    """Write every staged output, then swap them in with rollback on failure.

    Per-file atomic (os.replace); a mid-way failure restores replaced files,
    so the tree is all old or all new (tests/clients.py injects faults).
    """
    tmp, backups, replaced = {}, {}, []
    try:
        for path, text in _STAGE.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            t = path.with_suffix(path.suffix + ".tmp-sync")
            t.write_text(text)
            tmp[path] = t
        # Validate first: an unparseable artifact must not replace a good one.
        for path, t in tmp.items():
            if path.suffix == ".json":
                try:
                    json.loads(Path(t).read_text())
                except Exception as exc:  # noqa: BLE001
                    raise SystemExit(
                        f"generation aborted: {path.name} is not valid JSON "
                        f"({exc}); nothing was replaced")
        for path in sorted(tmp, key=str):
            if path.exists():
                b = path.with_suffix(path.suffix + ".bak-sync")
                shutil.copy2(path, b)
                backups[path] = b
            os.replace(tmp[path], path)
            replaced.append(path)
    except BaseException:
        # Restore every destination already replaced, newest first.
        for path in reversed(replaced):
            b = backups.get(path)
            try:
                if b is not None and Path(b).exists():
                    os.replace(b, path)
                else:
                    Path(path).unlink(missing_ok=True)   # it did not exist before
            except Exception:  # noqa: BLE001
                print(f"  ROLLBACK FAILED for {path}", file=sys.stderr)
        raise
    finally:
        for t in tmp.values():
            try:
                Path(t).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        for b in backups.values():
            try:
                Path(b).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    n = len(_STAGE)
    _STAGE.clear()
    return n



def _stable(t):
    return re.sub(r'"generated_at":\s*"[^"]*"', '"generated_at": ""', t)

def _same(dest, text):
    """Return whether the destination already holds this output.

    JSON and TOML compare parsed: co-owners reorder keys and tables; else text.
    """
    live = _stable(dest.read_text())
    staged = _stable(text)
    if live == staged:
        return True
    parse = {".json": json.loads, ".toml": tomllib.loads}.get(dest.suffix)
    if parse is None:
        return False
    try:
        return parse(live) == parse(staged)
    except ValueError:
        return False


def main(argv=None):
    tier, args = parse_profile_flag(sys.argv[1:] if argv is None else list(argv))

    check_only = bool(args) and args[0] == "--check"
    if check_only:
        args = args[1:]

    # Require the template, not config.yaml (output), so a fresh clone works.
    if not LITELLM_TEMPLATE.exists():
        print(f"Error: {LITELLM_TEMPLATE} not found", file=sys.stderr); sys.exit(1)

    tier = resolve_tier(tier)          # fail-closed; never an implicit default
    path = profile_path(tier=tier)
    step(f"Reading {path.relative_to(_pc.config_root())} + profiles/clients.toml")
    models = load_models_yaml(path)
    clients = load_clients_yaml()
    for role, info in models.items():
        print(f"  {role}: {backend_of(info)}  (ctx {ctx_of(info)}, keep_alive {norm_keep_alive(info.get('keep_alive'))})")

    # The rule is owned by policy; the generator's job is to fail closed on it.
    for severity, message in _pc.slot_problems():
        if severity == "error":
            sys.exit(f"error: {message}")
        warn(message)

    step("Regenerating litellm/config.yaml (model_list + aliases)")
    ok("litellm config regenerated" if regen_litellm(models, clients) else "litellm config unchanged/skipped")

    step("Writing derived files")
    ok("capabilities.generated.json") if write_caps_json(models) else warn("caps json skipped")
    ok("model_catalog.json") if regen_catalog(models) else warn("catalog skipped")
    vscode = regen_vscode(models)
    if vscode is not None:
        ok("VS Code native models + guidance" if vscode else "VS Code provider not configured")
    ok("claude/settings.json") if regen_claude_settings(models, clients) else warn("claude settings skipped")
    ok("configure.zsh (claude slots)") if regen_configure_zsh(models, clients) else warn("configure.zsh slots skipped")
    ok("integration-contract.json (for Cadence)") if write_integration_contract(models) else warn("contract skipped")
    ok("codex config + profiles") if regen_codex(models, clients) else warn("codex skipped")
    if check_only:
        # Normalise the generated_at timestamp, else every run is drift.
        def _label(d):
            for base in (_pc.state_root(), _pc.config_root(), _pc.data_root()):
                try:
                    return str(d.relative_to(base))
                except ValueError:
                    continue
            return str(d)

        drift = sorted(_label(d) for d, text in _STAGE.items()
                       if not d.exists() or not _same(d, text))
        # --check writes nothing: clear staging for later calls in-process.
        _STAGE.clear()
        _STAGED_TEXT.clear()
        if drift:
            print("DRIFT — generated files are stale: " + ", ".join(drift),
                  file=sys.stderr)
            return 1
        print("[REAL] in sync — generated files match the active profile "
              "and client policy")
        return 0
    n = flush_stage()
    ok(f"{n} generated files replaced atomically")
    step("Done — generated straight into the homes their consumers read. "
         "Restart LiteLLM with `ailocal start`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
