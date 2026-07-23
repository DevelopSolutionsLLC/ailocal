#!/usr/bin/env python3
"""
sync-models.py — propagate config/models.yaml to all derived files.

Edit config/models.yaml (or switch profiles via install.sh), then run
./scripts/sync-models.sh. This script:

  1. Regenerates the ENTIRE `model_list:` block of config/litellm/config.yaml
     deterministically from models.yaml, between the GENERATED markers. Every
     role becomes exactly one canonical model_name entry; there are no per-model
     duplicate alias blocks anymore — the Claude/OpenAI compatibility names are
     handled once by `router_settings.model_group_alias` (hand-maintained tail,
     which references stable role names, not backend tags). This removes the old
     global backend-name text-swap and its corruption risk.
  2. Regenerates config/clients/model_catalog.json capability fields + "<n>K ctx"
     tokens for the canonical roles Codex exposes.

Per-role flags read from models.yaml:
  reasoning : model thinks / streams reasoning (deepseek-r1). -> supports_reasoning,
              no parallel tool calls, no reasoning_effort:none pin.
  merge     : merge_reasoning_content_in_choices (OpenAI-format clients).
  persona   : informational only — the persona_injector LiteLLM hook injects
              config/personas/<role>.md at request time (roles are served on
              their raw base tags; there are no ailocal-<role> overlay models).
  vision    : advertise image/PDF input.

Non-reasoning chat roles are pinned to reasoning_effort:"none" (a long
reasoning_content burst reads as a hang in OpenAI-format clients like VS Code
Copilot). embed is emitted with its fixed embedding model_info.

Advanced cloud-only capabilities (audio, web_search, computer_use, prompt_caching,
response_schema) are intentionally left off — local Ollama backends don't support
them through the OpenAI-compatible API.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML    = ROOT / "config/models.yaml"
LITELLM_CONFIG = ROOT / "config/litellm/config.yaml"
CODEX_CATALOG  = ROOT / "config/clients/model_catalog.json"

GEN_BEGIN = "  # >>> BEGIN GENERATED model_list (sync-models.py) — do not edit <<<"
GEN_END   = "  # >>> END GENERATED model_list <<<"


def step(msg): print(f"\n▶ {msg}")
def ok(msg):   print(f"  ✓ {msg}")
def warn(msg): print(f"  ⚠ {msg}", file=sys.stderr)


def truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def load_models_yaml():
    """Return ordered dict: role → {backend, num_ctx, reasoning, merge, ...}."""
    models = {}
    current = None
    with open(MODELS_YAML) as f:
        for line in f:
            s = line.rstrip()
            if not s or s.lstrip().startswith("#"):
                continue
            if not s.startswith(" ") and s.endswith(":"):
                current = s.rstrip(":")
                models[current] = {}
            elif current and ":" in s:
                k, _, v = s.strip().partition(":")
                v = v.split("#", 1)[0].strip()
                models[current][k.strip()] = v
    # drop the scalar disk_gb "role"
    models.pop("disk_gb", None)
    return models


def served_tag(role, info):
    """The Ollama tag LiteLLM calls — always the raw base backend.

    Personas are delivered by the LiteLLM persona_injector hook (from the curated
    config/personas/<role>.md), NOT by a baked Ollama SYSTEM. So there is no need
    for ailocal-<role> overlay tags: roles point straight at their base model, and
    `ollama ps` shows the real model name. The `persona:` flag in models.yaml is
    now informational only (the hook keys off which <role>.md files exist).
    """
    return info["backend"]


def gen_role_block(role, info):
    """Return the config.yaml model_list entry text for one chat/embed role."""
    num_ctx = int(info.get("num_ctx") or info.get("context_window") or 32768)

    if role == "embed":
        return (
            f"  - model_name: {role}\n"
            f"    litellm_params:\n"
            f"      model: ollama_chat/{info['backend']}\n"
            f"      api_base: os.environ/OLLAMA_URL\n"
            f"      num_ctx: {num_ctx}\n"
            f"    model_info:\n"
            f"      mode: embedding\n"
            f"      max_tokens: {num_ctx}\n"
            f"      input_cost_per_token: 0\n"
            f"      output_cost_per_token: 0\n"
        )

    reasoning = truthy(info.get("reasoning", "false"))
    merge     = truthy(info.get("merge", "false"))
    vision    = truthy(info.get("vision", "false"))
    parallel  = not reasoning                 # DeepSeek-R1: no reliable parallel calls
    max_out   = min(16384, max(1024, num_ctx // 4))
    desc      = info.get("description", "")

    params = [
        f"  - model_name: {role}",
        f"    litellm_params:",
        f"      model: ollama_chat/{served_tag(role, info)}",
        f"      api_base: os.environ/OLLAMA_URL",
        f"      num_ctx: {num_ctx}",
    ]
    # Vendor-recommended sampling defaults (from models.yaml). Applied when the
    # client does not send its own. DeepSeek-R1: temp 0.6 / top_p 0.95, no system
    # prompt. Qwen coders: temp 0.7 / top_p 0.8 / top_k 20 / rep 1.05. Gemma:
    # temp 1.0 / top_p 0.95 / top_k 64.
    for key in ("temperature", "top_p", "top_k", "repetition_penalty"):
        if info.get(key) not in (None, ""):
            params.append(f"      {key}: {info[key]}")
    if merge:
        params.append("      merge_reasoning_content_in_choices: true")
    if not reasoning:
        # Execution roles: two independent guards.
        # (1) Drop client-sent thinking params — a client sending `thinking`/
        #     `reasoning_effort` (Claude Code does, on its opus/sonnet/haiku slots)
        #     otherwise 400s in Ollama ("<model> does not support thinking").
        # (2) think:false — suppress DEFAULT reasoning. Some capable backends
        #     (qwen3.6) emit reasoning_content unprompted, which OpenAI-format
        #     clients (VS Code Copilot) render as a silent "Considering…" hang.
        #     Accepted by all non-reasoner backends (no-op where not applicable).
        # The deep-think* reasoners omit both — they genuinely think.
        params.append('      additional_drop_params: ["thinking", "reasoning_effort"]')
        params.append("      think: false")

    mi = [
        f"    model_info:",
        f"      supports_function_calling: true",
        f"      supports_tool_choice: true",
        f"      supports_parallel_function_calling: {'true' if parallel else 'false'}",
        f"      supports_system_messages: true",
        f"      supports_native_streaming: true",
        f"      supports_reasoning: {'true' if reasoning else 'false'}",
    ]
    if vision:
        mi += ["      supports_vision: true", "      supports_pdf_input: true"]
    mi += [
        f"      max_input_tokens: {num_ctx}",
        f"      max_output_tokens: {max_out}",
        f"      input_cost_per_token: 0",
        f"      output_cost_per_token: 0",
    ]
    header = f"  # {role} — {desc}\n" if desc else ""
    return header + "\n".join(params) + "\n" + "\n".join(mi) + "\n"


def gen_model_list(models):
    blocks = [gen_role_block(role, info) for role, info in models.items()]
    return GEN_BEGIN + "\n\n" + "\n".join(blocks) + "\n" + GEN_END + "\n"


def splice_generated(text, generated):
    """Replace everything between the markers (inclusive) with `generated`."""
    pat = re.compile(re.escape(GEN_BEGIN) + r".*?" + re.escape(GEN_END) + r"\n",
                     re.DOTALL)
    if not pat.search(text):
        warn("GENERATED markers not found in config.yaml — cannot splice")
        return text, False
    return pat.sub(lambda _m: generated, text, count=1), True


def regen_codex_catalog(models):
    """Update config/clients/model_catalog.json capability fields from models.yaml."""
    if not CODEX_CATALOG.exists():
        return False
    try:
        catalog = json.loads(CODEX_CATALOG.read_text())
    except json.JSONDecodeError as e:
        warn(f"model_catalog.json is not valid JSON — skipping ({e})")
        return False
    for entry in catalog.get("models", []):
        role = entry.get("slug")
        info = models.get(role)
        if not info or role == "embed":
            continue
        num_ctx = int(info.get("num_ctx") or info.get("context_window") or 32768)
        vision = truthy(info.get("vision", "false"))
        entry["context_window"] = num_ctx
        entry["max_context_window"] = num_ctx
        entry["input_modalities"] = ["text", "image"] if vision else ["text"]
        entry["supports_parallel_tool_calls"] = not truthy(info.get("reasoning", "false"))
        if "description" in entry:
            entry["description"] = re.sub(
                r"\b\d+K ctx\b", f"{num_ctx // 1024}K ctx", entry["description"])
    new_raw = json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"
    if new_raw != CODEX_CATALOG.read_text():
        CODEX_CATALOG.write_text(new_raw)
        return True
    return False


def main():
    for path in [MODELS_YAML, LITELLM_CONFIG]:
        if not path.exists():
            print(f"Error: {path} not found", file=sys.stderr)
            sys.exit(1)

    step("Reading config/models.yaml")
    models = load_models_yaml()
    for role, info in models.items():
        tags = []
        if truthy(info.get("persona", "false")): tags.append("persona")
        if truthy(info.get("reasoning", "false")): tags.append("reasoning")
        if truthy(info.get("merge", "false")): tags.append("merge")
        if truthy(info.get("vision", "false")): tags.append("vision")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  {role}: {info.get('backend', '?')} → served as "
              f"{served_tag(role, info)}{suffix}")

    step("Regenerating config/litellm/config.yaml model_list")
    litellm_text = LITELLM_CONFIG.read_text()
    new_text, spliced = splice_generated(litellm_text, gen_model_list(models))
    if spliced and new_text != litellm_text:
        LITELLM_CONFIG.write_text(new_text)
        ok("config.yaml model_list regenerated")
    elif spliced:
        ok("config.yaml already up to date")

    step("Syncing Codex model_catalog.json (capabilities + ctx tokens)")
    ok("model_catalog.json updated" if regen_codex_catalog(models)
       else "model_catalog.json already up to date")

    step("Done — restart LiteLLM (./scripts/start.sh) so it reloads model_info; "
         "pull any new models (./scripts/install-models.sh)")


if __name__ == "__main__":
    main()
