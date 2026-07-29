#!/usr/bin/env python3
"""sync-models.py — propagate the capability registry to every derived file.

Single source of truth (both TRACKED — no gitignored intermediate):
  config/profiles/<tier>.yaml  WHAT each capability is (backend `active`, context, sampling,
                        keep_alive, persona, decision metadata), per RAM tier. Edit the profile
                        directly. The active tier is config/active-profile (machine-specific,
                        written by install.sh from detected RAM) or `--profile <tier>`; default 64gb.
  config/clients.yaml   WHICH capability each client surface uses (launch defaults, Codex
                        profiles, Continue entries, compat aliases).

Running ./scripts/sync-models.sh regenerates, deterministically:
  config/litellm/config.yaml         model_list + model_group_alias (between markers)
  config/capabilities.generated.json resolved capabilities (for `ailocal status`)
  config/clients/model_catalog.json  Codex picker (capability slugs)
  config/clients/claude/settings.json launch default + valid-capability note
  config/clients/codex/config.toml    default model + valid-capability note
  config/clients/codex/{plan,review}.config.toml  profile models
  config/clients/continue/config.json chat models, FIM autocomplete, embeddings

Also: `sync-models.py --resolve <capability>` prints the active Ollama backend tag, so shell
scripts (setup-startup.sh, preload-model.sh) resolve without parsing YAML themselves.

Capabilities are TOP-LEVEL keys in the profile; lists are flow-style [a, b, c]. keep_alive
accepts durations, -1, or the words forever/persistent (both -> -1). Never hand-edit a generated
region — edit config/profiles/<tier>.yaml / config/clients.yaml and re-run.
"""

import collections
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR   = ROOT / "config/profiles"
ACTIVE_PROFILE = ROOT / "config/active-profile"   # one line, e.g. "64gb" (machine-specific)
CLIENTS_YAML   = ROOT / "config/clients.yaml"
LITELLM_CONFIG = ROOT / "config/litellm/config.yaml"
CAPS_JSON      = ROOT / "config/capabilities.generated.json"
CODEX_CATALOG  = ROOT / "config/clients/model_catalog.json"
CLAUDE_SETTINGS= ROOT / "config/clients/claude/settings.json"
CODEX_CONFIG   = ROOT / "config/clients/codex/config.toml"
CODEX_PLAN     = ROOT / "config/clients/codex/plan.config.toml"
CODEX_REVIEW   = ROOT / "config/clients/codex/review.config.toml"
CONTINUE_CONFIG= ROOT / "config/clients/continue/config.json"
CONFIGURE_ZSH  = ROOT / "config/clients/configure.zsh"
COPILOT_REPO_MD= ROOT / "config/clients/copilot/repo-instructions.md"

# The machine-readable seam with Cadence. Cadence reads THIS and nothing else to
# learn about the local runtime — see write_integration_contract(). ailocal does
# NOT own client instruction policy; Cadence composes it from this contract.
CONTRACT_JSON  = ROOT / "config/integration-contract.json"
BASE_URL       = "http://localhost:4000"


ML_BEGIN = "  # >>> BEGIN GENERATED model_list (sync-models.py) — do not edit <<<"
ML_END   = "  # >>> END GENERATED model_list <<<"
AL_BEGIN = "  # >>> BEGIN GENERATED model_group_alias (sync-models.py) — do not edit <<<"
AL_END   = "  # >>> END GENERATED model_group_alias <<<"
CP_BEGIN = "<!-- >>> BEGIN GENERATED capabilities (sync-models.py) — do not edit <<< -->"
CP_END   = "<!-- >>> END GENERATED capabilities <<< -->"
CS_BEGIN = "  # >>> BEGIN GENERATED claude slots (sync-models.py) — do not edit <<<"
CS_END   = "  # >>> END GENERATED claude slots <<<"

# Capabilities are short keys in the source (config/profiles/<tier>.yaml + config/clients.yaml);
# every client-facing model id is that key with an `ailocal-` prefix, applied only at emit time.
# One canonical model_list entry per capability (`ailocal-<cap>`) — no `local/*` duplicate.
MODEL_PREFIX = "ailocal-"
def mn(cap):
    """Client-facing model id for a capability (the ailocal- prefixed name LiteLLM serves)."""
    return f"{MODEL_PREFIX}{cap}"


