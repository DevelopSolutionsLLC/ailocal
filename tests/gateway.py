#!/usr/bin/env python3
"""Test gateway tool-call repair (text, SSE bytes) and proxy auth on the host.

Only the auth check leaves the process: it calls the proxy at AILOCAL_PROXY.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite, load_module  # noqa: E402

try:
    from litellm.integrations.custom_logger import CustomLogger  # noqa: F401
except ImportError:  # host without LiteLLM: the hook only subclasses CustomLogger
    _c = types.ModuleType("litellm.integrations.custom_logger")
    _c.CustomLogger = type("CustomLogger", (), {"__init__": lambda self, *a, **k: None})
    sys.modules.update({"litellm": types.ModuleType("litellm"),
                        "litellm.integrations": types.ModuleType("litellm.integrations"),
                        "litellm.integrations.custom_logger": _c})
tr = load_module("tool_repair", RESOURCES / "deploy/litellm/hooks/tool_repair.py")
suite = Suite("gateway (host)")
check = suite.check


def _tool(name, props, required):
    return {"name": name, "description": name, "input_schema": {
        "type": "object", "properties": {p: {"type": t} for p, t in props.items()},
        "required": required}}


TOOLS = [_tool("Read", {"file_path": "string"}, ["file_path"]),
         _tool("Bash", {"command": "string"}, ["command"])]
# Claude Code's Agent schema as captured: subagent_type is optional.
AGENT_TOOLS = TOOLS + [_tool("Agent", {"description": "string", "prompt": "string",
                                       "subagent_type": "string"}, ["description", "prompt"])]

suite.section("REPAIR: a reply that is only a call becomes the call")
repaired = {}
for label, text in (("json fence", '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/a"}}\n```'),
                    ("bare JSON", '{"name": "Read", "arguments": {"file_path": "/tmp/a"}}'),
                    ("qwen <function=>", '<function=Read>\n<parameter=file_path>/tmp/a</parameter>\n</function>')):
    calls, left = tr.recover(text, TOOLS)
    repaired[label] = ([(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls or []], left or "")
check(all(v == ([("Read", {"file_path": "/tmp/a"})], "") for v in repaired.values()),
      "a call emitted as text becomes exactly one tool call (dropped or duplicated call)", str(repaired))
prose = ('To read a file, the agent emits a call like this:\n\n'
         '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/x"}}\n```\n\nThat is how it works.')
accepted = [label for text, label in (
        (prose, "a fenced example inside prose"),
        ('```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
         '```json\n{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n```', "two fences"),
        ('```json\n{"name": "DropDatabase", "arguments": {"db": "prod"}}\n```', "an undeclared tool"),
        ('```json\n{"name": "Read", "arguments": {"wrong_field": "x"}}\n```', "a missing required arg"))
    if tr.recover(text, TOOLS) != (None, text)]
check(not accepted, "an example or invalid call in text is never executed", str(accepted))

suite.section("SSE: native tool calls on the Anthropic byte stream")


def wire(ev):
    return f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()


def block(i, kind, name=None, *parts):
    start = {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}} if kind == "tool_use" else {"type": kind}
    deltas = [{"type": "input_json_delta", "partial_json": p} if kind == "tool_use"
              else {"type": f"{kind}_delta", kind: p} for p in parts]
    return ([{"type": "content_block_start", "index": i, "content_block": start}]
            + [{"type": "content_block_delta", "index": i, "delta": d} for d in deltas]
            + [{"type": "content_block_stop", "index": i}])


state, emitted = {"saw_tool_use": False, "ctx": None}, []
for ev in (block(0, "thinking", None, "Let me delegate.")
           + block(1, "tool_use", "Agent", '{"prompt": "Find th', 'e retry code"}')
           + block(2, "tool_use", "Bash", '{"command": "ls"}')):
    emitted += [json.loads(c.decode().split("data:", 1)[1])
                for c in tr.ToolRepair._anthropic_sse_repair(wire(ev), state, AGENT_TOOLS)]
args = {}
for e in emitted:
    if (e.get("delta") or {}).get("type") == "input_json_delta":
        args[e["index"]] = args.get(e["index"], "") + e["delta"]["partial_json"]
got = ([(e["index"], e["content_block"]["type"]) for e in emitted if e["type"] == "content_block_start"],
       {i: json.loads(s) for i, s in args.items()})
check(got == ([(0, "thinking"), (1, "tool_use"), (2, "tool_use")],
              {1: {"prompt": "Find the retry code", "description": "Find the retry code"}, 2: {"command": "ls"}}),
      "a buffered native call is emitted once, completed, in order, beside its sibling call", str(got))


async def truncated():
    async def stream():
        for ev in block(0, "tool_use", "Agent", '{"prompt": "Find the ret')[:2]:
            yield wire(ev)
    hook = tr.ToolRepair().async_post_call_streaming_iterator_hook(
        None, stream(), {"tools": AGENT_TOOLS, "messages": []})
    return b"".join([c async for c in hook]).decode()

body = asyncio.run(truncated())
check("Find the ret" in body and "tool_use" in body, "a stream cut mid-call is flushed, not swallowed")

suite.section("AUTH (running proxy)")
from ailocal.checks import services as S  # noqa: E402

try:  # LiteLLM logs an expected auth traceback for this request.
    with urllib.request.urlopen(S.PROXY + "/v1/models", timeout=15) as r:
        status = r.status
except urllib.error.HTTPError as e:
    status = e.code
except OSError as e:
    status = f"unreachable: {e}"
check(status in (401, 403), "the running proxy refuses a request without the master key", str(status))

sys.exit(suite.report())
