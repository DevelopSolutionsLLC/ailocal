#!/usr/bin/env python3
"""Proxy hooks as the image loads them: import, capability registry, tool negotiator,
reasoning-effort transport and the reasoning loop guard. Runs inside the LiteLLM
container via tests/in-container.sh; reads /app paths only and talks to a fake
Ollama on 127.0.0.1.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from harness import Suite, load_module

HOOKS = "/app/config/hooks"
suite = Suite("gateway (in the proxy image)")
check = suite.check

# ── hooks import ────────────────────────────────────────────────────────
# A registered-but-unimportable callback takes the container down at boot, and a
# sibling import that works on the host fails under LiteLLM's loader (by path,
# hooks dir not on sys.path). Each runs in a fresh isolated interpreter to match it.
suite.section("HOOKS IMPORT (LiteLLM loader semantics)")
_LOADER = """
import importlib.util, sys
name = sys.argv[2]
spec = importlib.util.spec_from_file_location(name, f"{sys.argv[1]}/{name}.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[name] = mod
spec.loader.exec_module(mod)
"""
failed = {}
for hook in ("reasoning_router", "reasoning_loop_guard", "startup", "tool_repair",
             "tool_gateway", "capability_registry"):
    r = subprocess.run([sys.executable, "-I", "-c", _LOADER, HOOKS, hook],
                       capture_output=True, text=True, timeout=60, cwd="/")
    if r.returncode:
        failed[hook] = (r.stderr.strip().splitlines() or [""])[-1]
check(not failed, "every registered hook imports by path, as LiteLLM loads it (boot crash)", str(failed))

cr = load_module("capability_registry", f"{HOOKS}/capability_registry.py")
tg = load_module("tool_gateway", f"{HOOKS}/tool_gateway.py")
router = load_module("reasoning_router", f"{HOOKS}/reasoning_router.py")
reg = cr.Registry()
gw = tg.ToolGateway(registry=reg)
FRONTIER = "claude-3-5-sonnet-2024-10-22"
CLAUDE = {"proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0.0"}}}
CODEX = {"proxy_server_request": {"headers": {"originator": "codex_cli_rs"}}}

# ── registry ────────────────────────────────────────────────────────────
suite.section("MODEL CLASSIFICATION")
classes = {}
for backend in ("nemotron-3.5-bolt:e4b-unrestricted",
                "gemma4:26b-unrestricted-qat-mtp",
                "ailocal-qwen3.5-9b-coder-uncensored:mlx-7fed86c",
                "hf.co/localslm/Qwen3.5-4B-MTP-Uncensored:model_q4_k_m.gguf",
                "huihui_ai/qwen3.5-abliterated:9b-Claude-4.6-Opus-q4_K"):
    small = cr.Registry()
    small.backends["cyber"] = backend
    denied = small.removable_groups("claude-code", "ailocal-cyber")
    classes[backend] = (small.model_class("ailocal-cyber")[0], small.is_passthrough("ailocal-cyber"),
                        "orchestration" in denied, "edit_and_run" in denied)
check(all(v == ("local_cyber", False, True, False) for v in classes.values()),
      "every shipped cyber backend is local_cyber and negotiated, shedding orchestration only", str(classes))

# ── negotiator: known answers from hand-written canonical JSON ──────────
READ = ('{"description":"Read a file","input_schema":{"properties":{"path":{"type":"string"}},'
        '"required":["path"],"type":"object"},"name":"Read"}')
WORKFLOW = ('{"description":"' + "x" * 500 + '","input_schema":'
            '{"properties":{},"type":"object"},"name":"Workflow"}')
LSP = '{"description":"Hover","input_schema":{"properties":{},"type":"object"},"name":"mcp__lsp__get_hover"}'
TOOLS = [json.loads(s) for s in (READ, WORKFLOW, LSP)]
BYTES_IN = len(READ) + len(WORKFLOW) + len(LSP)


def req(model="ailocal-primary", tools=None, headers=CLAUDE, **kw):
    return dict(headers, model=model, tools=list(TOOLS if tools is None else tools), **kw)


suite.section("NEGOTIATION")
rep, _ = gw.negotiate(req(), "anthropic_messages")
got = (rep["model_class"], rep["dropped_names"], rep["dropped_groups"], rep["bytes_dropped"],
       rep["bytes_kept"] + rep["bytes_dropped"] == rep["bytes_in"] == BYTES_IN)
check(got == ("local_agentic", ["Workflow"], ["orchestration"], len(WORKFLOW), True),
      "Claude Code on a local model sheds only orchestration, and kept + dropped == in", str(got))
# bytes_kept vs bytes_reachable once reported a -133.7% "reduction" on a Codex capture.
sh = {"type": "shell", "name": "multi_agent_v1", "description": "spawn sub-agents"}
fn = {"type": "function", "name": "exec_command", "description": "run", "parameters": {"type": "object"}}
rr, _ = gw.negotiate(req(headers=CODEX, input="", tools=[sh, fn]), "aresponses")
got = (rr["bytes_dropped"], rr["bytes_dropped_moot"], rr["bytes_reachable"],
       rr["bytes_kept_reachable"] <= min(rr["bytes_kept"], rr["bytes_reachable"]))
check(got == (0, tg.tool_bytes(sh), tg.tool_bytes(fn), True),
      "a tool LiteLLM discards on /v1/responses is moot, never claimed as a saving (-133.7% report)", str(got))
missing = cr.Registry(path="/nonexistent", caps_json="/nonexistent", config_path="/nonexistent")
outcomes = {"frontier": gw.negotiate(req(FRONTIER), "anthropic_messages")[0],
            "unregistered model": gw.negotiate(req("nobody-registered"), "anthropic_messages")[0],
            "no registry": tg.ToolGateway(registry=missing).negotiate(req(), "anthropic_messages")[0]}
got = {k: (r["passthrough"], r["tools_dropped"], r["bytes_in"]) for k, r in outcomes.items()}
check(all(v == (True, 0, BYTES_IN) for v in got.values()),
      "frontier, unregistered model and missing registry fail open: nothing dropped, still measured", str(got))
# Native LSP survived only by fail-open once; every task class with a floor must keep it.
os.environ[tg.MODE_ENV], os.environ[tg.TASK_ENV] = "filter", "1"
NATIVE = {"description": "Language server", "input_schema": {"properties": {}, "type": "object"}, "name": "LSP"}
kept = {}
for prompt in ("fix the failing auth test", "review this diff for security issues",
               "show me an example of hello world in C++"):
    d = req(tools=[NATIVE, json.loads(WORKFLOW)], messages=[{"role": "user", "content": prompt}])
    asyncio.run(gw.async_pre_call_hook(None, None, d, "anthropic_messages"))
    kept[prompt] = "LSP" in [t.get("name") for t in d["tools"]]
os.environ.pop(tg.TASK_ENV, None)
os.environ.pop(tg.MODE_ENV, None)
check(list(kept.values()) == [True, True, False],
      "filter mode keeps native LSP for every task class with a floor; only conversation sheds it", str(kept))

# ── reasoning effort and tool identity at the Ollama boundary ──────────
import litellm  # noqa: E402

suite.section("OLLAMA BOUNDARY (fake backend)")
captured = []


class Ollama(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        result = {}
        if self.path == "/api/chat":
            captured.append(body)
            result = {"model": body["model"], "message": {"role": "assistant", "content": "ok"},
                      "done": True, "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}
        wire = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def log_message(self, *args):
        pass


def send(data, responses=False):
    asyncio.run(router.ReasoningRouter().async_pre_call_hook(None, None, data, None))
    before = len(captured)
    kw = dict(model="ollama_chat/fixture-model", api_base=f"http://127.0.0.1:{server.server_port}", **data)
    if responses:
        litellm.responses(input="fixture", **kw)
    else:
        litellm.completion(messages=[{"role": "user", "content": "fixture"}], **kw)
    return captured[-1] if len(captured) == before + 1 else {"<not sent>": True}


server = HTTPServer(("127.0.0.1", 0), Ollama)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    want = {"none": False, "minimal": "low", "low": "low", "medium": "medium", "high": "high",
            "xhigh": "xhigh", "xhigh + think:false": False, "responses none": False, "responses xhigh": "xhigh"}
    got = {e: send({"reasoning_effort": e}).get("think") for e in ("none", "minimal", "low", "medium", "high", "xhigh")}
    got["xhigh + think:false"] = send({"reasoning_effort": "xhigh", "think": False}).get("think")
    got["responses none"] = send({"reasoning": {"effort": "none"}}, True).get("think")
    got["responses xhigh"] = send({"reasoning": {"effort": "xhigh"}}, True).get("think")
    check(got == want, "reasoning effort reaches Ollama as think on both routes; explicit think:false wins",
          str(got))

    # #84: Ollama's Gemma4Renderer names a tool result from tool_name, else by the
    # call id; LiteLLM 1.106.5 sends neither, so the model read "response:unknown".
    load_module("startup", f"{HOOKS}/startup.py")  # installs the tool-identity patch in-process
    loop = [{"role": "user", "content": "read both"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "Read", "arguments": '{"path": "a"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "Grep", "arguments": '{"q": "b"}'}}]},
            {"role": "tool", "tool_call_id": "call_b", "content": "B"},
            {"role": "tool", "tool_call_id": "call_a", "content": "A"},
            {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan"}]
    before = len(captured)
    litellm.completion(model="ollama_chat/fixture-model", api_base=f"http://127.0.0.1:{server.server_port}",
                       messages=loop)
    sent = captured[-1]["messages"] if len(captured) == before + 1 else []
    names = [m.get("tool_name") for m in sent[2:]]
    check(names == ["Grep", "Read", None],
          "tool results reach Ollama named by call id, out of order; an unmatched id stays unnamed (#84)",
          str(names))
finally:
    server.shutdown()
    server.server_close()

# ── reasoning loop guard (#68, #69) ─────────────────────────────────────
suite.section("REASONING LOOP GUARD")
guard_mod = load_module("reasoning_loop_guard", f"{HOOKS}/reasoning_loop_guard.py")
gemma = cr.Registry()
gemma.backends["primary"] = "gemma4:26b-qat-mtp"
LIMIT = gemma.loop_guard_limit("ailocal-primary") or 0

REQ = {"model": "ailocal-primary", "stream": True, "tools": [{"type": "function", "name": "exec"}],
       "reasoning": {"effort": "medium"}, "litellm_logging_obj": object(), "litellm_call_id": "x",
       "input": [{"type": "message", "role": "user", "content": "q"},
                 {"type": "reasoning", "summary": [{"type": "summary_text", "text": "t"}]},
                 {"type": "function_call", "name": "exec", "arguments": "{}", "call_id": "c"},
                 {"type": "function_call_output", "call_id": "c", "output": "o"}]}


def think(n):
    return {"type": "response.reasoning_summary_text.delta", "delta": "x" * n}


START = [{"type": "response.created"},
         {"type": "response.output_item.added", "item": {"type": "reasoning"}}]
ANSWER = [{"type": "response.output_item.added", "item": {"type": "message"}},
          {"type": "response.output_text.delta", "delta": "done"}, {"type": "response.completed"}]
RETRY_EVENTS = [{"type": "response.created", "retry": True}, {"type": "response.completed", "retry": True}]


async def _aiter(items):
    for i in items:
        yield i


def run_guard(registry, request, events):
    sent = []

    async def retry(data):
        sent.append(data)
        return _aiter(RETRY_EVENTS)

    guard = guard_mod.ReasoningLoopGuard(registry=registry, retry=retry)

    async def collect():
        return [e async for e in guard.async_post_call_streaming_iterator_hook(None, _aiter(events), request)]
    return asyncio.run(collect()), sent


looping = START + [think(LIMIT // 2), think(LIMIT // 2), think(10)] + ANSWER
out, sent = run_guard(gemma, REQ, looping)
r = sent[0] if sent else {}
check(LIMIT > 0 and out == RETRY_EVENTS and len(sent) == 1
      and [i["type"] for i in r.get("input", [])] == ["message", "function_call", "function_call_output"]
      and "litellm_logging_obj" not in r and len(REQ["input"]) == 4,
      "a gemma4 reasoning loop past the limit is retried once without replayed reasoning (#68)")
fresh = dict(REQ, input=[i for i in REQ["input"] if i["type"] != "reasoning"])
out, sent = run_guard(gemma, fresh, START + [think(100), {"type": "response.completed"}])
check(out == RETRY_EVENTS and len(sent) == 1,
      "a reasoning-only completion (dropped tool call) is retried once; only the retry reaches the client (#69)")

sys.exit(suite.report())