def step(m): print(f"\n▶ {m}")
def ok(m):   print(f"  ✓ {m}")
def warn(m): print(f"  ⚠ {m}", file=sys.stderr)


def truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def flow_list(v):
    """Parse a flow-style list "[a, b, c]" -> ["a","b","c"]; tolerate a bare scalar."""
    if v is None:
        return []
    v = str(v).strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    return [v] if v else []


def flow_dict(v):
    """Parse a flow-style map "{a: b, c: d}" -> {"a":"b","c":"d"}."""
    v = str(v).strip()
    out = {}
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        for pair in inner.split(","):
            if ":" in pair:
                k, _, val = pair.partition(":")
                out[k.strip()] = val.strip()
    return out


# ── profile resolution ─────────────────────────────────────────────────────────
def resolve_tier(explicit=None):
    """Which RAM profile is active: an explicit --profile wins; else the tracked-intent
    config/active-profile marker (written by install.sh from detected RAM); else 64gb."""
    if explicit:
        return explicit
    if ACTIVE_PROFILE.exists():
        t = ACTIVE_PROFILE.read_text().strip()
        if t:
            return t
    return "64gb"


def profile_path(tier=None, explicit=None):
    p = PROFILES_DIR / f"{resolve_tier(explicit) if tier is None else tier}.yaml"
    if not p.exists():
        print(f"Error: profile not found: {p}", file=sys.stderr)
        sys.exit(1)
    return p


# ── config loading ────────────────────────────────────────────────────────────
def load_models_yaml(path):
    """Read a profile (config/profiles/<tier>.yaml) into an ordered dict:
    capability -> {field: scalar-string}. List fields stay as their raw "[a, b, c]"
    string; use flow_list() at the point of use. Top-level scalars (disk_gb, status,
    profile) are ignored — only indented `key: value` under a capability is captured."""
    models, current = {}, None
    for line in Path(path).read_text().splitlines():
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        if not s.startswith(" ") and s.endswith(":"):
            current = s[:-1].strip()
            models[current] = {}
        elif current and ":" in s and s.startswith(" "):
            k, _, v = s.strip().partition(":")
            models[current][k.strip()] = v.split("#", 1)[0].strip()
    models.pop("disk_gb", None)
    return models


def load_clients_yaml():
    """Two-level: section -> {key: scalar | list | dict}."""
    data, section = {}, None
    if not CLIENTS_YAML.exists():
        return data
    for line in CLIENTS_YAML.read_text().splitlines():
        s = line.rstrip()
        if not s or s.lstrip().startswith("#"):
            continue
        if not s.startswith(" ") and s.endswith(":"):
            section = s[:-1].strip()
            data[section] = {}
        elif section is not None and ":" in s and s.startswith(" "):
            k, _, v = s.strip().partition(":")
            # Strip an inline "# comment" from scalar values (flow [..]/{..} never contain one),
            # so a documentation comment can't leak into a generated alias value.
            v = v.strip()
            if not v.startswith(("[", "{")):
                v = v.split("#", 1)[0].strip()
            if v.startswith("["):
                data[section][k.strip()] = flow_list(v)
            elif v.startswith("{"):
                data[section][k.strip()] = flow_dict(v)
            else:
                data[section][k.strip()] = v
    return data


def backend_of(info):
    """The Ollama backend tag actually served: `active`, else `backend`, else first
    `preferred`. Never hidden — this is the real model that runs."""
    if info.get("active"):
        return info["active"]
    if info.get("backend"):
        return info["backend"]
    pref = flow_list(info.get("preferred"))
    return pref[0] if pref else ""


