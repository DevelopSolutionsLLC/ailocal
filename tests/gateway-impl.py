#!/usr/bin/env python3
"""Proxy hooks as the image loads them: import, capability registry, tool negotiator,
reasoning-effort transport. Runs inside the LiteLLM container via tests/in-container.sh;
reads /app paths only and talks to a fake Ollama on 127.0.0.1.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

from harness import Suite, load_module

HOOKS = "/app/config/hooks"
suite = Suite("gateway (in the proxy image)")
check = suite.check

# ── hooks import ────────────────────────────────────────────────────────
# A registered-but-unimportable callback takes the container down at boot, and a
# sibling import that works on the host fails under LiteLLM's loader (by path,
# hooks dir not on sys.path). Run in a fresh isolated interpreter to match it.
suite.section("HOOKS IMPORT (LiteLLM loader semantics)")
_LOADER = """
import importlib.util, sys
for name in sys.argv[2:]:
    spec = importlib.util.spec_from_file_location(name, f"{sys.argv[1]}/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
"""
for hook in ("reasoning_router", "reasoning_loop_guard", "startup", "tool_repair",
             "tool_gateway", "capability_registry"):
    r = subprocess.run([sys.executable, "-I", "-c", _LOADER, HOOKS, hook],
                       capture_output=True, text=True, timeout=60, cwd="/")
    check(r.returncode == 0, f"{hook} imports", (r.stderr.strip().splitlines() or [""])[-1])

cr = load_module("capability_registry", f"{HOOKS}/capability_registry.py")
tg = load_module("tool_gateway", f"{HOOKS}/tool_gateway.py")
router = load_module("reasoning_router", f"{HOOKS}/reasoning_router.py")
repair = load_module("tool_repair", f"{HOOKS}/tool_repair.py")
reg = cr.Registry()
gw = tg.ToolGateway(registry=reg)
FRONTIER = "claude-3-5-sonnet-2024-10-22"
CLAUDE = {"proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0.0"}}}
CODEX = {"proxy_server_request": {"headers": {"originator": "codex_cli_rs"}}}

# ── registry ────────────────────────────────────────────────────────────
suite.section("REGISTRY LOADING")
d = reg.describe()
check(reg.loaded, f"registry.yaml loads ({reg.state})", reg.error or "")
check(d["groups"] >= 8 and d["model_classes"] >= 5 and d["routes"] == 3,
      f"groups, classes and three routes present ({d})")
check(d["contexts_from_generated"] >= 5, "context windows come from the generated file")
check(d["aliases"] > 0, "model_group_alias loaded")

suite.section("MODEL CLASSIFICATION")
for backend in ("qwen3.5:4b-unrestricted-mtp-q4_K_M",
                "gemma4:26b-unrestricted-qat-mtp-q4_K_M",
                "ailocal-qwen3.5-9b-coder-uncensored:mlx-7fed86c",
                "hf.co/localslm/Qwen3.5-4B-MTP-Uncensored:model_q4_k_m.gguf",
                "huihui_ai/qwen3.5-abliterated:9b-Claude-4.6-Opus-q4_K"):
    small = cr.Registry()
    small.backends["cyber"] = backend
    denied = small.removable_groups("claude-code", "ailocal-cyber")
    check(small.model_class("ailocal-cyber")[0] == "local_cyber"
          and not small.is_passthrough("ailocal-cyber")
          and "orchestration" in denied and "edit_and_run" not in denied,
          f"{backend}: local_cyber, negotiated, sheds orchestration only")
for alias, want in (("ailocal-architecture", "local_agentic"),
                    ("ailocal-implementation", "local_agentic"),
                    ("ailocal-completion", "local_completion"),
                    (FRONTIER, "frontier_passthrough")):
    check(reg.model_class(alias)[0] == want, f"{alias} -> {want}")
# The class keys off the backing model, not the role: one model, one class.
by_backend = {}
for cap in ("architecture", "implementation", "review", "fast", "completion"):
    by_backend.setdefault(reg.backend_of("ailocal-" + cap), set()).add(
        reg.model_class("ailocal-" + cap)[0])
check(all(len(c) == 1 for b, c in by_backend.items() if b),
      f"each backend gets one class across roles ({by_backend})")
check(reg.model_class("claude-sonnet-4-6")[0] in ("local_agentic", "local_nonagentic")
      and reg.is_passthrough("claude-sonnet-4-6") is False,
      "a compat alias resolves to a local class, not frontier")
check(reg.is_passthrough(FRONTIER) and reg.is_passthrough("nobody-registered"),
      "frontier and unknown models are passthrough (fail open)")

suite.section("CAPABILITY FLAGS AND CONTEXT")
check(reg.supports("ailocal-architecture", "tools") is True
      and reg.supports("ailocal-architecture", "reasoning") is True,
      "local_agentic declares tools and reasoning")
check(reg.supports("ailocal-completion", "tools") is False, "FIM tier declares no tools")
check(reg.supports("ailocal-implementation", "structured_output") is False,
      "no local tier claims enforced structured output (ollama/ollama#6002)")
check(reg.supports("ailocal-architecture", "nonexistent") is None, "unknown feature -> None")
ctx = {c: reg.max_context(f"ailocal-{c}") for c in
       ("architecture", "fast", "implementation", "review", "completion") if c in reg.contexts}
check(all(isinstance(v, int) and v > 0 for v in ctx.values()), f"positive windows ({ctx})")
check(all(w == ctx["architecture"] for c, w in ctx.items()
          if reg.backend_of("ailocal-" + c) == reg.backend_of("ailocal-architecture")),
      "roles sharing architecture's backend share its window")
check(ctx.get("completion", 0) == min(ctx.values()) if "completion" in ctx
      else reg.backend_of("ailocal-completion") is None, "completion is the smallest window")
check(reg.max_context(FRONTIER) == 200000, "cloud class falls back to declared max_context")

suite.section("ROUTES AND CLIENTS")
check(reg.route_for_call_type("anthropic_messages") == "/v1/messages"
      and reg.route_for_call_type("acompletion") == "/v1/chat/completions"
      and reg.route_for_call_type(None, has_input_key=True) == "/v1/responses",
      "call_type -> route")
# LiteLLM 1.98 expands namespace on /v1/responses; asserting it makes a flip visible.
check(reg.route_drops_type("/v1/responses", "namespace") is False, "/v1/responses keeps namespace")
check(all(reg.route_drops_type("/v1/responses", t) for t in
          ("computer_use", "image_generation", "shell")), "/v1/responses drops non-function types")
check(not reg.route_drops_type("/v1/responses", "function")
      and not reg.route_drops_type("/v1/messages", "namespace"), "drops are route-specific")
check(reg.result_status_mode("/v1/chat/completions") == "unknown"
      and reg.result_status_mode("/v1/messages") == "explicit", "result status modes")
for headers, want in (({"user-agent": "claude-cli/2.0.0"}, "claude-code"),
                      ({"User-Agent": "CLAUDE-CLI/2"}, "claude-code"),
                      ({"originator": "codex_cli_rs"}, "codex"),
                      ({"user-agent": "vscode/1.9"}, "vscode"),
                      ({"x-app": "cli"}, "claude-code"),
                      ({"user-agent": "curl/8"}, "unknown"), ({}, "unknown")):
    check(reg.detect_client(headers) == want, f"{headers} -> {want}")

suite.section("NEGOTIATION RULE AND GROUPS")
rm = reg.removable_groups("claude-code", "ailocal-architecture")
check({"orchestration", "scheduling", "worktree"} <= rm, "orchestration/scheduling/worktree removable")
check(not rm & {"edit_and_run", "search", "lsp", "interactive"},
      "coding surface kept; a group only one side denies stays (intersection)")
check(reg.removable_groups("unknown", "ailocal-architecture") == set()
      and reg.removable_groups("claude-code", "unknown-model") == set(),
      "unknown client or model -> nothing removable")
check(all(reg.is_protected(n) for n in ("web_search", "WebSearch", "<web_search>", "<namespace>"))
      and not reg.is_protected("Workflow"), "protected tools")
for tool, group in (("Workflow", "orchestration"), ("mcp__lsp__get_hover", "lsp"),
                    ("mcp__grepai__grepai_search", "search"), ("TaskCreate", "delegation"),
                    ("Agent", "delegation"), ("CronList", "scheduling"), ("LSP", "native_lsp"),
                    ("some_unknown_tool", None), ("mcp__github__issue_write", None)):
    check(reg.group_of(tool) == group, f"{tool} -> {group}")
# Agent sat in orchestration once and local models lost delegation entirely.
always = (reg.doc.get("task_classes") or {}).get("always") or []
check({"skills", "native_lsp"} <= set(always), "skills and native_lsp are in the always floor")
definite, ambiguous = reg.mutating_tools()
check({"Edit", "apply_patch"} <= definite and "Bash" in ambiguous
      and not definite & ambiguous, "mutating tools: Edit definite, Bash ambiguous, disjoint")

suite.section("TASK CLASSIFICATION")
name, groups, _ = reg.classify_task("show me an example of hello world in c++")
check(name == "conversational" and groups == set(), "a general question sheds every group")
floor = reg.task_always_groups()
for text, want in (("fix the typo in README.md", "simple_edit"),
                   ("where is the persona injector defined", "explore"),
                   ("why does the build keep failing", "debug"),
                   ("refactor the sync-models generator", "architecture")):
    name, groups, _ = reg.classify_task(text)
    check(name == want and floor <= groups, f"{want}: classified, keeps the floor")
_, g, _ = reg.classify_task("refactor the sync-models generator")
check("delegation" in g and "orchestration" not in g, "architecture keeps delegation only")
_, g, _ = reg.classify_task("where is the retry logic handled?")
check({"search", "lsp", "edit_and_run"} <= g, "explore keeps search, lsp, edit_and_run")
_, g, _ = reg.classify_task("fix the typo in parser.py")
check(not g & {"lsp", "search"}, "simple_edit sheds lsp and search")
check(reg.classify_task("")[0] is None and reg.classify_task("hello there")[1] is None,
      "unclassified returns None, not an empty set")

suite.section("REGISTRY FAILS OPEN")
missing = cr.Registry(path="/nonexistent", caps_json="/nonexistent", config_path="/nonexistent")
check(missing.state == "absent" and missing.is_passthrough("x")
      and missing.removable_groups("claude-code", "ailocal-architecture") == set()
      and missing.detect_client({"user-agent": "claude-cli"}) == "unknown",
      "missing registry: absent, passthrough, nothing removable, no client")
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
    fh.write("groups: [unclosed\n")
broken = cr.Registry(path=fh.name, caps_json="/nonexistent", config_path="/nonexistent")
os.unlink(fh.name)
check(broken.state == "error" and broken.is_passthrough("x"), "malformed registry: error, fails open")

suite.section("NO HARD-CODED LITERALS IN THE NEGOTIATOR")
# Only a complete quoted string counts: `tool.get("type") == "namespace"` fails,
# while identifiers like expand_namespaces or "{namespace}__{tool}" do not.
LITERAL = r"""(?<![A-Za-z0-9_{])['"]%s['"]"""
code = open(f"{HOOKS}/tool_gateway.py", encoding="utf-8").read()
code = re.sub(r"#.*", "", re.sub(r'"""(?:.|\n)*?"""', "", code))
hits = [t for t in ("qwen", "deepseek", "claude-cli", "codex_cli", "Workflow", "mcp__lsp",
                    "mcp__grepai", "namespace", "ailocal-architecture")
        if re.search(LITERAL % re.escape(t), code)]
check(not hits, f"tool_gateway.py has no model/client/tool literal (found {hits})")
check(re.search(LITERAL % "namespace", 'if t.get("type") == "namespace":')
      and not re.search(LITERAL % "namespace", 't = "{namespace}__{tool}"'),
      "the guard bites an inlined comparison, not a template")
check("registry" in code, "tool_gateway.py consults the registry")

# ── negotiator: known answers from hand-written canonical JSON ──────────
READ = ('{"description":"Read a file","input_schema":{"properties":{"path":{"type":"string"}},'
        '"required":["path"],"type":"object"},"name":"Read"}')
WORKFLOW = ('{"description":"' + "x" * 500 + '","input_schema":'
            '{"properties":{},"type":"object"},"name":"Workflow"}')
LSP = '{"description":"Hover","input_schema":{"properties":{},"type":"object"},"name":"mcp__lsp__get_hover"}'
READ_B, WORKFLOW_B, LSP_B = len(READ), len(WORKFLOW), len(LSP)
TOOLS = [json.loads(s) for s in (READ, WORKFLOW, LSP)]


def req(model="ailocal-architecture", tools=None, headers=CLAUDE, **kw):
    return dict(headers, model=model, tools=list(TOOLS if tools is None else tools), **kw)


suite.section("ENCODER AND NAMING")
check(tg.encode(json.loads(READ)) == READ and tg.tool_bytes(json.loads(READ)) == 136 == READ_B,
      "encode() reproduces the canonical literal; Read is 136 bytes")
check(tg.encode({"b": 1, "a": 2}) == '{"a":2,"b":1}', "keys are sorted")
check(len(tg.encode({"k": "é"}).encode()) == 10, "UTF-8 bytes (10), not characters (9)")
for tool, want in (({"name": "Read"}, "Read"), ({"type": "function", "function": {"name": "Read"}}, "Read"),
                   ({"type": "web_search"}, "<web_search>"), ({"junk": 1}, "<unknown>"),
                   ("not-a-dict", "<malformed>")):
    check(tg.tool_name(tool) == want, f"tool_name -> {want}")

suite.section("NEGOTIATION BYTE ACCOUNTING")
rep, _ = gw.negotiate(req(), "anthropic_messages")
check((rep["client"], rep["route"], rep["model_class"], rep["passthrough"])
      == ("claude-code", "/v1/messages", "local_agentic", False), "client, route, class resolved")
check(rep["bytes_in"] == READ_B + WORKFLOW_B + LSP_B == 822, "bytes_in == 822 (sum of literals)")
check(rep["dropped_names"] == ["Workflow"] and rep["dropped_groups"] == ["orchestration"]
      and rep["bytes_dropped"] == WORKFLOW_B == 585, "only Workflow (585 B) dropped, group named")
check(rep["bytes_kept"] == READ_B + LSP_B and rep["bytes_kept"] + rep["bytes_dropped"] == rep["bytes_in"],
      "kept + dropped == in")
check(isinstance(rep["max_context"], int) and rep["max_context"] > 0, "report carries the window")
check(isinstance(rep["tokens_est_in"], int) and rep["tokens_est_kept"] < rep["tokens_est_in"]
      and rep["tokenizer"] == "cl100k-proxy", "token estimate produced, lowered, tokenizer named")
fr, keep = gw.negotiate(req(FRONTIER), "anthropic_messages")
check(fr["passthrough"] and fr["tools_dropped"] == 0 and len(keep) == 3
      and "passthrough" in (fr.get("savings_claim") or ""), "frontier: untouched, reason stated")
fim, keep = gw.negotiate(req("ailocal-completion"), "anthropic_messages")
check(fim["tools_dropped"] == 3 and keep == [], "no-tools model is sent no schemas")
ws, _ = gw.negotiate(req(headers=CODEX, input="", tools=[
    {"type": "web_search"}, {"name": "Workflow", "description": "o", "input_schema": {"type": "object"}}]),
    "aresponses")
check("<web_search>" not in ws["dropped_names"] and "Workflow" in ws["dropped_names"],
      "unnamed web_search is protected; named Workflow is not")
pr, _ = gw.negotiate(req("ailocal-completion", tools=[
    {"name": "WebSearch", "description": "s", "input_schema": {"type": "object"}}]), "anthropic_messages")
check(pr["tools_dropped"] == 0, "a protected tool survives a no-tools model")

suite.section("NO CREDIT FOR LITELLM'S OWN DROPS")
sh = {"type": "shell", "name": "multi_agent_v1", "description": "spawn sub-agents"}
fn = {"type": "function", "name": "exec_command", "description": "run", "parameters": {"type": "object"}}
SH_B, FN_B = tg.tool_bytes(sh), tg.tool_bytes(fn)
rr, _ = gw.negotiate(req(headers=CODEX, input="", tools=[sh, fn]), "aresponses")
check(rr["route"] == "/v1/responses" and rr["bytes_in"] == SH_B + FN_B
      and rr["bytes_reachable"] == FN_B, "only the function tool is reachable")
check(rr["bytes_prefiltered_by_litellm"] == rr["bytes_dropped_moot"] == SH_B and rr["bytes_dropped"] == 0,
      "LiteLLM-discarded bytes are moot, not a saving")
check(rr["bytes_dropped"] + rr["bytes_dropped_moot"] + rr["bytes_kept"] == rr["bytes_in"],
      "saving + moot + kept == in")
# bytes_kept vs bytes_reachable once reported a -133.7% "reduction" on a Codex capture.
check(rr["bytes_kept_reachable"] <= min(rr["bytes_kept"], rr["bytes_reachable"]),
      "bytes_kept_reachable is bounded by kept and reachable")
bundle = {"type": "namespace", "name": "mcp__lsp", "description": "LSP bundle", "tools": [
    {"type": "function", "name": n, "description": n, "strict": False, "parameters": {"type": "object"}}
    for n in ("get_hover", "get_definition")]}
rb, _ = gw.negotiate(req(headers=CODEX, input="", tools=[bundle]), "aresponses")
check(rb["tools_dropped"] == 0 and rb["bytes_kept_reachable"] == rb["bytes_kept"] > 0
      and rb["tools_killed_by_translation"] == 0, "namespace bundle is kept and reaches the model (1.98)")
# A translation-killed tool once counted as kept, so Codex bundles read as delivered.
rk, _ = gw.negotiate(req(headers=CODEX, input="", tools=[
    {"type": "shell", "name": "local_shell", "description": "run"}]), "aresponses")
check(rk["bytes_kept"] > 0 and rk["bytes_kept_reachable"] == 0, "discarded tool contributes zero reachable bytes")
check((rk["tools_kept"], rk["tools_kept_by_gateway"], rk["tools_killed_by_translation"]) == (0, 1, 1),
      "tools_kept means forwarded; the gateway's decision is reported separately")
k = rk["killed_by_translation"]
check(k and k[0]["name"] == "local_shell" and k[0]["type"] == "shell" and "litellm" in k[0]["reason"],
      "killed entry names itself, type and reason")
check(rk["tokens_est_kept"] <= 1 < rk["tokens_est_in"], "tokens exclude tools never received")
rc, _ = gw.negotiate(req(tools=[{"type": "shell", "name": "local_shell", "description": "run"}],
                         messages=[{"role": "user", "content": "hi"}]), "acompletion")
check(rc["tools_kept"] == rc["tools_kept_by_gateway"] and rc["tools_killed_by_translation"] == 0,
      "a route that drops nothing: forwarded == kept")

suite.section("SCHEMA REWRITES")
rules = reg.rewrite_rules("claude-code")
check(rules["enabled"] and "$schema" in rules["strip_keys"] and rules["max_description_chars"] is None,
      "rewrites on, strip $schema, truncation off by default")
fat = {"name": "Thing", "description": "d" * 200, "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
    "additionalProperties": False, "properties": {"p": {
        "type": "string", "description": "x" * 100, "additionalProperties": True}}}}
out = tg.rewrite_tool(fat, rules)
check("$schema" not in tg.encode(out) and "additionalProperties" not in tg.encode(out)
      and out["description"] == "d" * 200 and tg.tool_bytes(out) < tg.tool_bytes(fat),
      "strip keys at every depth, description untouched, smaller")
# Regressions: report mode mutated the original; an identity check added a bogus function key.
check(fat["input_schema"].get("$schema") and "function" not in out, "original unmutated, no bogus key")
nested = tg.rewrite_tool({"type": "function", "function": {
    "name": "T", "description": "d", "parameters": {"$schema": "x", "type": "object"}}}, rules)
check("$schema" not in tg.encode(nested) and tg.tool_name(nested) == "T", "openai-nested schema stripped")
t = tg.rewrite_tool(fat, dict(rules, max_description_chars=20, max_param_description_chars=10))
check(t["description"].endswith("...") and len(t["description"]) <= 24
      and t["input_schema"]["properties"]["p"]["description"].endswith("..."), "truncation when enabled")
check(tg.rewrite_tool(fat, dict(rules, enabled=False)) is fat, "disabled returns the same object")
r2, _ = gw.negotiate(req(tools=[fat, json.loads(WORKFLOW)]), "anthropic_messages")
check(r2["bytes_saved_by_rewrite"] > 0 and r2["bytes_kept"] < r2["bytes_kept_before_rewrite"]
      and r2["bytes_kept_before_rewrite"] + r2["bytes_dropped"] == r2["bytes_in"],
      "rewrite saving reported apart from drop accounting")
f3, _ = gw.negotiate(req(FRONTIER, tools=[fat]), "anthropic_messages")
check(f3["rewrite_enabled"] is False and f3["bytes_kept"] == tg.tool_bytes(fat), "passthrough never rewritten")

suite.section("TASK NEGOTIATION")
os.environ.pop(tg.TASK_ENV, None)
check(tg.task_negotiation_enabled() is False, "off unless enabled")
for data, want in (({"messages": [{"role": "user", "content": "fix the typo"}]}, "fix the typo"),
                   ({"messages": [{"role": "user", "content": [{"type": "text", "text": "where"}]}]}, "where"),
                   ({"input": [{"type": "message", "role": "user", "content": "explain"}]}, "explain"),
                   ({"messages": [{"role": "user", "content":
                                   "<system-reminder>refactor</system-reminder>fix it"}]}, "fix it"),
                   ({"messages": [{"role": "assistant", "content": "I will refactor"},
                                  {"role": "user", "content": "where"}]}, "where"), ({}, "")):
    check(tg.first_user_text(data) == want, f"first_user_text -> {want!r}")
os.environ[tg.TASK_ENV] = "1"


def drops(text, model="ailocal-architecture"):
    r, _ = gw.negotiate(req(model, messages=[{"role": "user", "content": text}]), "anthropic_messages")
    return r


r = drops("fix the typo in a.py")
check(r["task_class"] == "simple_edit" and "mcp__lsp__get_hover" in r["dropped_names"]
      and "Read" not in r["dropped_names"], "simple edit sheds lsp, keeps Read")
check("mcp__lsp__get_hover" not in drops("where is the parser?")["dropped_names"], "explore keeps lsp")
r = drops("hello there")
check(r["task_class"] is None and r["dropped_names"] == ["Workflow"], "unmatched task subtracts nothing extra")
r = drops("fix the typo", FRONTIER)
check(r["task_class"] is None and r["tools_dropped"] == 0, "passthrough never task-negotiated")
os.environ.pop(tg.TASK_ENV, None)

suite.section("NAMESPACE EXPANSION")
check(tg.expand_namespaces([bundle], {"enabled": False}) == ([bundle], []), "disabled: untouched")
cfg = reg.namespace_expansion()
check(cfg["enabled"] is False and cfg["name_template"] == "{namespace}__{tool}",
      "ships disabled with the mcp__<server>__<tool> template")
on = dict(cfg, enabled=True)
out, info = tg.expand_namespaces([bundle], on, reg.group_of)
check([tg.tool_name(t) for t in out] == ["mcp__lsp__get_hover", "mcp__lsp__get_definition"]
      and all(t.get("type") == "function" for t in out) and info[0]["expanded"] == 2,
      "flattened to function tools, bundle removed, reported")
out, _ = tg.expand_namespaces([bundle], dict(on, only_groups=["search"]), reg.group_of)
check(any(t.get("type") == "namespace" for t in out), "only_groups leaves other bundles alone")
many = {"type": "namespace", "name": "mcp__big",
        "tools": [{"type": "function", "name": f"t{i}", "parameters": {"type": "object"}} for i in range(50)]}
out, info = tg.expand_namespaces([many], dict(on, max_tools_per_namespace=40), reg.group_of)
check(any(t.get("type") == "namespace" for t in out) and info[0].get("skipped"),
      "an oversized bundle is refused whole, not truncated")
empty = {"type": "namespace", "name": "mcp__empty", "tools": []}
check(tg.expand_namespaces([empty], on, reg.group_of)[0] == [empty], "an empty bundle is left as-is")

suite.section("NEGOTIATOR FAILS OPEN")
re_, _ = tg.ToolGateway(registry=missing).negotiate(req(), "anthropic_messages")
check(re_["tools_dropped"] == 0 and re_["passthrough"] and re_["bytes_in"] == 822,
      "no registry: nothing dropped, still measured")
ru, _ = gw.negotiate({"model": "ailocal-architecture", "tools": list(TOOLS)}, "anthropic_messages")
check(ru["client"] == "unknown" and ru["tools_dropped"] == 0, "unknown client is never filtered")

suite.section("MODES (off / report / filter)")


def hook(data):
    return asyncio.run(gw.async_pre_call_hook(None, None, data, "anthropic_messages"))


for mode, model, want in (("off", "ailocal-architecture", 3), ("report", "ailocal-architecture", 3),
                          ("filter", "ailocal-architecture", ["Read", "mcp__lsp__get_hover"]),
                          ("filter", FRONTIER, 3)):
    os.environ[tg.MODE_ENV] = mode
    d = req(model)
    hook(d)
    got = len(d["tools"]) if isinstance(want, int) else [tg.tool_name(t) for t in d["tools"]]
    check(got == want, f"{mode} + {model}: {want}")
for raw, want in (("FILTER", "filter"), ("typo", "off")):
    os.environ[tg.MODE_ENV] = raw
    check(tg.mode() == want, f"mode {raw!r} -> {want}")
os.environ.pop(tg.MODE_ENV)
check(tg.mode() == "off", "unset mode -> off")
os.environ[tg.MODE_ENV] = "filter"
d = req(tools=["not-a-dict", {"junk": 1}])
hook(d)
check(len(d["tools"]) == 2 and hook({"model": "x"}) is not None, "malformed entries kept; no tools is clean")
# Native LSP survived only by fail-open once; every class with a floor must keep it.
os.environ[tg.TASK_ENV] = "1"
NATIVE = {"description": "Language server", "input_schema": {"properties": {}, "type": "object"}, "name": "LSP"}
for prompt, want in (("fix the failing auth test", True), ("review this diff for security issues", True),
                     ("show me an example of hello world in C++", False)):
    d = req(tools=[NATIVE, json.loads(WORKFLOW)], messages=[{"role": "user", "content": prompt}])
    hook(d)
    check(("LSP" in [t.get("name") for t in d["tools"]]) is want, f"native LSP kept={want}: {prompt}")
os.environ.pop(tg.TASK_ENV, None)
os.environ.pop(tg.MODE_ENV, None)

# ── reasoning effort through the installed SDK ─────────────────────────
import litellm  # noqa: E402
from litellm.types.llms.openai import ResponsesAPIResponse  # noqa: E402
from openai.types.responses.response_reasoning_item import ResponseReasoningItem  # noqa: E402

suite.section("REASONING HISTORY (typed SDK objects)")
item = ResponseReasoningItem(id="rs_fixture", type="reasoning", summary=[],
                             content=[{"type": "reasoning_text", "text": "Inspect the task file."}],
                             encrypted_content="opaque-fixture")
resp = ResponsesAPIResponse(id="resp_fixture", created_at=0, model="fixture", object="response",
                            status="completed", output=[item])
with warnings.catch_warnings():
    warnings.simplefilter("error")
    fixed = repair._with_reasoning_summaries(resp)
    payload = fixed.output[0].model_dump()
    fixed.model_dump_json()
check(payload["summary"] == [{"type": "summary_text", "text": "Inspect the task file."}]
      and payload["content"] == [], "typed reasoning serializes a valid summary without warnings")
check(payload["id"] == "rs_fixture" and payload["encrypted_content"] == "opaque-fixture",
      "identity and encrypted replay data survive")
check(not item.summary and resp.output[0] is item and repair._with_reasoning_summaries(fixed) is fixed,
      "original unchanged; normalized passes through")

suite.section("REASONING EFFORT AT THE OLLAMA BOUNDARY (fake backend)")
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
    for effort, want in (("none", False), ("minimal", "low"), ("low", "low"),
                         ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh")):
        check(send({"reasoning_effort": effort}).get("think") == want, f"effort {effort} -> think={want!r}")
    check(send({"reasoning_effort": "xhigh", "think": False}).get("think") is False,
          "explicit think:false outranks effort")
    check(send({"reasoning": {"effort": "none"}}, True).get("think") is False, "Responses effort none -> false")
    check(send({"reasoning": {"effort": "xhigh"}}, True).get("think") == "xhigh", "Responses effort xhigh")
    opts = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5, "repeat_penalty": 1.0}
    got = send(dict(opts, allowed_openai_params=["presence_penalty"])).get("options")
    check(got == opts, f"sampling options incl. zero-valued min_p reach Ollama ({got})")
    stop = ["<|im_end|>", "<|im_start|>"]
    check(send({"stop": stop, "think": False}).get("options", {}).get("stop") == stop,
          "chat stop boundaries reach Ollama")
finally:
    server.shutdown()
    server.server_close()

# ── reasoning loop guard (#68) ──────────────────────────────────────────
suite.section("REASONING LOOP GUARD (retry once: a loop without replayed reasoning, an empty completion as sent)")
guard_mod = load_module("reasoning_loop_guard", f"{HOOKS}/reasoning_loop_guard.py")
gemma, qwen = cr.Registry(), cr.Registry()
gemma.backends["implementation"] = "gemma4:26b-qat-mtp"
qwen.backends["implementation"] = "qwen3.5:9b-mtp"
LIMIT = gemma.loop_guard_limit("ailocal-implementation")
check(isinstance(LIMIT, int) and LIMIT > 0, f"gemma4 backend is guarded ({LIMIT})")
check(qwen.loop_guard_limit("ailocal-implementation") is None, "qwen backend is not guarded (#38 replay kept)")
check(gemma.loop_guard_limit("nobody-registered") is None, "unknown model is not guarded")

REQ = {"model": "ailocal-implementation", "stream": True, "tools": [{"type": "function", "name": "exec"}],
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


normal = START + [think(500)] + ANSWER
out, sent = run_guard(gemma, REQ, normal)
check(out == normal and not sent, "a step that answers passes through unchanged, no retry")
looping = START + [think(LIMIT // 2), think(LIMIT // 2), think(10)] + ANSWER
out, sent = run_guard(gemma, REQ, looping)
check(out == RETRY_EVENTS and len(sent) == 1, "reasoning past the limit: one retry, only its events reach the client")
r = sent[0] if sent else {}
check(r.get("stream") is True and [i["type"] for i in r.get("input", [])]
      == ["message", "function_call", "function_call_output"]
      and r.get("tools") == REQ["tools"] and r.get("reasoning") == REQ["reasoning"]
      and "litellm_logging_obj" not in r and "litellm_call_id" not in r
      and len(REQ["input"]) == 4,
      "the retry drops only reasoning items and proxy bookkeeping; the original request is untouched")
out, sent = run_guard(gemma, REQ, START + [think(100), {"type": "response.incomplete"}])
check(out == RETRY_EVENTS and len(sent) == 1, "an incomplete reasoning-only end is retried")
out, sent = run_guard(gemma, REQ, START + [think(100)] + ANSWER[:1] + [think(LIMIT * 2), {"type": "response.incomplete"}])
check(not sent and out[-1] == {"type": "response.incomplete"},
      "after the first answer event the stream is never retried")
fresh = dict(REQ, input=[i for i in REQ["input"] if i["type"] != "reasoning"])
out, sent = run_guard(gemma, fresh, looping)
check(out == looping and not sent, "with no replayed reasoning the threshold does not apply")
out, sent = run_guard(gemma, fresh, START + [think(100), {"type": "response.incomplete"}])
check(not sent and out[-1] == {"type": "response.incomplete"},
      "with no replayed reasoning an incomplete end passes through")
out, sent = run_guard(qwen, REQ, looping)
check(out == looping and not sent, "an unguarded backend passes through")

# ── dropped tool call (#69): completed with reasoning only ──
EMPTY = START + [think(100), {"type": "response.completed"}]
out, sent = run_guard(gemma, fresh, EMPTY)
check(out == RETRY_EVENTS and len(sent) == 1,
      "a reasoning-only completion is retried once; only the retry reaches the client")
out, sent = run_guard(gemma, REQ, EMPTY)
check(len(sent) == 1 and [i["type"] for i in sent[0].get("input", [])] == [i["type"] for i in REQ["input"]]
      and "litellm_logging_obj" not in sent[0],
      "the empty-completion retry keeps replayed reasoning and drops proxy bookkeeping")
out, sent = run_guard(gemma, fresh, START + [think(100)] + ANSWER)
check(not sent and out[-1] == {"type": "response.completed"}, "a completion with an answer is not retried")
out, sent = run_guard(qwen, fresh, EMPTY)
check(out == EMPTY and not sent, "an unguarded backend's empty completion passes through")

sys.exit(suite.report())
