"""system_transport.py — preserve the client's interleaved system messages.

THE DEFECT. LiteLLM's Anthropic `/v1/messages` -> OpenAI adapter
(llms/anthropic/experimental_pass_through/adapters/transformation.py,
`translate_anthropic_messages_to_openai`) branches on `role == "user"` and
`role == "assistant"` and nothing else. A `role: "system"` entry inside the
messages array matches neither branch, falls off the end of the loop body and
is silently discarded. Not reordered, not merged — dropped, with no warning.

[REAL] Measured through the running stack, identical 28-token text:

    mid-array role:"system"   ->  input_tokens 25   (ailocal-architecture)
    same text as role:"user"  ->  input_tokens 53   (ailocal-architecture)
    mid-array role:"system"   ->  input_tokens 36, model answers "NONE"
    top-level `system` field  ->  input_tokens 68, model answers the codeword
    same text as role:"user"  ->  input_tokens 68, model answers the codeword

A message costing exactly zero tokens is not being handled; it is being
deleted.

WHY IT MATTERS HERE. Claude Code uses that channel for everything it wants to
say out of band: SessionStart hook output, the Plan-mode gate and plan-file
path, the five-phase Plan workflow, the subagent-type inventory, the skill
inventory, and mid-session reminders. On claude-local none of it reached the
model. A Plan-mode session was captured in which the model received 29 tool
schemas and a bare user prompt, then behaved exactly as an unconstrained
model should — it never called ExitPlanMode because it was never told it was
planning.

UPSTREAM. Fixed in https://github.com/BerriAI/litellm/pull/30443, which adds an
in-place branch whose helper is documented "Translate an in-sequence system
entry without changing its role or position."

[REAL] That code is in no stable release. Counting occurrences of the new
branch in the adapter, fetched per tag:

    v1.90.0 0   v1.93.0 0   v1.94.0 0   v1.95.0 0
    v1.96.2 0   v1.97.0-rc.1 0   v1.98.0-dev.2 1

Only the dev pre-release has it. The pinned image is 1.93.0 and carries the
PR's cost-map flag (`supports_mid_conversation_system`) but none of its code —
the cost map is a data file that syncs ahead of the wheel. Pinning local
inference to a dev build to obtain one branch is the worse trade, so this hook
carries the behaviour until a stable release ships it.

DELETE THIS FILE, its config entry and its tests when that happens:

    docker exec ailocal-litellm grep -c 'role"\\] == "system"' \\
      /app/.venv/lib/python3.13/site-packages/litellm/llms/anthropic/\\
      experimental_pass_through/adapters/transformation.py

Non-zero means upstream now does this and the hook is dead weight.

WHAT IT DOES. Two rules, matching upstream's semantics:

  leading system entries — those before any user or assistant turn — are
  hoisted into the top-level `system` field, in order, after anything already
  there. That is what the Anthropic shape means by a conversation-level
  instruction, and it is what upstream does.

  mid-conversation system entries are converted IN PLACE. Position is the
  payload: Claude Code inserts a reminder after the turn it comments on, and
  "the reminder that followed your last answer" is not the same statement as
  "a preamble to the whole conversation". Nothing is hoisted, merged, sorted
  or deduplicated.

WHY role "user" AND NOT role "system" FOR THE IN-PLACE CASE. This hook runs
before the adapter, so whatever it emits must survive a translator that only
understands `user` and `assistant`; emitting `system` would re-enter the very
branch that drops it. `user` is the only in-place role that survives.

[REAL] That substitution is measured, not assumed. Against the backend on the
native OpenAI route, where an in-place `system` role is preserved and can be
compared directly:

    mid-array role:"system"  prompt_tokens 55  -> recalls the marker
    mid-array role:"user"    prompt_tokens 55  -> recalls the marker
    marker absent (control)  prompt_tokens 39  -> "NONE"

Same token cost, same recall. For this backend the two representations are
equivalent, so the one that survives translation is used. Re-measure if the
backend or its chat template changes.

The text is wrapped in <system-reminder> unless it already carries that
marker, so the model can tell the harness apart from the user. That is the
client's own convention, not one invented here.

FAIL-OPEN. Any unexpected shape returns the request untouched. A transport
shim that raises turns a client quirk into an outage.
"""

import json
import os
import sys

from litellm.integrations.custom_logger import CustomLogger


