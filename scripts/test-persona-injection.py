#!/usr/bin/env python3
"""test-persona-injection.py — regression tests for config/litellm/persona_injector.py.

Deterministic unit tests (no proxy, no model, no network): they stub the `litellm`
import, construct the hook with a known personas/alias map, and assert what
async_pre_call_hook does to the request `data` for each route. This pins the
behavior that matters and is immune to model (dis)obedience:

  - OpenAI  (/v1/chat/completions): persona merged into the messages[] system entry.
  - Anthropic (/v1/messages): persona merged into the TOP-LEVEL `system` field
    (absent / string / list-of-blocks), which is the route Claude Code uses.
  - completion / embeddings (no persona file) and non-chat call_types: untouched.
  - injection is idempotent (persona never doubled).

Run: python3 scripts/test-persona-injection.py   (stdlib only; exit 1 on failure)
"""

import asyncio
import importlib.util
import os
import sys
import types

# ── stub the `litellm` package so persona_injector imports without it installed ──
_clog = types.ModuleType("litellm.integrations.custom_logger")
class _CustomLogger:            # minimal stand-in for CustomLogger
    def __init__(self, *a, **k): pass
_clog.CustomLogger = _CustomLogger
sys.modules["litellm"] = types.ModuleType("litellm")
sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
sys.modules["litellm.integrations.custom_logger"] = _clog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AILOCAL_PERSONA_DIR", "/nonexistent")   # _load_personas → {} (we override)
_spec = importlib.util.spec_from_file_location(
    "persona_injector", os.path.join(ROOT, "config/litellm/persona_injector.py"))
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)

# Deterministic state: known personas + one compat alias (claude-* → ailocal-*).
inj = pi.PersonaInjector()
inj.personas = {"implementation": "IMPL_XYZ", "architecture": "ARCH_XYZ", "review": "REV_XYZ"}
inj.alias = {"claude-sonnet-4-6": "ailocal-implementation"}

P = "IMPL_XYZ"
fails = 0
def check(cond, name):
    global fails
    print(f"  {'✓' if cond else '✗'} {name}")
    if not cond:
        fails += 1

def hook(data, call_type):
    return asyncio.run(inj.async_pre_call_hook(None, None, data, call_type))

# ── OpenAI route ────────────────────────────────────────────────────────────────
d = hook({"model": "ailocal-implementation", "messages": [{"role": "user", "content": "hi"}]}, "acompletion")
sys0 = d["messages"][0]
check(sys0["role"] == "system" and sys0["content"].startswith(P),
      "openai: persona inserted as system when none present")

d = hook({"model": "ailocal-implementation",
          "messages": [{"role": "system", "content": "CLIENT_SYS"}, {"role": "user", "content": "hi"}]}, "acompletion")
c = d["messages"][0]["content"]
check(P in c and "CLIENT_SYS" in c and c.index(P) < c.index("CLIENT_SYS"),
      "openai: persona prepended to existing system, client text preserved")

# ── Anthropic route (/v1/messages) — top-level `system` ─────────────────────────
d = hook({"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]}, "anthropic_messages")
check(d.get("system") == P and all(m["role"] != "system" for m in d["messages"]),
      "anthropic: persona set as top-level system when absent (compat alias resolved)")

d = hook({"model": "claude-sonnet-4-6", "system": "CLIENT_SYS",
          "messages": [{"role": "user", "content": "hi"}]}, "anthropic_messages")
check(isinstance(d["system"], str) and d["system"].startswith(P) and "CLIENT_SYS" in d["system"],
      "anthropic: persona prepended to string system, client text preserved")

d = hook({"model": "claude-sonnet-4-6", "system": [{"type": "text", "text": "CLIENT_SYS"}],
          "messages": [{"role": "user", "content": "hi"}]}, "anthropic_messages")
blocks = d["system"]
check(isinstance(blocks, list) and blocks[0].get("text") == P
      and any(b.get("text") == "CLIENT_SYS" for b in blocks[1:]),
      "anthropic: persona prepended as a text block to list system")

# ── never-inject cases ──────────────────────────────────────────────────────────
d = hook({"model": "ailocal-completion", "messages": [{"role": "user", "content": "hi"}]}, "acompletion")
check(all(m["role"] != "system" for m in d["messages"]),
      "openai: no persona for a capability without a persona file (completion)")

d = hook({"model": "ailocal-completion", "messages": [{"role": "user", "content": "hi"}]}, "anthropic_messages")
check("system" not in d,
      "anthropic: no persona for a capability without a persona file (completion)")

d = hook({"model": "ailocal-implementation", "input": "x"}, "embeddings")
check("system" not in d and "messages" not in d,
      "embeddings call_type: request passes through untouched")

# ── idempotency ─────────────────────────────────────────────────────────────────
data = {"model": "ailocal-implementation", "messages": [{"role": "user", "content": "hi"}]}
hook(data, "acompletion"); hook(data, "acompletion")
check(data["messages"][0]["content"].count(P) == 1, "openai: injection is idempotent (no doubling)")

data = {"model": "claude-sonnet-4-6", "system": "CLIENT_SYS", "messages": [{"role": "user", "content": "hi"}]}
hook(data, "anthropic_messages"); hook(data, "anthropic_messages")
check(data["system"].count(P) == 1, "anthropic: injection is idempotent (no doubling)")

print()
if fails:
    print(f"PERSONA INJECTION TESTS: {fails} FAILED")
    sys.exit(1)
print("PERSONA INJECTION TESTS: OK")
