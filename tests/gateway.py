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
suite.section("REPAIR: a reply that is only a call becomes the call")
repaired = {}
for label, text in (("json fence", '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/a"}}\n```'),
                    ("bare fence", '```\n{"name": "Read", "arguments": {"file_path": "/tmp/a"}}\n```'),
                    ("bare JSON", '{"name": "Read", "arguments": {"file_path": "/tmp/a"}}'),
                    ("qwen <function=>", '<function=Read>\n<parameter=file_path>/tmp/a</parameter>\n</function>')):
    calls, left = tr.recover(text, TOOLS)
    repaired[label] = ([(c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls or []], left or "")
check(all(v == ([("Read", {"file_path": "/tmp/a"})], "") for v in repaired.values()),
      "every supported call format is repaired into exactly one tool call, nothing left over", str(repaired))
prose = ('To read a file, the agent emits a call like this:\n\n'
         '```json\n{"name": "Read", "arguments": {"file_path": "/tmp/x"}}\n```\n\nThat is how it works.')
accepted = [label for text, tools, label in (
        (prose, TOOLS, "a fenced example inside prose"),
        ('```json\n{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
         '```json\n{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n```', TOOLS, "two fences"),
        ('```json\n{"name": "DropDatabase", "arguments": {"db": "prod"}}\n```', TOOLS, "an undeclared tool"),
        ('```json\n{"name": "Read", "arguments": {"wrong_field": "x"}}\n```', TOOLS, "a missing required arg"),
        ('```json\n{"name":"Read","arguments":{"file_path":"/a"}}\n```', [], "no declared tools"))
    if tr.recover(text, tools) != (None, text)]
check(not accepted, "examples and invalid calls are never executed; the text is returned unchanged", str(accepted))

# ── native-args ─────────────────────────────────────────────────────────
# gemma4 emits a well-formed Agent call without the required `description`;
# exactly that one field may be synthesised, and everything else fails closed.
suite.section("NATIVE ARGS: complete only a missing Agent description")


def complete(args, name="Agent", tools=AGENT_TOOLS):
    return tr.complete_ui_metadata(name, args if isinstance(args, str) else json.dumps(args), tools)


out = complete({"prompt": "Search for existing retry logic and backoff helpers", "subagent_type": "Explore"})
check(out and json.loads(out) == {"description": "Search for existing retry logic",
                                  "prompt": "Search for existing retry logic and backoff helpers",
                                  "subagent_type": "Explore"},
      "a missing Agent description is the first five prompt words; nothing else changes", out or "")
future = json.loads(json.dumps(AGENT))
future["input_schema"]["required"].append("run_in_background")
typed = json.loads(json.dumps(AGENT))
typed["input_schema"]["properties"]["description"] = {"type": "object"}
completed = [label for args, name, tools, label in (
        ({"description": "Explore repo", "prompt": "p"}, "Agent", AGENT_TOOLS, "already described"),
        ({"description": "", "prompt": "p"}, "Agent", AGENT_TOOLS, "empty description present"),
        ({"subagent_type": "Explore"}, "Agent", AGENT_TOOLS, "missing prompt"),
        ({"prompt": "p", "subagent_type": 7}, "Agent", AGENT_TOOLS, "wrongly typed subagent_type"),
        ("{not json", "Agent", AGENT_TOOLS, "malformed JSON"),
        ({"prompt": "p"}, "Bash", AGENT_TOOLS, "another tool"),
        ({"prompt": "p"}, "Agent", TOOLS, "undeclared Agent"),
        ({"prompt": "p"}, "Agent", TOOLS + [future], "two missing required fields"),
        ({"prompt": "p"}, "Agent", TOOLS + [typed], "description no longer a string"))
    if complete(args, name, tools) is not None]
check(not completed, "completion fails closed on anything but a lone missing Agent description", str(completed))

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



emitted = sse(block(0, "thinking", None, "Let me delegate.")
              + block(1, "tool_use", "Agent", '{"prompt": "Find th', 'e retry code"}')
              + block(2, "tool_use", "Bash", '{"command": "ls"}'))
got = ([(e["index"], e["content_block"]["type"]) for e in emitted if e["type"] == "content_block_start"],
       args_by_index(emitted))
check(got == ([(0, "thinking"), (1, "tool_use"), (2, "tool_use")],
              {1: {"prompt": "Find the retry code", "description": "Find the retry code"}, 2: {"command": "ls"}}),
      "fragmented Agent arguments are completed on the wire; block order and the sibling call are untouched",
      str(got))
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


async def drain(s):
    return [e async for e in s]


flagged = {"_websearch_interception_converted_stream": True}
resp = ResponsesAPIResponse()
hook = tr.ToolRepair().async_post_call_success_deployment_hook
untouched = asyncio.run(hook({}, resp, "aresponses"))
stream = asyncio.run(hook(flagged, resp, "aresponses"))
events = asyncio.run(drain(stream)) if stream is not None else []
kinds = [e["type"] if isinstance(e, dict) else getattr(e, "type", None) for e in events]
check(untouched is None and kinds and kinds[-1] == "response.completed"
      and getattr(stream, "_hidden_params", None) == {"model_id": "m1"}
      and getattr(stream, "completed_response", None) is resp,
      "only a de-streamed result is re-streamed, ending in response.completed with the proxy's metadata")
# #38: Codex never replays a reasoning item that lacks `summary`.
reasoning = {"type": "reasoning", "id": "rs_1", "status": "completed", "role": "assistant",
             "content": [{"type": "output_text", "text": "Read calc.py first."}], "encrypted_content": None}
message = {"type": "message", "id": "msg_1", "content": [{"type": "output_text", "text": "DONE"}]}
r = ResponsesAPIResponse()
r.output = [reasoning, message]
item = tr._with_reasoning_summaries(r).output
check(item[0]["summary"] == [{"type": "summary_text", "text": "Read calc.py first."}]
      and item[0]["content"] == [] and item[1] is message and r.output[0] is reasoning,
      "reasoning text becomes summary_text so Codex replays it (#38); the original is untouched")

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


# The unauthenticated /v1/models request makes LiteLLM log an expected auth traceback.
got = {"HEAD /api/hello": call("HEAD", "/api/hello"), "GET /api/hello": call("GET", "/api/hello"),
       "GET /v1/models (auth)": (call("GET", "/v1/models?limit=1000", True)[0], b""),
       "GET /v1/models (no auth)": (call("GET", "/v1/models")[0] in (401, 403), b""),
       "GET /api/unknown": (call("GET", "/api/definitely-not-a-route")[0], b"")}
check(got == {"HEAD /api/hello": (200, b""), "GET /api/hello": (200, b""), "GET /v1/models (auth)": (200, b""),
              "GET /v1/models (no auth)": (True, b""), "GET /api/unknown": (404, b"")},
      "/api/hello answers unauthenticated without opening any other route or auth", str(got))

sys.exit(suite.report())