def norm_keep_alive(v):
    """forever/persistent -> -1; else pass through (durations like 2h/60m, or -1)."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    return "-1" if s.lower() in ("forever", "persistent") else s


def ctx_of(info):
    return int(info.get("context") or info.get("num_ctx") or info.get("context_window") or 32768)


# ── LiteLLM model_list ─────────────────────────────────────────────────────────
def gen_role_block(role, info):
    num_ctx = ctx_of(info)
    backend = backend_of(info)
    ka = norm_keep_alive(info.get("keep_alive"))

    if role == "embeddings" or truthy(info.get("embedding", "false")) or backend.startswith("nomic") or "embed" in role:
        # `ollama`, NOT `ollama_chat`. LiteLLM has no embeddings route for the
        # ollama_chat provider: an /v1/embeddings call against it fails with
        # "Unmapped LLM provider for this endpoint. You passed
        # model=nomic-embed-text, custom_llm_provider=ollama_chat".
        #
        # Verified empirically in the 1.93.0 image, not inferred from docs:
        #   litellm.embedding(model="ollama_chat/nomic-embed-text") -> BadRequest
        #   litellm.embedding(model="ollama/nomic-embed-text")      -> OK, 768 dims
        #
        # This shipped broken: ailocal-embeddings was advertised in /v1/models and
        # 400ed on every call. Cadence was unaffected because it talks to Ollama
        # directly, which is why nobody noticed — a client configured to use the
        # proxy for embeddings (Continue) would have failed.
        lines = [
            f"  - model_name: {mn(role)}",
            f"    litellm_params:",
            f"      model: ollama/{backend}",
            f"      api_base: os.environ/OLLAMA_URL",
            f"      num_ctx: {num_ctx}",
        ]
        if ka is not None:
            lines.append(f"      keep_alive: {ka}")
        lines += [
            f"    model_info:",
            f"      mode: embedding",
            f"      max_tokens: {num_ctx}",
            f"      input_cost_per_token: 0",
            f"      output_cost_per_token: 0",
            # Same reason as the chat branch: without these LiteLLM logs
            # "not in built-in cost map ... cache cost fields will default to 0"
            # at boot. Zero is the truthful value for a local model, not invented
            # pricing. Cost accounting only; no effect on routing or inference.
            f"      cache_creation_input_token_cost: 0",
            f"      cache_read_input_token_cost: 0",
        ]
        return "\n".join(lines) + "\n"

    reasoning = truthy(info.get("reasoning", "false"))
    merge     = truthy(info.get("merge", "false"))
    vision    = truthy(info.get("vision", "false"))
    parallel  = not reasoning
    max_out   = min(16384, max(1024, num_ctx // 4))
    desc      = info.get("role", "")

    params = [
        f"  - model_name: {mn(role)}",
        f"    litellm_params:",
        f"      model: ollama_chat/{backend}",
        f"      api_base: os.environ/OLLAMA_URL",
        f"      num_ctx: {num_ctx}",
    ]
    # repeat_penalty is OLLAMA's option name; repetition_penalty is LiteLLM's.
    # Only the former reaches the backend, so both are forwarded and the profile
    # decides which to set. A value of 1.0 means NO penalty and is set
    # explicitly rather than relying on a backend default.
    for key in ("temperature", "top_p", "top_k", "repetition_penalty",
                "repeat_penalty", "num_predict"):
        if info.get(key) not in (None, ""):
            params.append(f"      {key}: {info[key]}")
    if ka is not None:
        params.append(f"      keep_alive: {ka}")
    if merge:
        params.append("      merge_reasoning_content_in_choices: true")
    if not reasoning:
        # Suppress reasoning ONLY for models that cannot do it.
        #
        # This was unconditionally correct when no installed model could think:
        # Claude Code sends `thinking` on every request and a non-thinking
        # backend 400s on it, and a backend defaulting to reasoning hung VS Code.
        #
        # It is now WRONG for qwen3.5, qwen3.6 and gpt-oss, which all report
        # `thinking` in their Ollama capabilities. Emitting think:false for those
        # would suppress the single biggest capability the migration buys. The
        # `reasoning` flag in config/profiles/<tier>.yaml now drives this per
        # capability rather than applying to everything.
        params.append('      additional_drop_params: ["thinking", "reasoning_effort"]')
        params.append("      think: false")
    else:
        # A thinking-capable model: do NOT drop the client's thinking params and
        # do NOT force think:false. Verified against Ollama /api/show — the
        # capability list is the source of truth, not the model name.
        params.append("      think: true")

    mi = [
        f"    model_info:",
        f"      supports_function_calling: true",
        f"      supports_tool_choice: true",
        f"      supports_parallel_function_calling: {'true' if parallel else 'false'}",
        f"      supports_system_messages: true",
        f"      supports_native_streaming: true",
        f"      supports_reasoning: {'true' if reasoning else 'false'}",
        # Local models are free. Without these four fields LiteLLM's cost layer
        # logs "not in built-in cost map ... cache cost fields will default to 0"
        # for every model at boot. Purely cosmetic — it affects cost accounting
        # only, never routing, tools, MCP, LSP or inference — but it buries real
        # warnings in noise. Zeros are the truthful value here: nothing is billed.
        f"      input_cost_per_token: 0",
        f"      output_cost_per_token: 0",
        f"      cache_creation_input_token_cost: 0",
        f"      cache_read_input_token_cost: 0",
    ]
    if vision:
        mi += ["      supports_vision: true", "      supports_pdf_input: true"]
    # The cost zeros are emitted ONCE, above. They used to be repeated here,
    # producing duplicate YAML keys in every model_info block: harmless in
    # effect (both values were 0, and the later key wins) but a strict YAML
    # parser rejects the file outright, and it made validate-deployment.sh fail
    # on a defect that had nothing to do with the deployment.
    mi += [
        f"      max_input_tokens: {num_ctx}",
        f"      max_output_tokens: {max_out}",
    ]
    header = f"  # {mn(role)} — {desc} ({backend})\n" if desc else ""
    return header + "\n".join(params) + "\n" + "\n".join(mi) + "\n"


def gen_model_list(models):
    blocks = [gen_role_block(r, i) for r, i in models.items()]
    return ML_BEGIN + "\n\n" + "\n".join(blocks) + "\n" + ML_END + "\n"


def gen_alias_block(models, clients):
    """model_group_alias YAML from clients.yaml: the external client-compat names (claude-*/gpt-*)
    that Claude Code and the OpenAI SDK hard-code, each pointing at its `ailocal-<cap>` model group.
    No `local/*` namespace — the single canonical `ailocal-<cap>` model_list entry is the only name.
    Compat names inherit the target group's persona/settings."""
    lines = [AL_BEGIN, "  model_group_alias:"]
    for name, cap in clients.get("compat", {}).items():
        lines.append(f"    {name}: {mn(cap)}")
    lines.append(AL_END)
    return "\n".join(lines) + "\n"


