#!/usr/bin/env python3
"""generation.py — the ONE generator, and the only public entry point for it.

Reads profiles/<tier>.toml (what each capability is) and profiles/clients.toml
(which capability each client surface uses) through policy, and writes every
derived artifact DIRECTLY INTO THE HOME ITS CONSUMER READS — the two LiteLLM
bind-mounts under $AILOCAL_STATE, everything else under the config root. There
is no staging tree and no deployment copy. Outputs are staged in memory and
swapped atomically, so the tree is never part old and part new.

Never hand-edit a generated file: edit the profile; `ailocal start` regenerates.
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Interactive-compaction thresholds, read from the profile's `compaction:` block.
COMPACTION = {}

from ailocal import policy as _pc

# Every path comes from policy. Four roots, each with ONE owner:
#   config root   authored policy (profiles/, .env) — the operator's
#   data root     shipped templates and assets, read in place from the package
#   client root   generated client config, in the home each client reads
#   state root    the two artefacts LiteLLM bind-mounts, plus the tier marker
#
# Client config is generated STRAIGHT INTO the home the client reads. There is
# no staging copy under the state root: an intermediate generated file is a
# second thing that can be stale, and the copy step that fed it was the only
# reason a client could be running last week's routing.
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

#: settings.json `env` keys ailocal once wrote and now removes on regeneration.
#: [REAL] ENABLE_LSP_TOOL was the pre-2.0.74 gate for the LSP tool, which is
#: built in as of Claude Code 2.1.224: hosted ~/.claude/settings.json sets it
#: nowhere and a documentSymbol request there returns real pyright output. A
#: dead flag left in generated config reads like a live requirement.
RETIRED_CLAUDE_ENV = {
    "ENABLE_LSP_TOOL": "the LSP tool is built into Claude Code since 2.0.74",
}
CODEX_HOME     = _CLIENTS / "codex"
CODEX_TPL        = _DATA / "clients/codex/config.template.toml"
CODEX_PLAN_TPL   = _DATA / "clients/codex/plan.config.template.toml"
CODEX_REVIEW_TPL = _DATA / "clients/codex/review.config.template.toml"
CODEX_CATALOG  = CODEX_HOME / "model_catalog.json"
CODEX_CONFIG   = CODEX_HOME / "config.toml"
CODEX_PLAN     = CODEX_HOME / "plan.config.toml"
CODEX_REVIEW   = CODEX_HOME / "review.config.toml"
CONFIGURE_ZSH_TPL = _DATA / "clients/configure.template.zsh"
CONFIGURE_ZSH  = _CLIENTS / "configure.zsh"

# The machine-readable seam with Cadence. Cadence reads THIS and nothing else to
# learn about the local runtime — see write_integration_contract(). ailocal does
# NOT own client instruction policy; an external consumer composes it from this contract.
CONTRACT_JSON  = _CLIENTS / "integration-contract.json"
BASE_URL       = "http://localhost:4000"


ML_BEGIN = "  # >>> BEGIN GENERATED model_list — do not edit <<<"
ML_END   = "  # >>> END GENERATED model_list <<<"
AL_BEGIN = "  # >>> BEGIN GENERATED model_group_alias — do not edit <<<"
AL_END   = "  # >>> END GENERATED model_group_alias <<<"
CS_BEGIN = "  # >>> BEGIN GENERATED claude slots — do not edit <<<"
CS_END   = "  # >>> END GENERATED claude slots <<<"

# One model_list entry per capability, named `ailocal-<cap>`. The prefix is
# applied only at emit time; the source spells the bare capability key.
MODEL_PREFIX = "ailocal-"
def mn(cap):
    """Client-facing model id for a capability (the ailocal- prefixed name LiteLLM serves)."""
    return f"{MODEL_PREFIX}{cap}"


def step(m): print(f"\n▶ {m}")
def ok(m):   print(f"  ✓ {m}")
def warn(m): print(f"  ⚠ {m}", file=sys.stderr)




def flow_list(v):
    """Optional list field. policy.py returns typed lists; absent means empty."""
    return v or []






# ── profile resolution ─────────────────────────────────────────────────────────
def resolve_tier(explicit=None):
    """Which RAM profile is active: an explicit --profile wins, else the marker.

    Delegates to policy, which fails closed: there is no default tier."""
    if explicit:
        return explicit
    return _pc.resolve_active_tier()


def profile_path(tier=None, explicit=None):
    p = PROFILES_DIR / f"{resolve_tier(explicit) if tier is None else tier}.toml"
    if not p.exists():
        print(f"Error: profile not found: {p}", file=sys.stderr)
        sys.exit(1)
    return p


# ── config loading ────────────────────────────────────────────────────────────
def load_models_yaml(path):
    """Read a profile through the one policy reader."""
    data = _pc.load_profile_file(Path(path))
    models = {}
    for section, fields in data.items():
        if not isinstance(fields, dict):
            continue          # top-level scalars such as disk_gb
        models[section] = dict(fields)
    models.pop("disk_gb", None)
    COMPACTION.update(models.pop("compaction", {}))
    # policy owns which sections are NOT capabilities. This used to name
    # `compaction` and nothing else, so any other non-capability section reached
    # the geometry check and failed the whole generation — which is exactly what
    # a retired `[embeddings]` section did on every already-installed machine,
    # where profiles live in the user's config root and are not rewritten.
    for section in _pc.NON_ROLE_SECTIONS:
        models.pop(section, None)
    return models


def load_clients_yaml():
    """Client policy, from the one policy owner."""
    return _pc.load_client_policy()


def backend_of(info):
    """The Ollama backend tag actually served. `active` is required by the
    profile schema, so there is no fallback chain to guess through."""
    return info.get("active") or ""


def norm_keep_alive(v):
    """forever/persistent -> -1; else pass through (durations like 2h/60m, or -1)."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    return "-1" if s.lower() in ("forever", "persistent") else s


