"""
persona_injector.py — LiteLLM proxy pre-call hook that gives each role its baked
"Opus-like" persona, on EVERY client (Claude Code, Codex, Continue, Copilot).

A hook, not an Ollama Modelfile SYSTEM: a Modelfile SYSTEM applies only when the
request carries no system message, and every coding client sends its own. This
merges the persona INTO the client's system message so both survive.

TWO ROUTES, TWO INJECTION POINTS — the whole reason this is not one branch:
  /v1/chat/completions  the system prompt is a role:system entry in
                        data["messages"]
  /v1/messages          it is the TOP-LEVEL data["system"] field (string or
                        content blocks), NOT in messages[]

Registered as a CustomLogger async_pre_call_hook.
Ref: https://docs.litellm.ai/docs/proxy/call_hooks

Text comes from instructions/<capability>.md (shared _core.md plus a
per-capability enhancer), mounted read-only at $AILOCAL_INSTRUCTIONS_DIR. A
capability with no file gets _core.md alone; adding a file is the only step
needed to give one an enhancer, with no code change.
"""

import glob
import logging
import os

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("persona_injector")

INSTRUCTIONS_DIR = os.environ.get("AILOCAL_INSTRUCTIONS_DIR", "/app/instructions")
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/generated/config.yaml")


def _read(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _load_personas():
    """capability -> instruction text: the shared _core.md prepended to each curated
    per-capability enhancer deploy/litellm/instructions/<capability>.md. Files whose name starts
    with '_' are shared fragments, not capabilities."""
    core = _read(os.path.join(INSTRUCTIONS_DIR, "_core.md"))
    personas = {}
    for path in glob.glob(os.path.join(INSTRUCTIONS_DIR, "*.md")):
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

    def _role_for(self, model):
        # Resolve a compat alias (claude-*/gpt-*) to its ailocal-<cap> group, then strip the
        # ailocal- prefix to get the capability key the persona files are named by.
        role = self.alias.get(model, model)
        if role.startswith("ailocal-"):
            role = role[len("ailocal-"):]
        return role

    @staticmethod
    def _merge_openai(data, persona):
        """Merge into the role:system entry of data["messages"] (OpenAI shape)."""
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

    @staticmethod
    def _merge_anthropic(data, persona):
        """Merge into the TOP-LEVEL data["system"] (Anthropic /v1/messages shape). The
        field may be absent, a string, or a list of content blocks — handle all three,
        idempotently."""
        system = data.get("system")
        if not system:
            data["system"] = persona
        elif isinstance(system, str):
            if persona not in system:
                data["system"] = persona + "\n\n" + system
        elif isinstance(system, list):
            # List of content blocks ({"type":"text","text":...}); prepend one text block
            # unless the persona is already present in some block.
            present = any(isinstance(b, dict) and persona in (b.get("text") or "")
                          for b in system)
            if not present:
                data["system"] = [{"type": "text", "text": persona}] + system
        return data

    def _inject(self, data, anthropic=False):
        model = data.get("model", "")
        role = self._role_for(model)
        persona = self.personas.get(role)
        route = "anthropic_messages" if anthropic else "openai"
        # Debug line for tracing alias/model → capability resolution. Visible when the proxy
        # runs with --detailed_debug (or the persona_injector logger set to DEBUG).
        log.debug("persona_inject requested_model=%s resolved_capability=%s persona=%s route=%s",
                  model, role, f"{role}.md" if persona else "none", route)
        if not persona:
            return data
        return self._merge_anthropic(data, persona) if anthropic else self._merge_openai(data, persona)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Anthropic /v1/messages (Claude Code's route) — system is a top-level field.
        if call_type == "anthropic_messages":
            return self._inject(data, anthropic=True)
        # OpenAI-shaped chat/completion calls carry a role:system entry in messages[].
        if call_type in ("completion", "acompletion", "text_completion",
                         "chat_completion", None):
            return self._inject(data)
        # embeddings / image_generation / moderation / audio_transcription: no system prompt.
        return data


proxy_handler_instance = PersonaInjector()