def splice(text, begin, end, generated, label):
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    if not pat.search(text):
        warn(f"markers not found for {label}; skipping")
        return text, False
    return pat.sub(lambda _m: generated, text, count=1), True


def regen_litellm(models, clients):
    text = LITELLM_CONFIG.read_text()
    text, s1 = splice(text, ML_BEGIN, ML_END, gen_model_list(models), "model_list")
    text, s2 = splice(text, AL_BEGIN, AL_END, gen_alias_block(models, clients), "model_group_alias")
    LITELLM_CONFIG.write_text(text)
    return s1 and s2


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
    CAPS_JSON.write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "capabilities": caps}, indent=2) + "\n")
    return True


def write_integration_contract(models):
    """The ONLY surface Cadence reads to learn about this runtime.

    Cadence owns client instruction policy; ailocal owns the runtime. The seam
    between them is this file and nothing else — Cadence must never parse
    ailocal's prose or generated Markdown to discover a fact, because that
    couples a policy generator to our formatting.

    So this publishes FACTS ONLY: where the roots are, what the endpoint is,
    which capability names are canonical, and what is measurably true about
    routes that pass through the proxy. No policy, no instructions, no prose
    telling an agent how to behave.

    `schema_version` is load-bearing: Cadence fails CLOSED on a version it does
    not understand rather than guessing at fields whose meaning may have moved.
    Bump it on any breaking change to shape or field semantics.

    No timestamp — the file must be byte-stable so idempotence is hash-checkable.
    """
    contract = {
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
        "compatibility": {
            "claude_native_lsp": {
                "configured": True,
                # The gateway names native `LSP` explicitly (registry group
                # `native_lsp`, in the `always` floor) rather than relying on
                # fail-open, so the schema reaches the model on every task class
                # that keeps a floor.
                "schema_preserved": True,
                "execution": "failing",
            },
            "codex_mcp_lsp": {
                "configured": True,
                "schema_preserved": False,
                # Codex declares MCP servers as namespace BUNDLES, which LiteLLM
                # discards before the backend; flattening them makes Codex's own
                # dispatcher refuse the call (openai/codex#20652).
                "execution": "blocked_namespace_dispatch",
            },
        },
    }
    CONTRACT_JSON.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")
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
    if not CODEX_CATALOG.exists():
        return False
    entries = []
    prio = 10
    for name, info in models.items():
        if name == "embeddings":
            continue
        backend = backend_of(info)
        num_ctx = ctx_of(info)
        vision = truthy(info.get("vision", "false"))
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
            "input_modalities": ["text", "image"] if vision else ["text"],
            "supports_parallel_tool_calls": not truthy(info.get("reasoning", "false")),
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
    CODEX_CATALOG.write_text(json.dumps({"models": entries}, indent=2) + "\n")
    return True