def _geom(info):
    """Derived geometry for a role, from the ONE implementation in policy.

    generation never re-derives num_ctx, num_predict or admission, and injects
    no default: a role missing context_input is an error, not a guess.
    """
    return _pc.geometry(info.get("context_input"), info.get("max_output"))


def ctx_of(info):
    """Total physical window (Ollama num_ctx). Derived, never configured."""
    return _geom(info)["num_ctx"]


# ── LiteLLM model_list ─────────────────────────────────────────────────────────

def gen_role_block(role, info):
    num_ctx = ctx_of(info)
    backend = backend_of(info)
    ka = norm_keep_alive(info.get("keep_alive"))

    # Every remaining capability is conversational, so the provider is
    # `ollama_chat`. The `ollama` branch that used to live here existed only to
    # emit an embedding route (`mode: embedding`) for a capability nothing
    # called; it went with the capability. No profile declares `provider`.
    provider = "ollama_chat"

    reasoning = bool(info.get("reasoning"))
    parallel  = not reasoning
    _g        = _geom(info)
    max_out   = _g["max_output"]
    desc      = info.get("role", "")
    # No invented ceiling. A chat role without max_output has no knowable output
    # reserve, so admission is uncomputable and a guess would advertise a window
    # the backend does not honour.
    if max_out is None:
        raise SystemExit(f"invalid geometry: {role} declares no max_output")

    params = [
        f"  - model_name: {mn(role)}",
        f"    litellm_params:",
        f"      model: {provider}/{backend}",
        f"      api_base: os.environ/OLLAMA_URL",
        f"      num_ctx: {num_ctx}",
    ]
    # repeat_penalty is OLLAMA's option name; repetition_penalty is LiteLLM's.
    # Only the former reaches the backend, so both are forwarded and the profile
    # decides which to set. A value of 1.0 means NO penalty and is set
    # explicitly rather than relying on a backend default.
    for key in ("temperature", "top_p", "top_k", "repetition_penalty",
                "repeat_penalty"):
        if info.get(key) not in (None, ""):
            params.append(f"      {key}: {info[key]}")
    # num_predict is the only ceiling the backend honours: [REAL] a per-request
    # max_tokens of 512 against an alias declaring 32768 returned 4,199 tokens
    # (LiteLLM 1.93.0, ollama_chat).
    if _g["num_predict"] is not None:
        params.append(f"      num_predict: {_g['num_predict']}")
    if ka is not None:
        params.append(f"      keep_alive: {ka}")
    if not reasoning:
        # Suppress reasoning ONLY for models that cannot do it. Claude Code sends
        # `thinking` on every request and a non-thinking backend 400s on it, but
        # emitting think:false for a reasoning-capable model throws away its
        # biggest capability. The `reasoning` flag in profiles/<tier>.toml drives
        # this per capability.
        params.append('      additional_drop_params: ["thinking", "reasoning_effort"]')
        params.append("      think: false")
    else:
        params.append("      think: true")

    mi = [
        f"    model_info:",
        f"      supports_function_calling: true",
        f"      supports_tool_choice: true",
        f"      supports_parallel_function_calling: {'true' if parallel else 'false'}",
        f"      supports_system_messages: true",
        f"      supports_native_streaming: true",
        f"      supports_reasoning: {'true' if reasoning else 'false'}",
        # Local models are billed nothing, and declaring it keeps LiteLLM's
        # "not in built-in cost map" warning out of every boot log. Cost
        # accounting only; no effect on routing or inference.
        f"      input_cost_per_token: 0",
        f"      output_cost_per_token: 0",
        f"      cache_creation_input_token_cost: 0",
        f"      cache_read_input_token_cost: 0",
    ]
    # Admission is context_input by construction (policy.geometry). Deriving it
    # a second time here is what let advertised and enforced windows disagree.
    mi += [
        f"      max_input_tokens: {_g['max_input_tokens']}",
        f"      max_output_tokens: {max_out}",
    ]
    header = f"  # {mn(role)} — {desc} ({backend})\n" if desc else ""
    return header + "\n".join(params) + "\n" + "\n".join(mi) + "\n"


