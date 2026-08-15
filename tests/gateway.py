#!/usr/bin/env python3
"""Gateway-side request handling: tool-call repair, system-message transport.

  repair    recovery of fenced JSON tool calls, and the harder direction --
            refusing to fire on tutorial examples.
  transport interleaved role:"system" entries surviving the Anthropic adapter,
            in their original positions.

Persona injection was the other section; the mechanism it covered is gone (see
the note below). The docstring also advertised a `trace` section that was never
in SECTIONS.

Usage: gateway.py [repair|transport]   (default: all)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402

_suite = Suite()
check = _suite.check

# ── (persona injection removed) ────────────────────────────────────────
# The injector merged a per-capability system prompt into every request. Its
# content was measured twice: the 34-line version made a toolless model invent
# <call:ls_tree>/<call:read_file> and fail to finish; the 12-line replacement
# produced no measurable difference through claude-local WITH tools (identical
# answers and tool counts on two tasks, 416 vs 454 words on a third). A
# subsystem delivering an unmeasurable effect is not a subsystem worth having,
# so the hook, the instruction files and the profile flag are gone.

# ── repair ──────────────────────────────────────────────────────────────
import os
import sys
import types
from pathlib import Path
try:
    from litellm.integrations.custom_logger import CustomLogger  # noqa: F401
except ImportError:
    _c = types.ModuleType("litellm.integrations.custom_logger")
    class _CL:
        def __init__(self, *a, **k): pass
    _c.CustomLogger = _CL
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
    sys.modules["litellm.integrations.custom_logger"] = _c
tr = load_module("tool_repair", os.environ.get(
    "AILOCAL_TOOL_REPAIR", RESOURCES / "deploy/litellm/hooks/tool_repair.py"))
TOOLS = [
    {"name": "Read", "description": "Read a file",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"}},
                      "required": ["file_path"]}},
    {"name": "Bash", "description": "Run a command",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
]

# ── trace ───────────────────────────────────────────────────────────────
import json
import re
import sys
from pathlib import Path

def repair_checks() -> None:
    print("\nMUST REPAIR — the reply IS the call")
    calls, left = tr.recover(
        '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/a.py"}}\n```',
        TOOLS)
    check(calls is not None and len(calls) == 1, "sole fenced JSON becomes a tool call")
    if calls:
        check(calls[0]["function"]["name"] == "Read", "correct tool name")
        import json as _j
        check(_j.loads(calls[0]["function"]["arguments"])["file_path"] == "/tmp/a.py",
              "arguments preserved with correct types")
    check(not left, "no leftover text when the fence was the whole reply")
    calls, _ = tr.recover(
        '```\n{"name": "Bash", "arguments": {"command": "ls"}}\n```', TOOLS)
    check(calls is not None, "unlabelled fence also repaired")
    calls, _ = tr.recover('{"name": "Read", "arguments": {"file_path": "/tmp/b"}}', TOOLS)
    check(calls is not None, "bare JSON (no fence) still repaired — existing path intact")
    calls, _ = tr.recover(
        '<function=Read>\n<parameter=file_path>/tmp/c</parameter>\n</function>', TOOLS)
    check(calls is not None, "qwen <function=> format still repaired — existing path intact")
    print("\nMUST NOT REPAIR — the fence is an example, not a call")
    prose = ('To read a file, the agent emits a call like this:\n\n'
             '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/x"}}\n```\n\n'
             'That is how the protocol works.')
    calls, left = tr.recover(prose, TOOLS)
    check(calls is None, "fenced example SURROUNDED BY PROSE is not executed")
    check(left == prose, "the explanation is returned unchanged")
    two = ('```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
           '```json\n{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n```')
    calls, _ = tr.recover(two, TOOLS)
    check(calls is None, "TWO fences are never auto-executed, even if both validate")
    calls, _ = tr.recover(
        '```json\n{"name": "DropDatabase", "arguments": {"db": "prod"}}\n```', TOOLS)
    check(calls is None, "a fenced call to an UNDECLARED tool is refused")
    calls, _ = tr.recover(
        '```json\n{"name": "Read", "arguments": {"wrong_field": "x"}}\n```', TOOLS)
    check(calls is None, "a fenced call missing a REQUIRED argument is refused")
    calls, _ = tr.recover('```python\nprint("hello")\n```', TOOLS)
    check(calls is None, "an ordinary code fence is not a tool call")
    calls, _ = tr.recover('Here is some JSON: {"name": "config", "value": 1}', TOOLS)
    check(calls is None, "JSON that is not a tool-call shape is left alone")
    print("\nGUARDS")
    calls, left = tr.recover("Just a normal answer.", TOOLS)
    check(calls is None and left == "Just a normal answer.", "plain prose untouched")
    calls, _ = tr.recover('```json\n{"name":"Read","arguments":{"file_path":"/a"}}\n```', [])
    check(calls is None, "no declared tools -> never repairs")
    calls, _ = tr.recover("", TOOLS)
    check(calls is None, "empty content -> no crash")


# ── transport ───────────────────────────────────────────────────────────
# LiteLLM's Anthropic /v1/messages -> OpenAI adapter branches only on role
# user/assistant, so a role:"system" entry inside the messages array is
# silently discarded at zero token cost. system_transport.py restores it.
# These checks pin the two properties that matter and are easy to lose:
# leading entries are hoisted, mid-conversation entries stay WHERE THEY WERE.
import asyncio

st = load_module("system_transport", os.environ.get(
    "AILOCAL_SYSTEM_TRANSPORT_MODULE",
    RESOURCES / "deploy/litellm/hooks/system_transport.py"))


class _Route:
    """Stub registry: the route is the only thing the hook asks it for."""

    def __init__(self, route):
        self._route = route

    def route_for_call_type(self, call_type, has_input_key=False):
        return self._route


def _run(data, route="/v1/messages"):
    hook = st.SystemTransport(registry=_Route(route))
    return asyncio.run(hook.async_pre_call_hook(None, None, data, "acompletion"))


def _roles(data):
    return [m["role"] for m in data["messages"]]


def _texts(data):
    return [m.get("content") for m in data["messages"]]


def transport_checks():
    print("\nHOISTING (leading entries)")
    out = _run({"messages": [
        {"role": "system", "content": "BOOT"},
        {"role": "user", "content": "hi"}]})
    check(_roles(out) == ["user"] and out.get("system") == "BOOT",
          "a leading system entry is hoisted to the top-level field")

    out = _run({"system": "OUTER", "messages": [
        {"role": "system", "content": "BOOT"},
        {"role": "user", "content": "hi"}]})
    check(out["system"] == "OUTER\n\nBOOT",
          "hoisted text is APPENDED after an existing top-level system")

    out = _run({"system": "OUTER", "messages": [{"role": "user", "content": "hi"}]})
    check(out["system"] == "OUTER" and _roles(out) == ["user"],
          "a top-level system with no in-array entries is untouched")

    print("\nPOSITION (mid-conversation entries)")
    out = _run({"messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "system", "content": "MID"},
        {"role": "user", "content": "q2"}]})
    check(_roles(out) == ["user", "assistant", "user", "user"],
          "a mid-conversation entry becomes an in-place message")
    check("MID" in _texts(out)[2] and out.get("system") is None,
          "it is NOT hoisted into the top-level system field")
    check(_texts(out)[1] == "a1" and _texts(out)[3] == "q2",
          "the turns either side of it are unchanged")

    out = _run({"messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "system", "content": "FIRST"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "system", "content": "SECOND"},
        {"role": "user", "content": "q3"}]})
    joined = [t for t in _texts(out)]
    check(joined.index([t for t in joined if "FIRST" in t][0])
          < joined.index([t for t in joined if "SECOND" in t][0]),
          "multiple reminders keep their relative order")
    check(joined.index([t for t in joined if "FIRST" in t][0]) == 2,
          "a reminder after an assistant turn is not moved before it")

    print("\nCONTENT")
    out = _run({"messages": [
        {"role": "user", "content": "q"},
        {"role": "system", "content": [
            {"type": "text", "text": "Plan mode is active."},
            {"type": "text", "text": "Write to /plans/x.md"}]}]})
    body = _texts(out)[1]
    check("Plan mode is active." in body and "/plans/x.md" in body,
          "a block-list system entry survives with all its text")
    check(body.count("<system-reminder>") == 1,
          "harness speech is marked exactly once")

    out = _run({"messages": [
        {"role": "user", "content": "q"},
        {"role": "system",
         "content": "<system-reminder>\nalready wrapped\n</system-reminder>"}]})
    check(_texts(out)[1].count("<system-reminder>") == 1,
          "an already-wrapped reminder is not double-wrapped")

    out = _run({"messages": [
        {"role": "user", "content": "q"},
        {"role": "system", "content": "SessionStart hook additional context: 512 chunks"},
        {"role": "user", "content": "q2"}]})
    check("512 chunks" in _texts(out)[1],
          "SessionStart/Cadence context survives translation")

    print("\nNO DUPLICATION / NO SIDE EFFECTS")
    out = _run({"messages": [
        {"role": "user", "content": "q"},
        {"role": "system", "content": "ONCE"},
        {"role": "user", "content": "q2"}]})
    check(sum("ONCE" in str(t) for t in _texts(out)) == 1
          and "ONCE" not in (out.get("system") or ""),
          "a reminder is delivered once, not in both places")

    plain = {"messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"}]}
    out = _run(dict(plain))
    check(out["messages"] == plain["messages"] and "system" not in out,
          "ordinary user/assistant history is passed through unchanged")

    out = _run({"messages": [
        {"role": "user", "content": "q"},
        {"role": "system", "content": "   "},
        {"role": "user", "content": "q2"}]})
    check(_roles(out) == ["user", "user"],
          "an empty reminder is dropped rather than sent as empty content")

    print("\nSCOPE")
    other = {"messages": [
        {"role": "user", "content": "q"},
        {"role": "system", "content": "MID"}]}
    out = _run(dict(other), route="/v1/chat/completions")
    check(out["messages"] == other["messages"],
          "a non-Anthropic route is left alone (no hosted-client change)")

    for streaming in (True, False):
        out = _run({"stream": streaming, "messages": [
            {"role": "user", "content": "q"},
            {"role": "system", "content": "MID"},
            {"role": "user", "content": "q2"}]})
        check(out["stream"] is streaming and "MID" in _texts(out)[1],
              f"stream={streaming} is preserved and the reminder survives")

    print("\nFAIL-OPEN")
    check(_run({"messages": []})["messages"] == [],
          "an empty messages array does not crash")
    weird = {"messages": [{"role": "user", "content": "q"}, "not-a-dict"]}
    check(_run(dict(weird))["messages"][1] == "not-a-dict",
          "an unexpected message shape is passed through, not dropped")


SECTIONS = {"repair": repair_checks, "transport": transport_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
