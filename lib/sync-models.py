#!/usr/bin/env python3
"""sync-models.py — propagate the capability registry to every derived file.

Single source of truth (both TRACKED — no gitignored intermediate):
  profiles/<tier>.yaml  WHAT each capability is (backend `active`, context, sampling,
                        keep_alive, persona, decision metadata), per RAM tier. Edit the profile
                        directly. The active tier lives in $AILOCAL_STATE (machine-specific,
                        written by install.sh from detected RAM) or `--profile <tier>`. There is NO
                        default tier: an unresolvable marker is an error.
  profiles/clients.yaml   WHICH capability each client surface uses (launch defaults, Codex
                        profiles, Continue entries, compat aliases).

Running `ailocal sync` regenerates, deterministically:
  the generated config.yaml         model_list + model_group_alias (between markers)
  config/capabilities.generated.json resolved capabilities (for `ailocal status`)
  clients/model_catalog.json  Codex picker (capability slugs)
  clients/claude/settings.json launch default + valid-capability note
  clients/codex/config.toml    default model + valid-capability note
  clients/codex/{plan,review}.config.toml  profile models
  clients/continue/config.json chat models, FIM autocomplete, embeddings

Also: `sync-models.py --resolve <capability>` prints the active Ollama backend tag, so shell
scripts (setup-startup.sh, preload-model.sh) resolve without parsing YAML themselves.

Capabilities are TOP-LEVEL keys in the profile; lists are flow-style [a, b, c]. keep_alive
accepts durations, -1, or the words forever/persistent (both -> -1). Never hand-edit a generated
region — edit profiles/<tier>.yaml / profiles/clients.yaml and re-run.
"""

import collections
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR   = ROOT / "profiles"
# Interactive-compaction thresholds, read from the profile's `compaction:` block.
COMPACTION = {}

sys.path.insert(0, str(ROOT / "lib"))
import policy as _pc  # noqa: E402

ACTIVE_PROFILE = _pc.active_profile_path(ROOT)   # machine-selected tier
CLIENTS_YAML   = ROOT / "profiles/clients.yaml"
LITELLM_TEMPLATE = ROOT / "deploy/litellm/config.template.yaml"
_CLIENTS_OUT   = _pc.runtime_root() / "clients"
_LITELLM_OUT   = _pc.runtime_root() / "litellm"
LITELLM_CONFIG   = _LITELLM_OUT / "config.yaml"
CAPS_JSON      = _LITELLM_OUT / "capabilities.json"
CODEX_CATALOG  = _CLIENTS_OUT / "model_catalog.json"
CLAUDE_SETTINGS_TPL = ROOT / "clients/claude/settings.template.json"
CLAUDE_SETTINGS = _CLIENTS_OUT / "claude/settings.json"
CODEX_TPL        = ROOT / "clients/codex/config.template.toml"
CODEX_PLAN_TPL   = ROOT / "clients/codex/plan.config.template.toml"
CODEX_REVIEW_TPL = ROOT / "clients/codex/review.config.template.toml"
CODEX_CONFIG   = _CLIENTS_OUT / "codex/config.toml"
CODEX_PLAN     = _CLIENTS_OUT / "codex/plan.config.toml"
CODEX_REVIEW   = _CLIENTS_OUT / "codex/review.config.toml"
CONTINUE_CONFIG_TPL = ROOT / "clients/continue/config.template.json"
CONTINUE_CONFIG = _CLIENTS_OUT / "continue/config.json"
CONFIGURE_ZSH_TPL = ROOT / "clients/configure.template.zsh"
CONFIGURE_ZSH  = _CLIENTS_OUT / "configure.zsh"
COPILOT_REPO_TPL = ROOT / "clients/copilot/repo-instructions.template.md"
COPILOT_REPO_MD  = _CLIENTS_OUT / "copilot/repo-instructions.md"

# The machine-readable seam with Cadence. Cadence reads THIS and nothing else to
# learn about the local runtime — see write_integration_contract(). ailocal does
# NOT own client instruction policy; Cadence composes it from this contract.
CONTRACT_JSON  = _pc.runtime_root() / "integration-contract.json"
BASE_URL       = "http://localhost:4000"