def gen_model_list(models):
    blocks = [gen_role_block(r, i) for r, i in models.items()]
    return ML_BEGIN + "\n\n" + "\n".join(blocks) + "\n" + ML_END + "\n"


def gen_alias_block(clients):
    """model_group_alias entries from clients.toml: the external client-compat names (claude-*/gpt-*)
    that Claude Code and the OpenAI SDK hard-code, each pointing at its `ailocal-<cap>` model group.
    No `local/*` namespace — the single canonical `ailocal-<cap>` model_list entry is the only name.
    Compat names inherit the target group's persona/settings."""
    lines = [AL_BEGIN, "  model_group_alias:"]
    for name, cap in clients.get("compat", {}).items():
        lines.append(f"    {name}: {mn(cap)}")
    lines.append(AL_END)
    return "\n".join(lines) + "\n"


def splice(text, begin, end, generated, label):
    """Replace a marked region. A missing marker is fatal.

    Skipping produced a config.yaml with no model_list at all — a template whose
    markers have drifted must stop generation, not silently emit an empty stack.
    """
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    if not pat.search(text):
        sys.exit(f"  \u2717 markers not found for {label}; the template has drifted "
                 f"from generation.py. Expected:\n      {begin}")
    return pat.sub(lambda _m: generated, text, count=1)


def regen_litellm(models, clients):
    # ALWAYS from the template. config.yaml is a build artifact: reading it back
    # would let a hand-edit to the generated file survive, which is the drift the
    # template exists to prevent.
    text = LITELLM_TEMPLATE.read_text()
    text = splice(text, ML_BEGIN, ML_END, gen_model_list(models), "model_list")
    text = splice(text, AL_BEGIN, AL_END, gen_alias_block(clients),
                  "model_group_alias")
    stage(LITELLM_CONFIG, text)
    return True