# ── Claude settings.json ───────────────────────────────────────────────────────
# ── configure.zsh: the claude-local built-in-slot env block ───────────────────
# These four vars are what actually decide which backend Claude Code's built-in
# tiers (and any subagent whose frontmatter says `model: haiku|sonnet|opus|fable`)
# resolve to. They were hand-maintained and drifted out of sync with clients.yaml,
# which is why haiku pointed at the 4096-token FIM tier and fable was absent.
SLOT_ENV = {
    "opus":   "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku":  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable":  "ANTHROPIC_DEFAULT_FABLE_MODEL",
}


def check_conversational_slots(models, clients):
    """`completion` is FIM-only at num_ctx 4096; any real agent turn routed there
    hard-400s. Every built-in Claude slot carries full conversation context, so
    none of them may point at it. Fail loudly at generation time rather than
    letting the breakage surface as a runtime 400."""
    slots = (clients.get("claude") or {}).get("slots", {})
    bad = [s for s, cap in slots.items() if cap == "completion"]
    if bad:
        sys.exit(f"error: claude.slots {bad} -> 'completion' (FIM tier, num_ctx "
                 f"{ctx_of(models.get('completion', {}))}). Conversational slots "
                 f"must not use it; see CLAUDE.md. Fix config/clients.yaml.")

    # Two slots on one capability is legal but shows up as a DUPLICATE entry in
    # Claude Code's /model picker (gateway discovery lists the capability once
    # per slot pointing at it), and it wastes a tier. Warn rather than fail:
    # it is a papercut, not a breakage, and a deliberate collapse may be wanted.
    dupes = [c for c, n in collections.Counter(slots.values()).items() if n > 1]
    for cap in dupes:
        owners = sorted(s for s, v in slots.items() if v == cap)
        warn(f"claude.slots {owners} all map to '{cap}' — /model will list "
             f"{mn(cap)} {len(owners)}x. Give each slot its own capability.")


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


def gen_copilot_capabilities(models):
    """The capability table for the VS Code Copilot repo instructions.

    Generated because the hand-written version drifted badly: by 2026-07-28 four
    of six rows were wrong — `review` had been gpt-oss:20b for weeks while the
    file still said deepseek-coder-v2, keep_alive values were stale, and the
    `fast` tier was missing entirely. A client instruction file that restates
    generated truth always drifts, because nothing regenerates it. Now something
    does."""
    lines = [CP_BEGIN,
             "",
             "Use **capability names** only — never a backend model tag. The router owns",
             "context, sampling and residency, so a model swap never touches this file.",
             "",
             "| Capability | Backend | Context | keep_alive |",
             "|---|---|---|---|"]
    for name, info in models.items():
        lines.append(f"| `{mn(name)}` | {backend_of(info)} | {ctx_of(info)} | "
                     f"{norm_keep_alive(info.get('keep_alive'))} |")
    lines += ["",
              "`ailocal-completion` is FIM autocomplete **only** — it hard-400s on a chat",
              "turn. Cold-loading a model costs a few seconds; the dominant latency is",
              "prompt evaluation, not loading (docs/adr/013-latency-profile.md).",
              CP_END]
    return "\n".join(lines) + "\n"