ML_BEGIN = "  # >>> BEGIN GENERATED model_list (sync-models.py) — do not edit <<<"
ML_END   = "  # >>> END GENERATED model_list <<<"
AL_BEGIN = "  # >>> BEGIN GENERATED model_group_alias (sync-models.py) — do not edit <<<"
AL_END   = "  # >>> END GENERATED model_group_alias <<<"
CP_BEGIN = "<!-- >>> BEGIN GENERATED capabilities (sync-models.py) — do not edit <<< -->"
CP_END   = "<!-- >>> END GENERATED capabilities <<< -->"
CS_BEGIN = "  # >>> BEGIN GENERATED claude slots (sync-models.py) — do not edit <<<"
CS_END   = "  # >>> END GENERATED claude slots <<<"

# Capabilities are short keys in the source (profiles/<tier>.yaml + profiles/clients.yaml);
# every client-facing model id is that key with an `ailocal-` prefix, applied only at emit time.
# One canonical model_list entry per capability (`ailocal-<cap>`) — no `local/*` duplicate.
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

    Delegates to policy, which FAILS CLOSED. The previous version fell
    through to a hardcoded tier, so a missing or empty marker silently
    regenerated every client and the proxy for the wrong hardware."""
    if explicit:
        return explicit
    return _pc.resolve_active_tier(ROOT)


def profile_path(tier=None, explicit=None):
    p = PROFILES_DIR / f"{resolve_tier(explicit) if tier is None else tier}.yaml"
    if not p.exists():
        print(f"Error: profile not found: {p}", file=sys.stderr)
        sys.exit(1)
    return p


# ── config loading ────────────────────────────────────────────────────────────
def load_models_yaml(path):
    """Read a profile via the shared parser. Kept as a named function because the
    generator calls it in several places; the parsing is no longer local."""
    data = _pc.parse_profile_text(Path(path).read_text())
    models = {}
    for section, fields in data.items():
        if not isinstance(fields, dict):
            continue          # top-level scalars such as disk_gb
        # Typed values pass through as-is. flow_list() is idempotent and the
        # Scalar consumers coerce with int() at the point of use, so
        # no string round-trip is needed.
        models[section] = dict(fields)
    models.pop("disk_gb", None)
    COMPACTION.update(models.pop("compaction", {}))
    return models


def load_clients_yaml():
    """Client policy, from the one policy owner."""
    return _pc.load_client_policy()


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


def _int_or_none(v):
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None

def _geom(info):
    """Derived geometry for a role, from the ONE implementation.

    sync-models must not re-derive num_ctx / num_predict / admission. It reads
    context_input + max_output straight from the profile and hands them to
    policy.geometry(), which is the same function every other consumer
    calls. There is deliberately no default: a role missing context_input is a
    migration error, not a 32768 guess -- the old fallback silently rewrote
    every alias to 32768 when the schema changed.
    """
    return _pc.geometry(_int_or_none(info.get("context_input")),
                        _int_or_none(info.get("max_output")))


def ctx_of(info):
    """Total physical window (Ollama num_ctx). Derived, never configured."""
    return _geom(info)["num_ctx"]


# ── LiteLLM model_list ─────────────────────────────────────────────────────────

OUTPUT_RESERVE_UNBOUNDED = "OUTPUT_RESERVE_UNBOUNDED_UNSAFE_FOR_STRICT_ADMISSION"


def admission_for(role, num_ctx, num_predict):
    """How much INPUT this alias may admit, and why.

    num_ctx is the PHYSICAL window and it holds prompt AND generation. Advertising
    the whole window as admissible input is the defect measured in
    KNOWN_ISSUES #19: LiteLLM's pre-call check accepts a prompt that leaves no
    room for the reply, Ollama then trims from the FRONT, and the caller gets
    HTTP 200 with finish_reason=stop and a silently truncated prompt. Measured
    on qwen3.5:2b at num_ctx 40960: 43,645 tokens admitted -> 20,482 evaluated,
    system prompt gone, task instruction not followed.

    Returns (max_input_tokens, note). Invalid finite geometry RAISES rather than
    clamping: a clamp would hide exactly the misconfiguration this exists to
    surface.
    """
    if not isinstance(num_ctx, int) or num_ctx <= 0:
        raise SystemExit(f"invalid geometry: {role} num_ctx={num_ctx!r}")
    # num_predict -1 is Ollama's INFINITE and -2 is fill-context. Neither reserves
    # a knowable amount, so no arithmetic is possible and none is invented. The
    # deployed value is preserved and the role is MARKED, not silently treated as
    # though it reserved nothing. A real reserve arrives with the schema
    # migration (context_input + max_output), not here.
    if num_predict in (-1, -2):
        return num_ctx, OUTPUT_RESERVE_UNBOUNDED
    if num_predict in (None, "", 0):
        return num_ctx, "no output reserve declared"
    if not isinstance(num_predict, int) or num_predict < 0:
        raise SystemExit(f"invalid geometry: {role} num_predict={num_predict!r}")
    if num_predict >= num_ctx:
        raise SystemExit(
            f"invalid geometry: {role} reserves {num_predict} output tokens from "
            f"a {num_ctx}-token window — nothing would be left for the prompt")
    admit = num_ctx - num_predict
    if admit <= 0:
        raise SystemExit(f"invalid geometry: {role} admits {admit} input tokens")
    return admit, ""


def gen_role_block(role, info):
    num_ctx = ctx_of(info)
    backend = backend_of(info)
    ka = norm_keep_alive(info.get("keep_alive"))

    # Provider comes from the profile. This used to sniff the role name and the
    # model tag (`backend.startswith("nomic")`) — a model-name conditional that
    # broke the moment an embedding model was not called "nomic".
    provider = (info.get("provider") or "").strip() or "ollama_chat"
    if provider == "ollama":
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
            f"      model: {provider}/{backend}",
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

    reasoning = bool(info.get("reasoning"))
    merge     = bool(info.get("merge"))
    vision    = bool(info.get("vision"))
    parallel  = not reasoning
    # Was min(16384, max(1024, num_ctx // 4)) -- a derivation with no owner that
    # disagreed with num_predict. max_output is now declared in the profile.
    # One geometry derivation per call: this computed _geom(info) twice.
    _g        = _geom(info)
    max_out   = _g["max_output"] or min(16384, max(1024, num_ctx // 4))
    _admit, _admit_note = admission_for(role, _g["num_ctx"], _g["num_predict"])
    desc      = info.get("role", "")

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
    # num_predict is DERIVED from max_output. It is the only ceiling the backend
    # honours: a per-request max_tokens of 512 against an alias declaring 32768
    # returned 4,199 tokens (measured, LiteLLM 1.93.0 ollama_chat).
    if _g["num_predict"] is not None:
        params.append(f"      num_predict: {_g['num_predict']}")
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
        # `reasoning` flag in profiles/<tier>.yaml now drives this per
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
    # Cost zeros are emitted ONCE, above. Repeating them here produces duplicate
    # YAML keys in every model_info block — the later key wins, so the values are
    # still right, but a strict parser rejects the file outright.
    mi += [
        f"      max_input_tokens: {_admit}",
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
    # ALWAYS from the template. config.yaml is a build artifact: reading it back
    # would let a hand-edit to the generated file survive, which is the drift the
    # template exists to prevent.
    text = LITELLM_TEMPLATE.read_text()
    text, s1 = splice(text, ML_BEGIN, ML_END, gen_model_list(models), "model_list")
    text, s2 = splice(text, AL_BEGIN, AL_END, gen_alias_block(models, clients), "model_group_alias")
    stage(LITELLM_CONFIG, text)
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
    # JSON cannot carry comments, so ownership travels in a "//" key -- every
    # other generated artifact states its owner and how to regenerate it, and a
    # timestamp alone does not tell a reader not to hand-edit this.
    stage(CAPS_JSON, json.dumps(
        {"//": ["GENERATED by lib/sync-models.py — DO NOT EDIT.",
                "Source of truth: profiles/<active tier>.yaml.",
                "Regenerate: ailocal sync"],
         "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
            # THESE DESCRIBE DEPLOYED STATE, NOT HISTORICAL EXPERIMENTS.
            # Both entries previously reported the outcome of an investigation
            # rather than the configuration that ships, and Cadence reads this
            # file to decide whether a tool is usable -- so a stale value here
            # makes it apply the wrong policy. `execution: failing` was recorded
            # while native LSP was being debugged; `codex_mcp_lsp.configured:
            # true` described an experiment that was concluded and withdrawn.
            "claude_native_lsp": {
                "configured": True,
                # The gateway names native `LSP` explicitly (registry group
                # `native_lsp`, in the `always` floor) rather than relying on
                # fail-open, so the schema reaches the model on every task class
                # that keeps a floor.
                "schema_preserved": True,
                # Asserted by the gate: tests/lsp-baseline.py drives a
                # real documentSymbol request through claude-local and requires
                # actual symbols back. If that stops working the gate fails
                # before this file can claim otherwise.
                # "working", not an invented word. Cadence's consumer maps
                # execution onto a FIXED vocabulary in _state_from()
                # (compose_instructions.py): working | failing | blocked |
                # blocked_namespace_dispatch. Anything else falls through to
                # `configured` -- "configured but not verified working" --
                # which UNDERSTATES a capability the gate proves works.
                # Measured against the real consumer: "verified" produced
                # state='configured'; "working" produces state='working'.
                "execution": "working",
                "verified_by": "tests/lsp-baseline.py",
                "scope": "python",
            },
            "codex_mcp_lsp": {
                # WITHHELD, so `configured` is false. codex-local ships with no
                # MCP servers at all -- no grepai, no LSP, no GitHub -- and
                # tests/codex-mcp-withheld.sh asserts the generated and
                # deployed configs contain zero [mcp_servers.*] blocks.
                "configured": False,
                "schema_preserved": False,
                "execution": "withheld_client_incompatible",
                # WHY, so Cadence does not read this as a transient failure and
                # retry: Codex declares MCP servers as namespace BUNDLES, which
                # LiteLLM discards before the backend; flattening them makes
                # Codex's own dispatcher refuse the call (openai/codex#20652).
                # An MCP server here would advertise a surface Codex cannot
                # drive, so none is registered.
                "reason": "codex_cannot_dispatch_namespaced_tools",
                "upstream": "openai/codex#20652",
                "verified_by": "tests/codex-mcp-withheld.sh",
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
    # No self-existence check. This writes the WHOLE file, so requiring it to
    # already exist meant a fresh clone — where it is correctly git-ignored —
    # could never produce it.
    CODEX_CATALOG.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    prio = 10
    for name, info in models.items():
        if name == "embeddings":
            continue
        backend = backend_of(info)
        num_ctx = ctx_of(info)
        vision = bool(info.get("vision"))
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
    stage(CODEX_CATALOG, json.dumps({"models": entries}, indent=2) + "\n")
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
    if not COPILOT_REPO_TPL.exists():
        return False
    text = COPILOT_REPO_TPL.read_text()
    text, spliced = splice(text, CP_BEGIN, CP_END, gen_copilot_capabilities(models),
                           "copilot capabilities")
    stage(COPILOT_REPO_MD, text)
    return spliced


def regen_configure_zsh(clients):
    if not CONFIGURE_ZSH_TPL.exists():
        return False
    text = CONFIGURE_ZSH_TPL.read_text()
    text, spliced = splice(text, CS_BEGIN, CS_END, gen_slot_block(clients), "claude slots")
    stage(CONFIGURE_ZSH, text)
    return spliced


def regen_claude_settings(models, clients):
    if not CLAUDE_SETTINGS_TPL.exists():
        return False
    data = json.loads(CLAUDE_SETTINGS_TPL.read_text())
    default = (clients.get("claude") or {}).get("launch_default", next(iter(models)))
    caps = " | ".join(mn(k) for k in models.keys())
    slots = (clients.get("claude") or {}).get("slots", {})
    slot_txt = ", ".join(f"{k.title()}->{mn(v)}" for k, v in slots.items())
    data["model"] = mn(default)
    data["//"] = [
        "Claude Code settings — deployed to ~/.config/ailocal/claude/settings.json",
        "(CLAUDE_CONFIG_DIR for the local variant; ~/.claude is never touched).",
        "",
        "Generated by ailocal. Regenerate: ailocal sync. Edit profiles/clients.yaml.",
        "Base URL + key are injected per-process by the claude-local() wrapper.",
        "",
        "All requests route through LiteLLM by ailocal-<capability> model id.",
        f"Models: {caps}",
        f"Launch default = {mn(default)}. At runtime /model lists every model (the wrapper sets",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1, so Claude Code GETs /v1/models).",
        f"Built-in slots remap: {slot_txt}.",
        "Launch with: claude-local",
    ]
    # Client-native compaction. ailocal does NOT summarise conversations; it only
    # tells Claude Code to compact EARLIER than it would for a hosted model, because
    # a local backend's cold prompt eval is super-linear (measured: 85 s at 28K,
    # 341 s at 58K, 789 s at 88K). Compacting before that zone is what keeps a long
    # architecture session alive. The model maximum is unchanged and still available.
    win, pct = COMPACTION.get("window"), COMPACTION.get("pct")
    if win and pct:
        data.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(win)
        data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = str(pct)
        data["//"].append(
            f"Auto-compaction: window {win} x {pct}% = {int(win)*int(pct)//100} tokens "
            f"(profile-owned). NOT the model limit - architecture keeps its full context.")
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


def regen_codex(models, clients):
    cx = clients.get("codex", {})
    default = cx.get("default", next(iter(models)))
    profiles = cx.get("profiles", {})
    done = False
    if CODEX_TPL.exists():
        text = MARKER_TOML + CODEX_TPL.read_text()
        text = re.sub(r'(?m)^model\s*=.*$', f'model = "{mn(default)}"', text, count=1)
        text = re.sub(r'(?m)^# Valid models:.*$',
                      "# Valid models: " + " | ".join(mn(k) for k in models.keys()), text, count=1)
        stage(CODEX_CONFIG, text)
        done = True
        # Codex's own compactor, same policy as claude-local. These keys must be
        # TOP-LEVEL: Codex does not reliably honour them inside a named profile,
        # so they are written to the root of the isolated codex-local config
        # rather than relying on profile inheritance.
        # Derive from the capability CODEX ACTUALLY DEFAULTS TO, not architecture.
        # Using architecture's 98,304 wrote a compaction limit of 49,152 for a
        # default model whose ENTIRE context is 24,576 -- compaction could never
        # fire, because the model would 400 on context length first. The window
        # must describe the model Codex will really talk to.
        win, pct = COMPACTION.get("window"), COMPACTION.get("pct")
        # total_context from the SHARED geometry. This read `.get("context")`,
        # a key the geometry migration removed, so the guard below silently
        # failed and this file was never regenerated -- it simply kept its
        # pre-migration contents, which looked correct only because the value
        # had not changed yet. Fail loudly instead of skipping.
        _cx = _geom(models.get(default) or {})
        cx_ctx = _cx["total_context"]        # the window Codex advertises
        cx_in = _cx["context_input"]         # what the backend will ADMIT
        if not (win and pct and cx_ctx and cx_in):
            raise SystemExit(
                "codex compaction cannot be derived: "
                f"window={win!r} pct={pct!r} total_context={cx_ctx!r} "
                f"context_input={cx_in!r}")
        if True:
            # Never advertise a compaction point the model cannot reach.
            #
            # The cap is context_input, NOT total_context. total_context is
            # input+output, and the output half is space the INPUT can never
            # occupy -- so a fraction of it can still land above the admission
            # limit. On implementation (16384 + 8192):
            # 75% of total_context gave a trigger of 18,432 while LiteLLM
            # admits 16,384 (max_input_tokens, confirmed via /model/info), so a
            # long session would have taken an HTTP 400 ContextWindowExceeded
            # 2,048 tokens BEFORE Codex could compact. That is precisely the
            # failure the line above claims to prevent; only the denominator
            # was wrong.
            trigger = min(int(win) * int(pct) // 100, int(int(cx_in) * int(pct) / 100))
            if trigger > int(cx_in):
                raise SystemExit(
                    "codex compaction trigger exceeds admissible input: "
                    f"trigger={trigger} context_input={cx_in}")
            arch_ctx = cx_ctx
            text = _staged_text(CODEX_CONFIG)
            for key, val in (("model_context_window", arch_ctx),
                             ("model_auto_compact_token_limit", trigger)):
                if re.search(rf'(?m)^{key}\s*=', text):
                    text = re.sub(rf'(?m)^{key}\s*=.*$', f'{key} = {val}', text, count=1)
                else:
                    text = f'{key} = {val}\n' + text
            stage(CODEX_CONFIG, text)
    if "plan" in profiles:
        _set_toml_model(CODEX_PLAN, mn(profiles["plan"]), CODEX_PLAN_TPL)
    if "review" in profiles:
        _set_toml_model(CODEX_REVIEW, mn(profiles["review"]), CODEX_REVIEW_TPL)
    return done


# ── Continue config.json ───────────────────────────────────────────────────────
def regen_continue(models, clients):
    if not CONTINUE_CONFIG_TPL.exists():
        return False
    data = json.loads(CONTINUE_CONFIG_TPL.read_text())
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
        "ailocal — VS Code Continue config. MANAGED/GENERATED — edit profiles/clients.yaml +",
        "profiles/<tier>.yaml + profiles/clients.yaml, run `ailocal sync`. Deployed by",
        "install-clients.sh (vscode); __LITELLM_KEY__ substituted from .env at install.",
        "Chat/edit go through LiteLLM (4000); autocomplete hits Ollama (11434) directly for FIM.",
        "Models: " + " | ".join(mn(k) for k in models.keys()),
    ]
    stage(CONTINUE_CONFIG, json.dumps(data, indent=2) + "\n")
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


def resolve_keep_alive(role, tier=None):
    info = load_models_yaml(profile_path(explicit=tier)).get(role)
    if not info:
        print("", end="")
        return 1
    print(norm_keep_alive(info.get("keep_alive")))
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


EFFECTIVE_SCHEMA_VERSION = 2
EFFECTIVE_JSON = _pc.effective_profile_path()


def build_effective_profile(active_tier, path):
    """The canonical post-generation view of the deployed role configuration.

    A DEDICATED artifact rather than an extension of capabilities.generated.json:
    that file's documented purpose is the resolved-capability view for
    `ailocal status`, it is consumed by ten call sites including the LiteLLM
    container, and overloading it would couple the proxy's capability contract
    to profile internals such as sampling parameters.

    Hashes of BOTH inputs are recorded so a consumer can tell that generated
    state no longer matches the profile it came from -- staleness is detectable
    rather than assumed away."""
    def tier_block(t):
        prof_path = PROFILES_DIR / f"{t}.yaml"
        roles = {}
        for role in _pc.ROLES:
            try:
                c = _pc.resolve_role(t, role, ROOT)
            except _pc.ProfileError:
                continue
            roles[role] = {k: c[k] for k in (
                "model", "provider", "context_input", "max_output",
                "total_context", "max_input_tokens", "context", "num_predict",
                "reasoning", "temperature",
                "top_p", "top_k", "repeat_penalty", "keep_alive", "persona",
                "enabled", "name", "preferred")}
        data = _pc.load_profile(t, ROOT)
        return {"source_profile": str(prof_path.relative_to(ROOT)),
                "source_profile_sha256": _sha_file(prof_path),
                "compaction": data.get("compaction", {}),
                "roles": roles}

    # EVERY tier is normalized here, at generation time. Benchmark cross-tier
    # planning previously parsed non-active profile YAML at runtime, which
    # contradicted "sync-models is the sole YAML consumer" and left a second
    # parser reachable from live code. One indexed artifact removes that without
    # creating four unrelated files.
    tiers = {t: tier_block(t) for t in _pc.TIERS
             if (PROFILES_DIR / f"{t}.yaml").exists()}
    body = {
        "schema_version": EFFECTIVE_SCHEMA_VERSION,
        "generator": "sync-models.py",
        "active_tier": active_tier,
        "active_profile_sha256": _sha_file(ACTIVE_PROFILE),
        "tiers": tiers,
        # Retained so the active tier is reachable without indexing, and so a
        # v1 consumer's mental model still holds.
        "tier": active_tier,
        "source_profile": tiers[active_tier]["source_profile"],
        "source_profile_sha256": tiers[active_tier]["source_profile_sha256"],
        "compaction": tiers[active_tier]["compaction"],
        "roles": tiers[active_tier]["roles"],
    }
    body["config_sha256"] = _sha_text(
        json.dumps(body, sort_keys=True, separators=(",", ":")))
    body["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return body


def _sha_file(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def _sha_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ── atomic generation ───────────────────────────────────────────────────────
# Outputs are STAGED and only replaced once every generator has succeeded. A
# partial generation is worse than no generation: LiteLLM would serve one
# profile's aliases while the clients pointed at another's, and nothing would
# report a problem. Nothing here is replaced if any step raises.
_STAGE: dict = {}
_STAGED_TEXT: dict = {}

MARKER_TOML = ("# Generated by ailocal. Do not edit.\n"
               "# Regenerate: ailocal sync\n"
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
    transaction -- os.replace() is atomic PER FILE and the set is replaced one
    file at a time -- but a failure part-way through restores every destination
    already replaced, so the tree ends up entirely old or entirely new.

    WHY ROLLBACK AND NOT JUST AN ORDERED COMMIT MARKER. Writing
    effective-profile.json last does make marker-aware readers fail closed, but
    it does not protect the deployed system: LiteLLM reads
    the generated config.yaml directly and the clients read their own generated
    files directly -- none of them consult the marker. Proven by fault
    injection (tests/generation-rollback.py): failing after three
    replaces left config/capabilities.generated.json new while the marker and
    the client configs were still old. Mixed state, on disk, servable.

    Order still matters and is kept: temp files are written and validated
    first, dependent artifacts are replaced next, and effective-profile.json
    goes LAST -- so even in the window before rollback completes, the marker
    never claims a generation that is not fully applied."""
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
        marker = next((p for p in tmp if p.name == "effective-profile.json"), None)
        ordered = sorted((p for p in tmp if p is not marker), key=lambda p: str(p))
        if marker is not None:
            ordered.append(marker)
        for path in ordered:
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