# ── capabilities.generated.json (for `ailocal status`) ─────────────────────────
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
        })
    # JSON cannot carry comments, so ownership travels in a "//" key -- every
    # other generated artifact states its owner and how to regenerate it, and a
    # timestamp alone does not tell a reader not to hand-edit this.
    stage(CAPS_JSON, json.dumps(
        {"//": ["Generated by ailocal. Do not edit.",
                "Source: profiles/<active tier>.toml"],
         "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "capabilities": caps}, indent=2) + "\n")
    return True


def write_integration_contract(models):
    """The ONLY surface an external consumer reads to learn about this runtime.

    FACTS ONLY — roots, endpoint, canonical capability names, and what is
    measurably true about routes through the proxy. No policy and no prose: a
    consumer must never have to parse ailocal's Markdown to discover a fact.

    `schema_version` is load-bearing (the consumer fails closed on a version it
    does not understand); bump it on any change to shape or field semantics.
    No timestamp, so idempotence stays hash-checkable.
    """
    contract = {
        "//": ["Generated by ailocal. Do not edit.",
               "Source: profiles/<active tier>.toml"],
        "schema_version": 1,
        "producer": "ailocal",
        "client_roots": {
            # Where the launchers point CLAUDE_CONFIG_DIR / CODEX_HOME. Written
            # as ~-relative so the contract is not machine-specific.
            "claude": "~/.config/ailocal/claude",
            "codex": "~/.config/ailocal/codex",
        },
        "runtime": {
            "inference_endpoint": BASE_URL,
            "canonical_capabilities": [mn(c) for c in models],
        },
        # Measured compatibility of routes that pass THROUGH the proxy. These are
        # the facts that make Cadence describe a tool as usable or not; getting
        # them wrong makes it advertise a broken tool as a first choice.
        # THESE DESCRIBE DEPLOYED STATE, NOT AN INVESTIGATION. A consumer reads
        # them to decide whether a tool is usable, so a stale value here makes
        # it apply the wrong policy.
        "compatibility": {
            "claude_native_lsp": {
                "configured": True,
                # The gateway names native `LSP` explicitly (registry group
                # `native_lsp`, in the `always` floor) rather than relying on
                # fail-open, so the schema survives every task class.
                "schema_preserved": True,
                # `execution` is a FIXED vocabulary on the consumer side:
                # working | failing | blocked | blocked_namespace_dispatch.
                # Any other string degrades to "configured but not verified".
                "execution": "working",
                "verified_by": "tests/lsp-baseline.py",
                "scope": "python",
            },
            # WITHHELD, not missing: Codex declares MCP servers as namespace
            # BUNDLES, which LiteLLM discards before the backend, and flattening
            # them makes Codex's own dispatcher refuse the call
            # (openai/codex#20652). `reason` exists so a consumer does not read
            # this as a transient failure and retry.
            "codex_mcp_lsp": {
                "configured": False,
                "schema_preserved": False,
                "execution": "withheld_client_incompatible",
                "reason": "codex_cannot_dispatch_namespaced_tools",
                "upstream": "openai/codex#20652",
                "verified_by": "tests/clients.sh codex",
            },
        },
    }
    stage(CONTRACT_JSON, json.dumps(contract, indent=2, sort_keys=True) + "\n")
    return True


# ── Codex model_catalog.json ───────────────────────────────────────────────────
CATALOG_PREAMBLE = (
    "Ground every claim in the actual code: open the files, search before claiming something "
    "does not exist, and read a value before quoting it — the repository is the source of truth, "
    "not prior assumptions. Understand before you change; keep edits scoped and in the codebase's "
    "existing style; produce complete, runnable code with no placeholder stubs. Be precise and "
    "honest — state what you did and did not do, and lead with any uncertainty. Respect explicit "
    "constraints, and ask one focused question when something essential is missing."
)
CATALOG_TOOLING = (
    "Use `rg` / `rg --files` for search. Use `apply_patch` for edits — never write files with "
    "`cat` or heredocs. Set the git author locally: `git config user.name \"Victor T. Chevalier\"` "
    "and `user.email \"13876123+VTChevalier@users.noreply.github.com\"`. Format in GitHub-flavored "
    "Markdown; no emojis or em dashes."
)
CATALOG_ROLE_SENTENCE = {
    "architecture":  "You are the architect tier: architecture, complex refactoring, multi-step debugging, and design. Decompose the work, map the system before committing, and pivot if an approach keeps failing.",
    "implementation":"You are the everyday coder for repository work, logic, and syntax. Plan the change briefly, then implement it fully; trace a bug to its source before fixing it.",
    "review":        "You review; you do not fix. Read the full file, its tests, and the diff, then report findings ranked by severity (correctness, security, error handling, test gaps, design) with file:line and a concrete fix each.",
    "completion":    "You handle quick, small tasks fast. If a task needs multi-file analysis or architecture, say so and hand off rather than doing it at low quality.",
}
CATALOG_PRIORITY = {"architecture": 10, "implementation": 15, "review": 40, "completion": 30}


def regen_catalog(models):
    entries = []
    prio = 10
    for name, info in models.items():
        backend = backend_of(info)
        num_ctx = ctx_of(info)
        role = info.get("role", name)
        instr = "\n\n".join([CATALOG_PREAMBLE,
                             CATALOG_ROLE_SENTENCE.get(name, f"You are the {role} capability."),
                             CATALOG_TOOLING])
        entries.append({
            "slug": mn(name),
            "display_name": f"{role} ({backend})",
            "description": f"{role} — {backend}, {num_ctx // 1024}K ctx",
            "base_instructions": instr,
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": CATALOG_PRIORITY.get(name, prio),
            "context_window": num_ctx,
            "max_context_window": num_ctx,
            "effective_context_window_percent": 90,
            "input_modalities": ["text"],
            "supports_parallel_tool_calls": not bool(info.get("reasoning")),
            "supports_search_tool": False,
            "supports_image_detail_original": False,
            "supports_reasoning_summaries": False,
            "support_verbosity": False,
            "apply_patch_tool_type": "freeform",
            "web_search_tool_type": "text",
            "experimental_supported_tools": [],
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Fast, lighter reasoning"},
                {"effort": "medium", "description": "Balanced speed and depth"},
                {"effort": "high", "description": "Deep reasoning for complex problems"},
            ],
            "service_tiers": [],
            "truncation_policy": {"mode": "tokens", "limit": 10000},
        })
        prio += 5
    stage(CODEX_CATALOG, json.dumps(
        {"//": ["Generated by ailocal. Do not edit.",
                "Source: profiles/<active tier>.toml + profiles/clients.toml"],
         "models": entries}, indent=2) + "\n")
    return True


# ── configure.zsh: the claude-local built-in-slot env block ───────────────────
# These decide which backend Claude Code's built-in tiers resolve to (and any
# subagent whose frontmatter names one). Generated from clients.toml, because a
# hand-maintained copy drifts from the profile it claims to describe.
SLOT_ENV = {
    "opus":   "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku":  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable":  "ANTHROPIC_DEFAULT_FABLE_MODEL",
}




def gen_slot_block(clients):
    lines = [CS_BEGIN, "  slots=("]
    for slot, cap in (clients.get("claude") or {}).get("slots", {}).items():
        var = SLOT_ENV.get(slot)
        if not var:
            warn(f"unknown claude slot '{slot}' — no ANTHROPIC_DEFAULT_* var; skipping")
            continue
        lines.append(f'    {var}="{mn(cap)}"')
    lines += ["  )", CS_END]
    return "\n".join(lines) + "\n"



def regen_configure_zsh(clients):
    if not CONFIGURE_ZSH_TPL.exists():
        return False
    text = CONFIGURE_ZSH_TPL.read_text()
    stage(CONFIGURE_ZSH, splice(text, CS_BEGIN, CS_END, gen_slot_block(clients),
                                "claude slots"))
    return True


def regen_claude_settings(models, clients):
    """settings.json is CO-OWNED: Claude Code writes to it too.

    `claude plugin enable` adds `enabledPlugins` to this exact file, so a
    generator that rewrites it from the template alone silently disables the
    Python LSP baseline on every `ailocal start`. The shipped template supplies
    the defaults, anything already on disk wins over the template, and only the
    keys ailocal actually owns — `model`, `//`, and the two compaction env
    vars — are then overwritten.
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
            # `env` is a mapping both sides contribute to, so it merges rather
            # than being replaced wholesale by whichever side is read last.
            if isinstance(data.get("env"), dict) or isinstance(live.get("env"), dict):
                merged["env"] = {**(data.get("env") or {}), **(live.get("env") or {})}
            data = merged
    default = (clients.get("claude") or {}).get("launch_default", next(iter(models)))
    caps = " | ".join(mn(k) for k in models.keys())
    slots = (clients.get("claude") or {}).get("slots", {})
    slot_txt = ", ".join(f"{k.title()}->{mn(v)}" for k, v in slots.items())
    data["model"] = mn(default)
    data["//"] = [
        "Claude Code settings — deployed to ~/.config/ailocal/claude/settings.json",
        "(CLAUDE_CONFIG_DIR for the local variant; ~/.claude is never touched).",
        "",
        "Generated by ailocal. Source: profiles/clients.toml + profiles/<tier>.toml.",
        "Base URL + key are injected per-process by the claude-local() wrapper.",
        "",
        "All requests route through LiteLLM by ailocal-<capability> model id.",
        f"Models: {caps}",
        f"Launch default = {mn(default)}. At runtime /model lists every model (the wrapper sets",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1, so Claude Code GETs /v1/models).",
        f"Built-in slots remap: {slot_txt}.",
        "Launch with: claude-local",
    ]
    # Client-native compaction. ailocal does NOT summarise conversations and holds
    # no conversation state; Claude Code does the compacting. What ailocal owns is
    # the THRESHOLD, and it sets one earlier than a hosted model would use because
    # a cold local prefill costs real time. The per-tier numbers and the runner
    # they were measured against live in each profile's [compaction] block --
    # deliberately not restated here, because a second copy is what let the
    # previous figures (85 s at 28K, 341 s at 58K, 789 s at 88K) outlive the
    # llama_cpp backend that produced them.
    # The model maximum is unchanged and still available for one-shot work.
    win, pct = COMPACTION.get("window"), COMPACTION.get("pct")
    if win and pct:
        data.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(win)
        data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(pct)
        data["//"].append(
            f"Auto-compaction: window {win} x {pct}% = {int(win)*int(pct)//100} tokens "
            f"(profile-owned). NOT the model limit - architecture keeps its full context.")
    # Output ceiling, from the SAME max_output the geometry already hands
    # LiteLLM as num_predict. Without it the two ends disagreed:
    # [REAL] Claude Code 2.1.224 sends max_tokens 32000 by default (captured
    # against a local endpoint that logs the request), while the launch-default
    # role serves num_predict = max_output = 16384. Every answer long enough to
    # reach the ceiling therefore came back stop_reason=max_tokens, which the
    # client reads as a turn it should continue rather than the backend's
    # limit — so it re-asked, and a single response spent ~27 minutes emitting
    # 16384-token chunks before Claude Code's own 32000 accumulator aborted it
    # with "response exceeded the 32000 output token maximum". The abort was the
    # only thing that stopped it.
    #
    # Projecting max_output makes the client ask for exactly what the backend
    # will produce, so the ceiling is reached once, deliberately, instead of
    # being discovered by truncation. This does NOT make a verbose model concise:
    # it makes an over-long answer fail in one generation instead of six.
    #
    # Derived, never configured — a second number here would drift from the
    # profile the way the retired figures above did.
    out_geom = _geom(models[default])
    if not out_geom["max_output"]:
        raise SystemExit(
            f"invalid geometry: claude launch_default '{default}' declares no "
            "max_output, so there is no knowable output ceiling to project")
    data.setdefault("env", {})["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(out_geom["max_output"])
    data["//"].append(
        f"Output ceiling: CLAUDE_CODE_MAX_OUTPUT_TOKENS = {out_geom['max_output']} "
        f"(= {default}.max_output, the same number LiteLLM gets as num_predict). "
        "Claude Code defaults to 32000, which the backend does not serve.")
    # Retire keys ailocal used to write. `env` merges live-over-template so that
    # Claude Code's own additions survive regeneration — which also means simply
    # deleting a key from the template never reaches an existing install: it
    # would sit in every settings.json already on disk forever. Removal has to be
    # stated, not implied.
    for key, why in RETIRED_CLAUDE_ENV.items():
        if key in (data.get("env") or {}):
            data["env"].pop(key)
            data["//"].append(f"Removed {key}: {why}")
    if data.get("env") == {}:
        data.pop("env")
    stage(CLAUDE_SETTINGS, json.dumps(data, indent=2) + "\n")
    return True


# ── Codex config.toml + profiles ───────────────────────────────────────────────
def _set_toml_model(path, model, template=None):
    """Render a Codex profile template to its runtime destination."""
    src = template if template is not None else path
    if not src.exists():
        return False
    text = MARKER_TOML + src.read_text()
    if re.search(r'(?m)^model\s*=', text):
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{model}"', text, count=1)
    else:
        text = f'model = "{model}"\n' + text
    stage(path, text)
    return True


#: `[projects."<path>"]` blocks, which CODEX writes into this file itself.
_CODEX_PROJECTS = re.compile(
    r'(?ms)^\[projects\.".*?"\]\n(?:(?!^\[).*?\n)*')


def _keep_codex_projects(text: str) -> str:
    """Carry Codex's own trust records across regeneration.

    config.toml is CO-OWNED, the same way claude/settings.json is. Codex appends

        [projects."/path/to/repo"]
        trust_level = "trusted"

    when you approve a directory, and a generator that rebuilds this file from
    the template alone deletes it — so every `ailocal start` silently un-trusts
    every directory the user had approved, and the next session asks again.
    [REAL] observed as gate drift after one `codex exec` run.

    ONLY `[projects.*]`. Everything else in this file is ailocal's and must be
    rewritten from the template; preserving more would let a stale hand edit
    outlive the source it was generated from.
    """
    if not CODEX_CONFIG.exists():
        return text
    try:
        live = CODEX_CONFIG.read_text()
    except OSError:
        return text
    blocks = [b for b in _CODEX_PROJECTS.findall(live) if b.strip()
              and b.split("\n", 1)[0] not in text]
    if not blocks:
        return text
    return text.rstrip("\n") + "\n\n" + "".join(blocks).rstrip("\n") + "\n"


def regen_codex(models, clients):
    cx = clients.get("codex", {})
    default = cx.get("default", next(iter(models)))
    profiles = cx.get("profiles", {})
    done = False
    if CODEX_TPL.exists():
        text = (MARKER_TOML + CODEX_TPL.read_text()
                ).replace("${CODEX_HOME}", str(CODEX_HOME))
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{mn(default)}"', text, count=1)
        text = re.sub(r'(?m)^# Valid models:.*$',
                      "# Valid models: " + " | ".join(mn(k) for k in models.keys()), text, count=1)
        stage(CODEX_CONFIG, text)
        done = True
        # Codex's own compactor, same policy as claude-local. These keys must be
        # TOP-LEVEL: Codex does not reliably honour them inside a named profile.
        # Both are derived from the capability CODEX DEFAULTS TO, never from
        # architecture: a window describing a different model makes compaction
        # unreachable, because the backend 400s on context length first.
        win, pct = COMPACTION.get("window"), COMPACTION.get("pct")
        _cx = _geom(models.get(default) or {})
        cx_ctx = _cx["total_context"]        # the window Codex advertises
        cx_in = _cx["context_input"]         # what the backend will ADMIT
        if not (win and pct and cx_ctx and cx_in):
            raise SystemExit(
                "codex compaction cannot be derived: "
                f"window={win!r} pct={pct!r} total_context={cx_ctx!r} "
                f"context_input={cx_in!r}")
        # The trigger is capped by context_input, NOT total_context: the output
        # half of the window is space the input can never occupy, so a fraction
        # of the total can still exceed the admission limit and take an HTTP 400
        # before Codex ever compacts.
        trigger = min(int(win) * int(pct) // 100, int(int(cx_in) * int(pct) / 100))
        if trigger > int(cx_in):
            raise SystemExit(
                "codex compaction trigger exceeds admissible input: "
                f"trigger={trigger} context_input={cx_in}")
        text = _staged_text(CODEX_CONFIG)
        for key, val in (("model_context_window", cx_ctx),
                         ("model_auto_compact_token_limit", trigger)):
            if re.search(rf'(?m)^{key}\s*=', text):
                text = re.sub(rf'(?m)^{key}\s*=.*$', f'{key} = {val}', text, count=1)
            else:
                text = f'{key} = {val}\n' + text
        text = _keep_codex_projects(text)
        stage(CODEX_CONFIG, text)
    if "plan" in profiles:
        _set_toml_model(CODEX_PLAN, mn(profiles["plan"]), CODEX_PLAN_TPL)
    if "review" in profiles:
        _set_toml_model(CODEX_REVIEW, mn(profiles["review"]), CODEX_REVIEW_TPL)
    return done



def parse_profile_flag(argv):
    """Pull `--profile <tier>` out of argv, returning (tier_or_None, remaining_argv)."""
    tier, rest, i = None, [], 0
    while i < len(argv):
        if argv[i] == "--profile" and i + 1 < len(argv):
            tier = argv[i + 1]; i += 2
        else:
            rest.append(argv[i]); i += 1
    return tier, rest


# ── atomic generation ───────────────────────────────────────────────────────
# Outputs are STAGED and only replaced once every generator has succeeded. A
# partial generation is worse than no generation: LiteLLM would serve one
# profile's aliases while the clients pointed at another's, and nothing would
# report a problem. Nothing here is replaced if any step raises.
_STAGE: dict = {}
_STAGED_TEXT: dict = {}

MARKER_TOML = ("# Generated by ailocal. Do not edit.\n"
               "# Source: profiles/<tier>.toml\n"
               "# Owner: DevelopSolutions, LLC\n")


def _staged_text(path):
    """Text already staged for a destination, else what is on disk."""
    return _STAGED_TEXT.get(Path(path), Path(path).read_text() if Path(path).exists() else "")


def stage(path, text):
    """Record an output. Written only by flush_stage()."""
    _STAGE[Path(path)] = text
    _STAGED_TEXT[Path(path)] = text


def flush_stage():
    """Write every staged output, then swap them in with rollback on failure.

    THE GUARANTEE: per-file atomic, with rollback on partial failure. Not a
    transaction -- os.replace() is atomic PER FILE -- but a failure part-way
    through restores every destination already replaced, so the tree ends up
    entirely old or entirely new. There is no commit marker to consult: LiteLLM
    and the clients read their generated files directly, so a partial run leaves
    mixed state that is servable (tests/generation-rollback.py proves it by
    fault injection). Every temp file is written and validated before any
    destination is touched."""
    tmp, backups, replaced = {}, {}, []
    try:
        for path, text in _STAGE.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            t = path.with_suffix(path.suffix + ".tmp-sync")
            t.write_text(text)
            tmp[path] = t
        # Validate BEFORE any destination is touched: an artifact that cannot be
        # parsed back must not replace a good one.
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



def main(argv=None):
    tier, args = parse_profile_flag(sys.argv[1:] if argv is None else list(argv))

    check_only = bool(args) and args[0] == "--check"
    if check_only:
        args = args[1:]

    # The TEMPLATE is the precondition. config.yaml is this script's output, so
    # requiring it to pre-exist made a fresh clone unable to bootstrap.
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
    ok("claude/settings.json") if regen_claude_settings(models, clients) else warn("claude settings skipped")
    ok("configure.zsh (claude slots)") if regen_configure_zsh(clients) else warn("configure.zsh slots skipped")
    ok("integration-contract.json (for Cadence)") if write_integration_contract(models) else warn("contract skipped")
    ok("codex config + profiles") if regen_codex(models, clients) else warn("codex skipped")
    if check_only:
        # generated_at is a timestamp: two identical generations differ by it,
        # so it is normalised away rather than reported as drift every run.
        def _stable(t):
            return re.sub(r'"generated_at":\s*"[^"]*"', '"generated_at": ""', t)

        def _label(d):
            for base in (_pc.state_root(), _pc.config_root(), _pc.data_root()):
                try:
                    return str(d.relative_to(base))
                except ValueError:
                    continue
            return str(d)

        drift = sorted(_label(d) for d, text in _STAGE.items()
                       if not d.exists() or _stable(d.read_text()) != _stable(text))
        # --check writes nothing, so the staging tables must not survive into a
        # later call in the same process.
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