def _load_registry_module():
    """Import the sibling capability_registry.py by path.

    Same loader, and the same reason, as tool_gateway.py: LiteLLM loads
    callbacks with spec_from_file_location, which leaves the hooks directory
    off sys.path, and the directory is not a package.
    """
    import importlib.util
    if "capability_registry" in sys.modules:
        return sys.modules["capability_registry"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "capability_registry.py")
    spec = importlib.util.spec_from_file_location("capability_registry", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["capability_registry"] = module
    spec.loader.exec_module(module)
    return module


Registry = _load_registry_module().Registry

MODE_ENV = "AILOCAL_SYSTEM_TRANSPORT"
VALID_MODES = ("off", "on")
ANTHROPIC_ROUTE = "/v1/messages"
MARKER_OPEN = "<system-reminder>"
MARKER_CLOSE = "</system-reminder>"


def mode():
    """Read per-request so the layer can be turned off without a restart.

    An unrecognised value is reported and treated as `on`: this hook restores
    information the client sent, so a typo must not silently resume dropping
    it. That is the opposite default from tool_gateway, which fails to `off`
    because its job is removal.
    """
    raw = (os.environ.get(MODE_ENV) or "on").strip().lower()
    if raw not in VALID_MODES:
        emit({"event": "bad_mode", "value": raw, "treated_as": "on"})
        return "on"
    return raw


def emit(record):
    print("system_transport " + json.dumps(record, default=str), flush=True)


def text_of(content):
    """Flatten an Anthropic system payload to plain text.

    `content` is a string or a list of blocks. Only text blocks carry
    instructions; anything else in a system entry has no OpenAI equivalent and
    is skipped rather than guessed at.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p)


def wrap(text):
    """Mark harness speech so the model does not read it as the user's."""
    if MARKER_OPEN in text:
        return text
    return MARKER_OPEN + "\n" + text + "\n" + MARKER_CLOSE


class SystemTransport(CustomLogger):

    def __init__(self, registry=None):
        super().__init__()
        self.registry = registry if registry is not None else Registry()

    def rewrite(self, data):
        """Return (messages, hoisted, moved) or None when nothing applies.

        `hoisted` are leading entries destined for the top-level field;
        `moved` counts in-place conversions. None means "not our shape",
        which is distinct from "our shape, no system entries".
        """
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return None

        out = []
        hoisted = []
        moved = 0
        emptied = 0
        seen_turn = False

        for message in messages:
            if not isinstance(message, dict):
                out.append(message)
                continue
            role = message.get("role")
            if role in ("user", "assistant"):
                seen_turn = True
                out.append(message)
                continue
            if role != "system":
                out.append(message)
                continue

            text = text_of(message.get("content"))
            if not text.strip():
                # An empty reminder carries nothing; dropping it is not a loss
                # and it keeps a role the adapter cannot read out of the
                # payload. Counted, so this alone still registers as a change.
                emptied += 1
                continue
            if not seen_turn:
                hoisted.append(text)
            else:
                out.append({"role": "user", "content": wrap(text)})
                moved += 1

        if not hoisted and not moved and not emptied:
            return None
        return out, hoisted, moved

    def hoist(self, data, hoisted):
        """Append leading entries to the top-level system field, in order.

        Appended, never prepended: whatever the client already put there is
        the outer frame, and these entries came after it in the payload.
        """
        if not hoisted:
            return
        existing = data.get("system")
        blocks = []
        if isinstance(existing, str) and existing:
            blocks.append(existing)
        elif isinstance(existing, list):
            for block in existing:
                if isinstance(block, dict) and block.get("type") == "text":
                    blocks.append(block.get("text") or "")
                elif isinstance(block, str):
                    blocks.append(block)
        blocks.extend(hoisted)
        data["system"] = "\n\n".join(b for b in blocks if b)

    # ── LiteLLM entrypoint ─────────────────────────────────────────────────
    async def async_pre_call_hook(self, user_api_key_dict, cache, data,
                                  call_type):
        if mode() == "off":
            return data
        try:
            route = self.registry.route_for_call_type(
                call_type, "input" in (data or {}))
            if route != ANTHROPIC_ROUTE:
                return data

            result = self.rewrite(data or {})
            if result is None:
                return data
            messages, hoisted, moved = result

            data["messages"] = messages
            self.hoist(data, hoisted)
            emit({"event": "restored", "route": route,
                  "hoisted": len(hoisted), "moved_in_place": moved,
                  "messages_out": len(messages)})
        except Exception as exc:
            # Restoring context must never be the reason a request fails.
            emit({"event": "failed",
                  "error": "%s: %s" % (type(exc).__name__, exc)})
        return data


proxy_handler_instance = SystemTransport()