def regen_copilot_repo_md(models):
    if not COPILOT_REPO_MD.exists():
        return False
    text = COPILOT_REPO_MD.read_text()
    text, spliced = splice(text, CP_BEGIN, CP_END, gen_copilot_capabilities(models),
                           "copilot capabilities")
    COPILOT_REPO_MD.write_text(text)
    return spliced


def regen_configure_zsh(clients):
    if not CONFIGURE_ZSH.exists():
        return False
    text = CONFIGURE_ZSH.read_text()
    text, spliced = splice(text, CS_BEGIN, CS_END, gen_slot_block(clients), "claude slots")
    CONFIGURE_ZSH.write_text(text)
    return spliced


def regen_claude_settings(models, clients):
    if not CLAUDE_SETTINGS.exists():
        return False
    data = json.loads(CLAUDE_SETTINGS.read_text())
    default = (clients.get("claude") or {}).get("launch_default", next(iter(models)))
    caps = " | ".join(mn(k) for k in models.keys())
    slots = (clients.get("claude") or {}).get("slots", {})
    slot_txt = ", ".join(f"{k.title()}->{mn(v)}" for k, v in slots.items())
    data["model"] = mn(default)
    data["//"] = [
        "Claude Code settings — deployed to ~/.config/ailocal/claude/settings.json",
        "(CLAUDE_CONFIG_DIR for the local variant; ~/.claude is never touched).",
        "",
        "GENERATED FIELDS ('model' + this note) — edit config/clients.yaml, run sync-models.sh.",
        "Base URL + key are injected per-process by the claude-local() wrapper.",
        "",
        "All requests route through LiteLLM by ailocal-<capability> model id.",
        f"Models: {caps}",
        f"Launch default = {mn(default)}. At runtime /model lists every model (the wrapper sets",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1, so Claude Code GETs /v1/models).",
        f"Built-in slots remap: {slot_txt}.",
        "Launch with: claude-local",
    ]
    CLAUDE_SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    return True


# ── Codex config.toml + profiles ───────────────────────────────────────────────
def _set_toml_model(path, model):
    if not path.exists():
        return False
    text = path.read_text()
    if re.search(r'(?m)^model\s*=', text):
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{model}"', text, count=1)
    else:
        text = f'model = "{model}"\n' + text
    path.write_text(text)
    return True


def regen_codex(models, clients):
    cx = clients.get("codex", {})
    default = cx.get("default", next(iter(models)))
    profiles = cx.get("profiles", {})
    done = False
    if CODEX_CONFIG.exists():
        text = CODEX_CONFIG.read_text()
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{mn(default)}"', text, count=1)
        text = re.sub(r'(?m)^# Valid models:.*$',
                      "# Valid models: " + " | ".join(mn(k) for k in models.keys()), text, count=1)
        CODEX_CONFIG.write_text(text)
        done = True
    if "plan" in profiles:
        _set_toml_model(CODEX_PLAN, mn(profiles["plan"]))
    if "review" in profiles:
        _set_toml_model(CODEX_REVIEW, mn(profiles["review"]))
    return done


