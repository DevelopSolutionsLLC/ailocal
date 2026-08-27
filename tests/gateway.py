#!/usr/bin/env python3
"""Gateway-side request handling: tool-call repair, system-message transport.

  repair    recovery of fenced JSON tool calls, and the harder direction --
            refusing to fire on tutorial examples.
  native-args
            completion of ONE missing UI-metadata argument on a native call,
            and the much longer list of cases that must fail closed.
  transport interleaved role:"system" entries surviving the Anthropic adapter,
            in their original positions.

Persona injection was the other section; the mechanism it covered is gone (see
the note below). The docstring also advertised a `trace` section that was never
in SECTIONS.

Usage: gateway.py [repair|native-args|transport]   (default: all)
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

# Claude Code's real Agent schema, as captured off the wire. `subagent_type` is
# NOT required — the tool defaults to general-purpose — which is why the repair
# treats it as "valid if present" rather than mandatory.
AGENT_TOOL = {
    "name": "Agent", "description": "Launch a new agent to handle complex tasks.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string",
                            "description": "A short (3-5 word) description of the task"},
            "prompt": {"type": "string", "description": "The task for the agent"},
            "subagent_type": {"type": "string", "description": "Agent type"},
            "run_in_background": {"type": "boolean", "description": "Background"},
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
}
AGENT_TOOLS = TOOLS + [AGENT_TOOL]

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


# ── native-args ─────────────────────────────────────────────────────────
# gemma4 emits a perfectly-parsed Agent call and omits the required
# `description`. complete_ui_metadata() adds it; everything else it refuses.
# The refusals are the point of the section — a rule that fabricates one field
# is only safe if it provably fabricates nothing else.
import json as _json


_DEFAULT = object()


def _complete(args, tools=_DEFAULT, name="Agent"):
    declared = AGENT_TOOLS if tools is _DEFAULT else tools
    return tr.complete_ui_metadata(name, _json.dumps(args), declared)


def native_arg_checks() -> None:
    print("\nMUST COMPLETE — native Agent call missing only `description`")
    out = _complete({"prompt": "Search for existing retry logic and backoff helpers",
                     "subagent_type": "Explore"})
    check(out is not None, "missing `description` is completed")
    if out:
        got = _json.loads(out)
        check(got["description"] == "Search for existing retry logic",
              "description is the first 5 prompt words, deterministically")
        check(got["prompt"] == "Search for existing retry logic and backoff helpers",
              "prompt is unchanged")
        check(got["subagent_type"] == "Explore", "subagent_type is unchanged")
        check(set(got) == {"description", "prompt", "subagent_type"},
              "no other argument is added")
        again = _complete(got)
        check(again is None, "idempotent — a completed call is not touched twice")
        check(_complete({"prompt": got["prompt"], "subagent_type": "Explore"}) == out,
              "deterministic — same input, same bytes")
    long_prompt = " ".join(["word"] * 200)
    out = _complete({"prompt": long_prompt, "subagent_type": "Explore"})
    check(out is not None and len(_json.loads(out)["description"]) <= 60,
          "description stays bounded on a very long prompt")
    out = _complete({"prompt": "Explore  \n  the   repo", "subagent_type": "Explore"})
    check(out and _json.loads(out)["description"] == "Explore the repo",
          "whitespace is collapsed, never leaked into the label")
    check(_complete({"prompt": "Find the config loader"}) is not None,
          "subagent_type is OPTIONAL in the schema, so its absence still completes")

    print("\nMUST NOT COMPLETE — anything else")
    check(_complete({"description": "Explore repo", "prompt": "p"}) is None,
          "an Agent call that ALREADY has a description is untouched")
    check(_complete({"description": "", "prompt": "p"}) is None,
          "a present-but-empty description is the model's business, not ours")
    check(_complete({"subagent_type": "Explore"}) is None,
          "Agent missing `prompt` is refused — normal validation must fire")
    check(_complete({"prompt": "   ", "subagent_type": "Explore"}) is None,
          "a blank prompt is not a usable label source")
    check(_complete({"prompt": "p", "subagent_type": ""}) is None,
          "a malformed subagent_type fails the whole repair closed")
    check(_complete({"prompt": "p", "subagent_type": 7}) is None,
          "a wrongly-typed subagent_type fails closed")
    check(tr.complete_ui_metadata("Agent", "{not json", AGENT_TOOLS) is None,
          "malformed arguments JSON is never rewritten")
    check(tr.complete_ui_metadata("Agent", '["a"]', AGENT_TOOLS) is None,
          "arguments that are not an object are never rewritten")
    check(_complete({"file_path": "/a"}, name="Read") is None,
          "another tool missing a required field is untouched")
    check(_complete({"prompt": "p"}, name="Bash") is None,
          "Bash is not eligible, whatever it is missing")
    check(tr.complete_ui_metadata("Agent", '{"prompt": "p"}', TOOLS) is None,
          "an UNDECLARED Agent tool is refused")
    check(_complete({"prompt": "p"}, tools=[]) is None, "no declared tools -> refuses")

    print("\nMUST NOT DRIFT — a future required field cannot be fabricated")
    future = _json.loads(_json.dumps(AGENT_TOOL))
    future["input_schema"]["required"] = ["description", "prompt", "run_in_background"]
    check(tr.complete_ui_metadata("Agent", '{"prompt": "p"}', TOOLS + [future]) is None,
          "TWO missing required fields -> no repair, the client sees the error")
    typed = _json.loads(_json.dumps(AGENT_TOOL))
    typed["input_schema"]["properties"]["description"] = {"type": "object"}
    check(tr.complete_ui_metadata("Agent", '{"prompt": "p"}', TOOLS + [typed]) is None,
          "a `description` that is no longer a string is never synthesised")

    # The unit rule is only worth anything if it reaches the byte stream Claude
    # Code actually reads, so drive the real SSE path end to end.
    print("\nSSE — the completed call reaches the client")

    def _sse_run(events, tools=AGENT_TOOLS):
        """Drive the real byte path; return (emitted events, reassembled args)."""
        state = {"saw_tool_use": False, "ctx": None}
        out = []
        for ev in events:
            raw = (f"event: {ev['type']}\ndata: {_json.dumps(ev)}\n\n").encode()
            out += tr.ToolRepair._anthropic_sse_repair(raw, state, tools)
        emitted, args_text = [], ""
        for chunk in out:
            payload = _json.loads(chunk.decode().split("data:", 1)[1].strip())
            emitted.append(payload)
            if payload.get("type") == "content_block_delta":
                args_text += (payload.get("delta") or {}).get("partial_json") or ""
        try:
            # Convenience for the single-call cases; multi-call cases read
            # `emitted` per index instead.
            return emitted, _json.loads(args_text)
        except ValueError:
            return emitted, None

    def _tool_block(args_json, name="Agent"):
        return [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": name,
                               "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": args_json}},
            {"type": "content_block_stop", "index": 0},
        ]

    events, args = _sse_run(_tool_block('{"prompt": "Find the retry helper", '
                                        '"subagent_type": "Explore"}'))
    check(args is not None and args.get("description") == "Find the retry helper",
          "SSE: the completed argument reaches the wire")
    check(args and args["prompt"] == "Find the retry helper" and
          args["subagent_type"] == "Explore", "SSE: the model's arguments survive intact")
    start = next(e for e in events if e["type"] == "content_block_start")
    check(start["content_block"]["id"] == "toolu_1" and
          start["content_block"]["name"] == "Agent", "SSE: block identity is preserved")
    check([e["type"] for e in events] ==
          ["content_block_start", "content_block_delta", "content_block_stop"],
          "SSE: a well-formed block sequence is emitted")
    # Arguments split across deltas, as Ollama actually streams them.
    _, split = _sse_run([
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t2", "name": "Agent", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"prompt": "Find th'}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": 'e retry helper"}'}},
        {"type": "content_block_stop", "index": 0},
    ])
    check(split and split.get("description") == "Find the retry helper",
          "SSE: fragmented arguments are reassembled before the decision")
    _, good = _sse_run(_tool_block('{"description": "Find retry", "prompt": "Go"}'))
    check(good == {"description": "Find retry", "prompt": "Go"},
          "SSE: a complete call passes through byte-equivalent, nothing added")
    _, other = _sse_run(_tool_block('{"command": "ls"}', name="Bash"))
    check(other == {"command": "ls"},
          "SSE: a non-eligible tool streams through untouched")

    # A stream cut off inside a tool_use block must not swallow it. Driven
    # through the real iterator hook, since that is where the flush lives.
    import asyncio as _asyncio

    async def _truncated():
        events = _tool_block('{"prompt": "Find the ret')[:2]   # no stop event
        async def _stream():
            for ev in events:
                yield (f"event: {ev['type']}\ndata: {_json.dumps(ev)}\n\n").encode()
        hook = tr.ToolRepair()
        return [c async for c in hook.async_post_call_streaming_iterator_hook(
            None, _stream(), {"tools": AGENT_TOOLS, "messages": []})]

    out = _asyncio.run(_truncated())
    body = b"".join(out).decode()
    check("Find the ret" in body and "tool_use" in body,
          "SSE: a stream truncated mid-call flushes it rather than dropping it")

    # Buffering one block at a time must not reorder a multi-call response, and
    # must not touch the thinking/text blocks it shares the stream with.
    def _block(index, btype, name=None, args=None, text=None):
        start = {"type": btype} if btype != "tool_use" else {
            "type": "tool_use", "id": f"t{index}", "name": name, "input": {}}
        delta = ({"type": "input_json_delta", "partial_json": args} if btype == "tool_use"
                 else {"type": f"{btype}_delta", btype: text})
        return [
            {"type": "content_block_start", "index": index, "content_block": start},
            {"type": "content_block_delta", "index": index, "delta": delta},
            {"type": "content_block_stop", "index": index},
        ]

    seq = (_block(0, "thinking", text="Let me delegate this.")
           + _block(1, "text", text="Delegating now.")
           + _block(2, "tool_use", name="Agent", args='{"prompt": "Find the retry code"}')
           + _block(3, "tool_use", name="Bash", args='{"command": "ls"}'))
    emitted, _ = _sse_run(seq)
    starts = [(e["index"], e["content_block"]["type"])
              for e in emitted if e["type"] == "content_block_start"]
    check(starts == [(0, "thinking"), (1, "text"), (2, "tool_use"), (3, "tool_use")],
          "SSE: block order and indices survive a multi-call response")
    thinking = [e for e in emitted if e.get("delta", {}).get("type") == "thinking_delta"]
    check(len(thinking) == 1 and thinking[0]["delta"]["thinking"] == "Let me delegate this.",
          "SSE: a thinking block passes through byte-identical alongside a repair")
    per_call = {}
    for e in emitted:
        if (e.get("delta") or {}).get("type") == "input_json_delta":
            per_call[e["index"]] = _json.loads(e["delta"]["partial_json"])
    check(per_call[2].get("description") == "Find the retry code",
          "SSE: the Agent call in a multi-call response is completed")
    check(per_call[3] == {"command": "ls"},
          "SSE: the sibling Bash call in the same response is untouched")


SECTIONS = {"repair": repair_checks, "native-args": native_arg_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
