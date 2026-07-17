#!/usr/bin/env python3
"""
sync-models.py — propagate config/models.yaml to all derived files.

Edit config/models.yaml (or switch profiles via install.sh), then run
./scripts/sync-models.sh. This script:

  1. Self-heals backend names everywhere. It maps EVERY backend used by a role
     in any profile to that role's ACTIVE backend, then applies that map to
     config.yaml + all derived docs/catalogs. Re-running always repairs drift
     (unlike the old swap-on-diff, which skipped docs when config.yaml already
     matched models.yaml).
  2. Regenerates the num_ctx and model_info capability block for each
     canonical Layer-1 role (router/coder/reasoner/supervisor) directly from
     models.yaml — so capabilities stay correct when profiles switch.
  3. Regenerates the Codex model_catalog.json capability fields and keeps its
     "<n>K ctx" description tokens in sync with num_ctx.

Derived files updated by name-swap:
  - config/litellm/config.yaml  (all role + alias layer entries)
  - config/clients/model_catalog.json
  - config/clients/CLAUDE.md
  - README.md

Capability model (per canonical role):
  - Tool calling         : all roles (Qwen3 / DeepSeek-R1 / Gemma all support it)
  - Parallel tool calls  : all except reasoner (DeepSeek-R1 has no reliable parallel)
  - Reasoning / thinking : all except supervisor (Gemma family is not a thinking model)
  - Vision + PDF input   : driven by the `vision:` field in models.yaml
  - Token budgets        : max_input_tokens = num_ctx; max_output_tokens = min(16384, num_ctx//4)

Advanced cloud-only capabilities (audio, web_search, url_context, computer_use,
prompt_caching, response_schema) are intentionally left off — local Ollama
backends do not support them through the OpenAI-compatible API, and advertising
them would make clients send payloads the models reject.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML   = ROOT / "config/models.yaml"
PROFILES_DIR  = ROOT / "config/profiles"
LITELLM_CONFIG = ROOT / "config/litellm/config.yaml"
CODEX_CATALOG  = ROOT / "config/clients/model_catalog.json"
# Plain backend-name text-swap targets. model_catalog.json is handled
# separately by regen_codex_catalog (structured JSON edit + name swap).
DERIVED_FILES = [
    ROOT / "README.md",
    ROOT / "config/clients/CLAUDE.md",
]

# Canonical Layer-1 roles whose capability block is generated from models.yaml.
# embed is excluded — it is an embedding model with a fixed, special model_info.
CANONICAL_ROLES = ["router", "coder", "reasoner", "supervisor"]


def step(msg): print(f"\n▶ {msg}")
def ok(msg):   print(f"  ✓ {msg}")
def warn(msg): print(f"  ⚠ {msg}", file=sys.stderr)


def truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def load_models_yaml():
    """Return dict: role → {backend, description, context_window, num_ctx, vision, ...}."""
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
                # strip trailing inline comments (e.g. "true   # note")
                v = v.split("#", 1)[0].strip()
                models[current][k.strip()] = v
    return models


def load_role_backends():
    """role -> set of every backend tag ever used for it, across ALL profiles
    plus the active models.yaml.

    Used to build a *self-healing* swap map: any historical backend for a role
    is remapped to that role's ACTIVE backend. This is why re-running
    sync-models.sh repairs drift in derived files even when config.yaml already
    matches models.yaml (the old swap-on-diff approach could not — it compared
    config.yaml to models.yaml, found them equal, and skipped the docs).
    """
    role_backends = {}
    files = sorted(PROFILES_DIR.glob("*.yaml")) + [MODELS_YAML]
    for f in files:
        if not f.exists():
            continue
        cur = None
        for line in f.read_text().splitlines():
            s = line.rstrip()
            if not s or s.lstrip().startswith("#"):
                continue
            if not s.startswith(" ") and s.endswith(":"):
                cur = s[:-1]
            elif cur and s.strip().startswith("backend:"):
                be = s.split("backend:", 1)[1].split("#")[0].strip()
                if be:
                    role_backends.setdefault(cur, set()).add(be)
    return role_backends


def gen_model_info(role, info):
    """Return (num_ctx, model_info_block_text) for a canonical role."""
    num_ctx = int(info.get("num_ctx") or info.get("context_window") or 32768)
    max_out = min(16384, max(1024, num_ctx // 4))
    vision = truthy(info.get("vision", "false"))
    # Only the reasoner role streams <think> reasoning. router/coder are
    # execution roles pinned to reasoning_effort:"none" in config.yaml (a long
    # reasoning_content burst reads as a hang in OpenAI-format clients like VS
    # Code Copilot); supervisor (Gemma) is not a thinking model.
    reasoning = role == "reasoner"
    parallel = role != "reasoner"        # DeepSeek-R1: no reliable parallel tool calls

    lines = [
        "    model_info:",
        "      supports_function_calling: true",
        "      supports_tool_choice: true",
        f"      supports_parallel_function_calling: {'true' if parallel else 'false'}",
        "      supports_system_messages: true",
        "      supports_native_streaming: true",
        f"      supports_reasoning: {'true' if reasoning else 'false'}",
    ]
    if vision:
        lines.append("      supports_vision: true")
        lines.append("      supports_pdf_input: true")
    lines += [
        f"      max_input_tokens: {num_ctx}",
        f"      max_output_tokens: {max_out}",
        "      input_cost_per_token: 0",
        "      output_cost_per_token: 0",
    ]
    return num_ctx, "\n".join(lines) + "\n"


def regen_canonical(text, models):
    """Rewrite num_ctx + model_info for each canonical role in config.yaml text.

    Scoped by exact `model_name: <role>` so the Layer-2/3 alias entries
    (claude-*, gpt-*) are never touched.
    """
    for role in CANONICAL_ROLES:
        info = models.get(role)
        if not info:
            warn(f"{role}: not in models.yaml — capability block left unchanged")
            continue
        num_ctx, block = gen_model_info(role, info)

        # 1) num_ctx inside litellm_params
        text, n1 = re.subn(
            rf"(- model_name: {re.escape(role)}\n    litellm_params:\n"
            rf"      model: [^\n]+\n      api_base: [^\n]+\n      num_ctx: )\d+",
            lambda m: m.group(1) + str(num_ctx),
            text, count=1)

        # 2) the whole model_info body (all 6-space-indented lines)
        text, n2 = re.subn(
            rf"(- model_name: {re.escape(role)}\n    litellm_params:\n"
            rf"(?:      [^\n]+\n)+)    model_info:\n(?:      [^\n]+\n)+",
            lambda m: m.group(1) + block,
            text, count=1)

        if n1 and n2:
            caps = ["tools"]
            if role != "reasoner": caps.append("parallel")
            if role != "supervisor": caps.append("reasoning")
            if truthy(info.get("vision", "false")): caps += ["vision", "pdf"]
            ok(f"{role}: num_ctx={num_ctx}, caps=[{', '.join(caps)}]")
        else:
            warn(f"{role}: could not locate canonical block (num_ctx match={n1}, model_info match={n2})")
    return text


def regen_codex_catalog(models, swaps):
    """Update config/clients/model_catalog.json from models.yaml.

    Codex (and other model_catalog.json consumers) read capabilities from this
    file, NOT from LiteLLM's /model/info. This single writer handles both the
    backend-name swaps (in display strings) and the capability fields
    (context window, image modality, parallel tools). Returns True if changed.
    """
    if not CODEX_CATALOG.exists():
        return False
    raw = CODEX_CATALOG.read_text()
    # Backend-name swaps in any string fields (display_name, description, …).
    for old, new in swaps.items():
        raw = raw.replace(old, new)
    try:
        catalog = json.loads(raw)
    except json.JSONDecodeError as e:
        warn(f"model_catalog.json is not valid JSON — skipping capability sync ({e})")
        return False

    for entry in catalog.get("models", []):
        role = entry.get("slug")
        info = models.get(role)
        if role not in CANONICAL_ROLES or not info:
            continue
        num_ctx = int(info.get("num_ctx") or info.get("context_window") or 32768)
        vision = truthy(info.get("vision", "false"))
        entry["context_window"] = num_ctx
        entry["max_context_window"] = num_ctx
        entry["input_modalities"] = ["text", "image"] if vision else ["text"]
        entry["supports_parallel_tool_calls"] = role != "reasoner"
        # Keep the human-written "<n>K ctx" token in the description honest.
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
        extra = " +vision" if truthy(info.get("vision", "false")) else ""
        print(f"  {role}: {info.get('backend', '?')}{extra}")

    litellm_text = LITELLM_CONFIG.read_text()

    step("Building self-healing backend map (every profile's backend → active)")
    role_backends = load_role_backends()
    swaps = {}  # any historical backend → the active backend for its role
    for role, info in models.items():
        active = info.get("backend")
        if not active:
            warn(f"{role}: no backend in models.yaml — skipping")
            continue
        for be in role_backends.get(role, set()):
            if be != active:
                swaps[be] = active
    if swaps:
        for old, new in sorted(swaps.items()):
            print(f"  {old} → {new}")
    else:
        ok("no alternate backends to remap")

    def apply_swaps(text):
        for old, new in swaps.items():
            # Match WHOLE backend tags only. A plain str.replace would corrupt a
            # longer tag that begins with a shorter one — e.g. mapping
            # deepseek-r1:8b -> deepseek-r1:32b would also rewrite the unrelated
            # tag deepseek-r1:8b-0528-qwen3-q8_0. The negative lookahead requires
            # the match to end at a tag boundary (not another tag character).
            text = re.sub(re.escape(old) + r'(?![\w.:-])', new, text)
        return text

    step("Updating config/litellm/config.yaml")
    # Plain-swap fixes backend names everywhere (litellm_params AND comments),
    # then regenerate the canonical capability blocks from models.yaml.
    new_text = regen_canonical(apply_swaps(litellm_text), models)
    if new_text != litellm_text:
        LITELLM_CONFIG.write_text(new_text)
        ok("config.yaml written")
    else:
        ok("config.yaml already up to date")

    step("Updating derived docs (self-healing backend names)")
    for path in DERIVED_FILES:
        text = path.read_text()
        new = apply_swaps(text)
        if new != text:
            path.write_text(new)
            ok(str(path.relative_to(ROOT)))
        else:
            ok(f"{path.relative_to(ROOT)} already up to date")

    step("Syncing Codex model_catalog.json (capabilities + names)")
    if regen_codex_catalog(models, swaps):
        ok("config/clients/model_catalog.json updated")
    else:
        ok("model_catalog.json already up to date")

    step("Done — restart LiteLLM (./scripts/start.sh) so it reloads model_info; "
         "pull any new models with ./scripts/install-models.sh")


if __name__ == "__main__":
    main()