def main():
    tier, args = parse_profile_flag(sys.argv[1:])

    check_only = bool(args) and args[0] == "--check"
    if check_only:
        args = args[1:]
    if args and args[0] == "--resolve":
        sys.exit(resolve(args[1], tier) if len(args) >= 2 else 1)

    if args and args[0] == "--resolve-keep-alive":
        sys.exit(resolve_keep_alive(args[1], tier) if len(args) >= 2 else 1)

    # The TEMPLATE is the precondition. config.yaml is this script's output, so
    # requiring it to pre-exist made a fresh clone unable to bootstrap.
    if not LITELLM_TEMPLATE.exists():
        print(f"Error: {LITELLM_TEMPLATE} not found", file=sys.stderr); sys.exit(1)

    tier = resolve_tier(tier)          # fail-closed; never an implicit default
    path = profile_path(tier=tier)
    step(f"Reading {path.relative_to(ROOT)} + profiles/clients.yaml")
    models = load_models_yaml(path)
    clients = load_clients_yaml()
    for role, info in models.items():
        print(f"  {role}: {backend_of(info)}  (ctx {ctx_of(info)}, keep_alive {norm_keep_alive(info.get('keep_alive'))})")

    # The rule is owned by policy; the generator's job is to fail closed on it.
    for severity, message in _pc.slot_problems():
        if severity == "error":
            sys.exit(f"error: {message}")
        warn(message)

    step("Regenerating the generated config.yaml (model_list + aliases)")
    ok("litellm config regenerated" if regen_litellm(models, clients) else "litellm config unchanged/skipped")

    step("Writing derived files")
    stage(EFFECTIVE_JSON,
          json.dumps(build_effective_profile(tier, path), indent=2,
                     sort_keys=True) + "\n")
    ok("effective-profile.json")
    ok("capabilities.generated.json") if write_caps_json(models) else warn("caps json skipped")
    ok("model_catalog.json") if regen_catalog(models) else warn("catalog skipped")
    ok("claude/settings.json") if regen_claude_settings(models, clients) else warn("claude settings skipped")
    ok("configure.zsh (claude slots)") if regen_configure_zsh(clients) else warn("configure.zsh slots skipped")
    ok("integration-contract.json (for Cadence)") if write_integration_contract(models) else warn("contract skipped")
    ok("copilot repo-instructions (capabilities)") if regen_copilot_repo_md(models) else warn("copilot repo md skipped")
    ok("codex config + profiles") if regen_codex(models, clients) else warn("codex skipped")
    ok("continue/config.json") if regen_continue(models, clients) else warn("continue skipped")

    if check_only:
        # generated_at is a timestamp: two identical generations differ by it,
        # so it is normalised away rather than reported as drift every run.
        def _stable(t):
            return re.sub(r'"generated_at":\s*"[^"]*"', '"generated_at": ""', t)

        def _label(d):
            for base in (_pc.runtime_root(), ROOT):
                try:
                    return str(d.relative_to(base))
                except ValueError:
                    continue
            return str(d)

        drift = sorted(_label(d) for d, text in _STAGE.items()
                       if not d.exists() or _stable(d.read_text()) != _stable(text))
        if drift:
            print("DRIFT — generated files are stale: " + ", ".join(drift),
                  file=sys.stderr)
            return 1
        print("[REAL] in sync — generated files match the active profile "
              "and client policy")
        return 0
    n = flush_stage()
    ok(f"{n} generated files replaced atomically")
    step("Done — restart LiteLLM (`ailocal start`) and re-run `ailocal clients` "
         "to deploy the regenerated client configs.")


if __name__ == "__main__":
    sys.exit(main() or 0)