# ── Continue config.json ───────────────────────────────────────────────────────
def regen_continue(models, clients):
    if not CONTINUE_CONFIG.exists():
        return False
    data = json.loads(CONTINUE_CONFIG.read_text())
    cont = clients.get("continue", {})
    chat = cont.get("chat", list(models.keys()))
    data["models"] = [
        {"title": f"{models.get(c, {}).get('role', c)} ({c})", "provider": "openai",
         "model": mn(c), "apiBase": "http://localhost:4000/v1", "apiKey": "__LITELLM_KEY__"}
        for c in chat
    ]
    ac = cont.get("autocomplete", "completion")
    ac_backend = backend_of(models.get(ac, {}))
    data["tabAutocompleteModel"] = {
        "title": f"Autocomplete — {ac_backend} (FIM, direct Ollama)",
        "provider": "ollama", "model": ac_backend, "apiBase": "http://localhost:11434",
    }
    emb = cont.get("embeddings", "embeddings")
    data["embeddingsProvider"] = {
        "provider": "openai", "model": mn(emb),
        "apiBase": "http://localhost:4000/v1", "apiKey": "__LITELLM_KEY__",
    }
    data["//"] = [
        "ailocal — VS Code Continue config. MANAGED/GENERATED — edit config/clients.yaml +",
        "config/profiles/<tier>.yaml + config/clients.yaml, run sync-models.sh. Deployed by",
        "install-clients.sh (vscode); __LITELLM_KEY__ substituted from .env at install.",
        "Chat/edit go through LiteLLM (4000); autocomplete hits Ollama (11434) directly for FIM.",
        "Models: " + " | ".join(mn(k) for k in models.keys()),
    ]
    CONTINUE_CONFIG.write_text(json.dumps(data, indent=2) + "\n")
    return True


# Copilot instruction files are hand-maintained prose (generating a table into them created
# duplication); they are NOT regenerated here. Update them by hand when capabilities change.

# ── resolve mode (for shell) ───────────────────────────────────────────────────
def resolve(role, tier=None):
    info = load_models_yaml(profile_path(explicit=tier)).get(role)
    if not info:
        print("", end="")
        return 1
    print(backend_of(info))
    return 0


def parse_profile_flag(argv):
    """Pull `--profile <tier>` out of argv, returning (tier_or_None, remaining_argv)."""
    tier, rest, i = None, [], 0
    while i < len(argv):
        if argv[i] == "--profile" and i + 1 < len(argv):
            tier = argv[i + 1]; i += 2
        else:
            rest.append(argv[i]); i += 1
    return tier, rest


def main():
    tier, args = parse_profile_flag(sys.argv[1:])

    if args and args[0] == "--resolve":
        sys.exit(resolve(args[1], tier) if len(args) >= 2 else 1)

    if not LITELLM_CONFIG.exists():
        print(f"Error: {LITELLM_CONFIG} not found", file=sys.stderr); sys.exit(1)

    path = profile_path(explicit=tier)
    step(f"Reading {path.relative_to(ROOT)} + config/clients.yaml")
    models = load_models_yaml(path)
    clients = load_clients_yaml()
    for role, info in models.items():
        print(f"  {role}: {backend_of(info)}  (ctx {ctx_of(info)}, keep_alive {norm_keep_alive(info.get('keep_alive'))})")

    check_conversational_slots(models, clients)

    step("Regenerating config/litellm/config.yaml (model_list + aliases)")
    ok("litellm config regenerated" if regen_litellm(models, clients) else "litellm config unchanged/skipped")

    step("Writing derived files")
    ok("capabilities.generated.json") if write_caps_json(models) else warn("caps json skipped")
    ok("model_catalog.json") if regen_catalog(models) else warn("catalog skipped")
    ok("claude/settings.json") if regen_claude_settings(models, clients) else warn("claude settings skipped")
    ok("configure.zsh (claude slots)") if regen_configure_zsh(clients) else warn("configure.zsh slots skipped")
    ok("integration-contract.json (for Cadence)") if write_integration_contract(models) else warn("contract skipped")
    ok("copilot repo-instructions (capabilities)") if regen_copilot_repo_md(models) else warn("copilot repo md skipped")
    ok("codex config + profiles") if regen_codex(models, clients) else warn("codex skipped")
    ok("continue/config.json") if regen_continue(models, clients) else warn("continue skipped")

    step("Done — restart LiteLLM (./scripts/start.sh) and re-run ./scripts/install-clients.sh "
         "to deploy the regenerated client configs.")


if __name__ == "__main__":
    main()
