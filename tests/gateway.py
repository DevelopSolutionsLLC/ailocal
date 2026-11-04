#!/usr/bin/env python3
"""Gateway request handling on the host: tool-call repair, Agent metadata completion,
re-streaming de-streamed Responses (LiteLLM#42770), and the client probe routes.
Probe checks hit the running proxy at AILOCAL_PROXY; nothing else leaves the process.
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
AGENT = _tool("Agent", {"description": "string", "prompt": "string", "subagent_type": "string",
                        "run_in_background": "boolean"}, ["description", "prompt"])
AGENT["input_schema"]["additionalProperties"] = False
AGENT_TOOLS = TOOLS + [AGENT]

# ── repair ──────────────────────────────────────────────────────────────
suite.section("REPAIR: the reply is the call")
calls, left = tr.recover('```json\n{"name": "Read", "arguments": {"file_path": "/tmp/a.py"}}\n```', TOOLS)
check(calls and len(calls) == 1 and calls[0]["function"]["name"] == "Read"
      and json.loads(calls[0]["function"]["arguments"]) == {"file_path": "/tmp/a.py"} and not left,
      "a sole fenced JSON call becomes one tool call, nothing left over")
for text, label in (('```\n{"name": "Bash", "arguments": {"command": "ls"}}\n```', "unlabelled fence"),
                    ('{"name": "Read", "arguments": {"file_path": "/tmp/b"}}', "bare JSON"),
                    ('<function=Read>\n<parameter=file_path>/tmp/c</parameter>\n</function>',
                     "qwen <function=> format")):
    check(tr.recover(text, TOOLS)[0] is not None, f"{label} is repaired")

suite.section("REPAIR: refuses examples and invalid calls")
prose = ('To read a file, the agent emits a call like this:\n\n'
         '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/x"}}\n```\n\nThat is how it works.')
check(tr.recover(prose, TOOLS) == (None, prose), "a fenced example inside prose is returned unchanged")
for text, tools, label in (
        ('```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
         '```json\n{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n```', TOOLS, "two fences"),
        ('```json\n{"name": "DropDatabase", "arguments": {"db": "prod"}}\n```', TOOLS, "an undeclared tool"),
        ('```json\n{"name": "Read", "arguments": {"wrong_field": "x"}}\n```', TOOLS, "a missing required arg"),
        ('```python\nprint("hello")\n```', TOOLS, "an ordinary code fence"),
        ('Here is some JSON: {"name": "config", "value": 1}', TOOLS, "non-call JSON"),
        ('```json\n{"name":"Read","arguments":{"file_path":"/a"}}\n```', [], "no declared tools"),
        ("", TOOLS, "empty content")):
    check(tr.recover(text, tools)[0] is None, f"not repaired: {label}")
check(tr.recover("Just a normal answer.", TOOLS) == (None, "Just a normal answer."), "plain prose untouched")

# ── native-args ─────────────────────────────────────────────────────────
# gemma4 emits a well-formed Agent call without the required `description`;
# exactly that one field may be synthesised, and everything else fails closed.
suite.section("NATIVE ARGS: complete only a missing Agent description")


def complete(args, name="Agent", tools=AGENT_TOOLS):
    return tr.complete_ui_metadata(name, args if isinstance(args, str) else json.dumps(args), tools)


out = complete({"prompt": "Search for existing retry logic and backoff helpers", "subagent_type": "Explore"})
got = json.loads(out) if out else {}
check(got == {"description": "Search for existing retry logic",
              "prompt": "Search for existing retry logic and backoff helpers", "subagent_type": "Explore"},
      "description is the first five prompt words; nothing else changes", out or "")
check(out and complete(got) is None, "idempotent")
long = complete({"prompt": " ".join(["word"] * 200)})
check(long and len(json.loads(long)["description"]) <= 60, "bounded on a long prompt")
ws = complete({"prompt": "Explore  \n  the   repo"})
check(ws and json.loads(ws)["description"] == "Explore the repo", "whitespace collapsed")
future = json.loads(json.dumps(AGENT))
future["input_schema"]["required"].append("run_in_background")
typed = json.loads(json.dumps(AGENT))
typed["input_schema"]["properties"]["description"] = {"type": "object"}
for args, name, tools, label in (
        ({"description": "Explore repo", "prompt": "p"}, "Agent", AGENT_TOOLS, "already described"),
        ({"description": "", "prompt": "p"}, "Agent", AGENT_TOOLS, "empty description present"),
        ({"subagent_type": "Explore"}, "Agent", AGENT_TOOLS, "missing prompt"),
        ({"prompt": "   "}, "Agent", AGENT_TOOLS, "blank prompt"),
        ({"prompt": "p", "subagent_type": 7}, "Agent", AGENT_TOOLS, "wrongly typed subagent_type"),
        ("{not json", "Agent", AGENT_TOOLS, "malformed JSON"),
        ('["a"]', "Agent", AGENT_TOOLS, "non-object arguments"),
        ({"prompt": "p"}, "Bash", AGENT_TOOLS, "another tool"),
        ({"prompt": "p"}, "Agent", TOOLS, "undeclared Agent"),
        ({"prompt": "p"}, "Agent", TOOLS + [future], "two missing required fields"),
        ({"prompt": "p"}, "Agent", TOOLS + [typed], "description no longer a string")):
    check(complete(args, name, tools) is None, f"refused: {label}")

suite.section("NATIVE ARGS: through the SSE byte path")


def sse(events):
    state, out = {"saw_tool_use": False, "ctx": None}, []
    for ev in events:
        out += tr.ToolRepair._anthropic_sse_repair(
            f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode(), state, AGENT_TOOLS)
    return [json.loads(c.decode().split("data:", 1)[1]) for c in out]


def block(i, kind, name=None, *parts):
    start = {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}} if kind == "tool_use" else {"type": kind}
    deltas = [{"type": "input_json_delta", "partial_json": p} if kind == "tool_use"
              else {"type": f"{kind}_delta", kind: p} for p in parts]
    return ([{"type": "content_block_start", "index": i, "content_block": start}]
            + [{"type": "content_block_delta", "index": i, "delta": d} for d in deltas]
            + [{"type": "content_block_stop", "index": i}])


def args_by_index(emitted):
    acc = {}
    for e in emitted:
        if (e.get("delta") or {}).get("type") == "input_json_delta":
            acc[e["index"]] = acc.get(e["index"], "") + e["delta"]["partial_json"]
    return {i: json.loads(s) for i, s in acc.items()}


emitted = sse(block(0, "tool_use", "Agent", '{"prompt": "Find th', 'e retry helper"}'))
check(args_by_index(emitted)[0] == {"prompt": "Find the retry helper", "description": "Find the retry helper"},
      "fragmented arguments are reassembled and completed on the wire")
check([e["type"] for e in emitted] == ["content_block_start", "content_block_delta", "content_block_stop"]
      and emitted[0]["content_block"]["id"] == "t0", "well-formed block, identity preserved")
emitted = sse(block(0, "thinking", None, "Let me delegate.") + block(1, "text", None, "Delegating.")
              + block(2, "tool_use", "Agent", '{"prompt": "Find the retry code"}')
              + block(3, "tool_use", "Bash", '{"command": "ls"}'))
check([(e["index"], e["content_block"]["type"]) for e in emitted if e["type"] == "content_block_start"]
      == [(0, "thinking"), (1, "text"), (2, "tool_use"), (3, "tool_use")], "multi-block order preserved")
a = args_by_index(emitted)
check(a[2].get("description") == "Find the retry code" and a[3] == {"command": "ls"},
      "only the Agent call is completed; its sibling is untouched")
fix = tr.ToolRepair._fix_anthropic_sse_stop_reason
end_turn = b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}\n\n'
fixed = fix(end_turn, {"injected": True})
check(fix(end_turn, {"saw_tool_use": True}) is None and fixed and b'"stop_reason": "tool_use"' in fixed,
      "stop_reason rewritten only after an injected call (native: fixed upstream, #39010)")


async def truncated():
    async def stream():
        for ev in block(0, "tool_use", "Agent", '{"prompt": "Find the ret')[:2]:
            yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
    hook = tr.ToolRepair().async_post_call_streaming_iterator_hook(
        None, stream(), {"tools": AGENT_TOOLS, "messages": []})
    return b"".join([c async for c in hook]).decode()

body = asyncio.run(truncated())
check("Find the ret" in body and "tool_use" in body, "a stream cut mid-call is flushed, not dropped")

# ── destream ────────────────────────────────────────────────────────────
suite.section("DESTREAM: de-streamed /v1/responses web search (LiteLLM#42770)")


class ResponsesAPIResponse:  # the predicate keys on the class name
    status, output, _hidden_params = "completed", [], {"model_id": "m1"}


flagged = {"_websearch_interception_converted_stream": True}
resp = ResponsesAPIResponse()
check(tr._destreamed_responses_result(flagged, resp), "flagged request + Responses result recognised")
check(not any(tr._destreamed_responses_result(d, r) for d, r in
              (({}, resp), (flagged, object()), (None, resp))),
      "unflagged, non-Responses and missing data are left alone")
hook = tr.ToolRepair().async_post_call_success_deployment_hook
check(asyncio.run(hook({}, resp, "aresponses")) is None, "no replacement when not de-streamed")
stream = asyncio.run(hook(flagged, resp, "aresponses"))


async def drain(s):
    return [e async for e in s]

events = asyncio.run(drain(stream)) if stream is not None else []
kinds = [e["type"] if isinstance(e, dict) else getattr(e, "type", None) for e in events]
check(kinds and kinds[-1] == "response.completed", "replacement stream ends in response.completed")
check(getattr(stream, "_hidden_params", None) == {"model_id": "m1"}
      and getattr(stream, "completed_response", None) is resp,
      "_hidden_params and completed_response carried for the proxy")
# #38: Codex never replays a reasoning item that lacks `summary`.
reasoning = {"type": "reasoning", "id": "rs_1", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "Read calc.py first.", "annotations": []}],
             "encrypted_content": None}
message = {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "DONE"}]}
r = ResponsesAPIResponse()
r.output = [reasoning, message]
item = tr._with_reasoning_summaries(r).output
check(item[0]["summary"] == [{"type": "summary_text", "text": "Read calc.py first."}]
      and item[0]["content"] == [] and item[1] is message and r.output[0] is reasoning,
      "reasoning text becomes summary_text; message and original untouched")
for out_item, label in ((dict(reasoning, summary=[{"type": "summary_text", "text": "kept"}]), "has a summary"),
                        (dict(reasoning, content=[]), "has no text")):
    r = ResponsesAPIResponse()
    r.output = [out_item]
    check(tr._with_reasoning_summaries(r) is r, f"passed through: item {label}")

# ── probe routes ────────────────────────────────────────────────────────
# Claude Code fires HEAD/GET /api/hello at ANTHROPIC_BASE_URL before credentials;
# LiteLLM 404'd it. The fix must not loosen auth or routing for anything else.
suite.section("PROBE ROUTES (running proxy)")
from ailocal.checks import services as S  # noqa: E402


def call(method, path, auth=False):
    headers = {"Authorization": f"Bearer {S.master_key()}"} if auth else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(
                S.PROXY + path, method=method, headers=headers), timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except OSError as e:
        return f"unreachable: {e}", b""


for method in ("HEAD", "GET"):
    status, body = call(method, "/api/hello")
    check(status == 200 and body == b"", f"{method} /api/hello unauthenticated -> 200, empty",
          f"{status} {body[:80]!r}")
# The unauthenticated /v1/models request makes LiteLLM log an expected auth traceback.
for path, auth, want in (("/v1/models?limit=1000", True, (200,)), ("/v1/models", False, (401, 403)),
                         ("/health/liveliness", False, (200,)), ("/api/definitely-not-a-route", False, (404,))):
    status, _ = call("GET", path, auth)
    check(status in want, f"GET {path} {'with' if auth else 'without'} auth -> {want}", str(status))

sys.exit(suite.report())
