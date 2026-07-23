"""
persona_injector.py — LiteLLM proxy pre-call hook that gives each role its baked
"Opus-like" persona, on EVERY client (Claude Code, Codex, Continue, Copilot).

Why a hook and not just an Ollama Modelfile SYSTEM: a Modelfile SYSTEM is only a
DEFAULT — Ollama uses it only when the request carries no system message. Every
coding client sends its own system prompt, which overrides the baked persona
(verified through the proxy: the persona vanished the moment a client system
message was present). This hook merges the persona INTO whatever system message
the client sends, so the persona voice survives alongside the client's task
instructions.

Mechanism (documented): a CustomLogger with async_pre_call_hook, registered via
  litellm_settings:
    callbacks: persona_injector.proxy_handler_instance
The hook mutates data["messages"] before the call is dispatched.
Ref: https://docs.litellm.ai/docs/proxy/call_hooks

Persona source of truth: config/personas/<role>.md (a shared _core.md plus a
per-role enhancer), mounted read-only at $AILOCAL_PERSONA_DIR. The same files
document the Claude Code persona (config/clients/CLAUDE.md), so persona text
lives in one place.

Roles without a persona file (coder-fast, embed) pass through untouched — the fast
autocomplete tier stays lean by design.
"""

import glob
import os

from litellm.integrations.custom_logger import CustomLogger

PERSONA_DIR = os.environ.get("AILOCAL_PERSONA_DIR", "/app/personas")
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/config/config.yaml")


def _read(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _load_personas():
    """role -> persona text: the shared _opus-core.md prepended to each curated
    per-role enhancer config/personas/<role>.md. Files whose name starts with '_'
    are shared fragments, not roles."""
    core = _read(os.path.join(PERSONA_DIR, "_core.md"))
    personas = {}
    for path in glob.glob(os.path.join(PERSONA_DIR, "*.md")):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        role = name[: -len(".md")]
        body = _read(path)
        if not body:
            continue
        personas[role] = (core + "\n\n" + body).strip() if core else body
    return personas


def _load_alias_map():
    """Compatibility name (claude-*, gpt-*) -> role, read from config.yaml's
    router_settings.model_group_alias so this stays in lockstep with routing."""
    try:
        import yaml
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return dict((cfg.get("router_settings") or {}).get("model_group_alias") or {})
    except Exception:
        return {}


class PersonaInjector(CustomLogger):
    def __init__(self):
        super().__init__()
        self.personas = _load_personas()
        self.alias = _load_alias_map()

    def _persona_for(self, model):
        role = self.alias.get(model, model)
        return self.personas.get(role)

    def _inject(self, data):
        persona = self._persona_for(data.get("model", ""))
        if not persona:
            return data
        messages = data.get("messages")
        if not isinstance(messages, list):
            return data
        for msg in messages:
            if msg.get("role") == "system":
                content = msg.get("content") or ""
                # Only merge text-content system messages; skip if already present.
                if isinstance(content, str) and persona not in content:
                    msg["content"] = persona + "\n\n" + content
                break
        else:
            messages.insert(0, {"role": "system", "content": persona})
        data["messages"] = messages
        return data

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Only chat/completion-shaped calls carry a messages array.
        if call_type in ("completion", "acompletion", "text_completion",
                         "chat_completion", None):
            return self._inject(data)
        return data


proxy_handler_instance = PersonaInjector()
