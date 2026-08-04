#!/usr/bin/env python3
"""test-tool-gateway.py — known-answer tests for the capability negotiator
(config/litellm/tool_gateway.py) driven by the real registry.yaml.

The gateway's job is to report a number and then act on it, so these tests do
not check it against itself. Every byte assertion is pinned to a LITERAL
canonical JSON string written out by hand below: the test asserts both that the
encoder reproduces that exact string and that the reported total equals the
literal's length. If the encoding ever changes, these fail loudly instead of
drifting.

Token estimates are deliberately not pinned — litellm selects cl100k for a
non-OpenAI backend, so the value is not the gateway's to guarantee. The tests
assert only that a token figure is an int or None, never a fabricated zero.

Needs PyYAML and the registry, so it runs inside the proxy image via
scripts/tests/in-container.sh.
"""

import asyncio
import importlib.util
import json
import os
import sys
import types

try:
    from litellm.integrations.custom_logger import CustomLogger  # noqa: F401
    REAL_LITELLM = True
except ImportError:
    REAL_LITELLM = False
    _clog = types.ModuleType("litellm.integrations.custom_logger")
    class _CustomLogger:
        def __init__(self, *a, **k): pass
    _clog.CustomLogger = _CustomLogger
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
    sys.modules["litellm.integrations.custom_logger"] = _clog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATEWAY = os.environ.get("AILOCAL_GATEWAY_MODULE",
                         os.path.join(ROOT, "config/litellm/tool_gateway.py"))
REG_PY = os.environ.get("AILOCAL_REGISTRY_MODULE",
                        os.path.join(ROOT, "config/litellm/capability_registry.py"))
REG_YAML = os.environ.get("AILOCAL_REGISTRY",
                          os.path.join(ROOT, "config/litellm/registry.yaml"))
CAPS = os.environ.get("AILOCAL_CAPABILITIES_JSON",
                      os.path.join(ROOT, "config/capabilities.generated.json"))
CONF = os.environ.get("AILOCAL_CONFIG_PATH",
                      os.path.join(ROOT, "config/litellm/config.yaml"))

try:
    import yaml  # noqa: F401
except ImportError:
    print("SKIPPED: PyYAML absent. Run via scripts/tests/in-container.sh so the "
          "registry-backed checks actually execute; exiting non-zero rather "
          "than reporting green over a reduced set.")
    sys.exit(1)

# capability_registry must be importable as a top-level module, the way the
# gateway imports it inside the container.
sys.path.insert(0, os.path.dirname(REG_PY))
_rspec = importlib.util.spec_from_file_location("capability_registry", REG_PY)
cr = importlib.util.module_from_spec(_rspec)
sys.modules["capability_registry"] = cr
_rspec.loader.exec_module(cr)

_spec = importlib.util.spec_from_file_location("tool_gateway", GATEWAY)
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Suite

_suite = Suite()
check = _suite.check

reg = cr.Registry(path=REG_YAML, caps_json=CAPS, config_path=CONF)
gw = tg.ToolGateway(registry=reg)

CLAUDE_HEADERS = {"proxy_server_request":
                  {"headers": {"user-agent": "claude-cli/2.0.0"}}}
CODEX_HEADERS = {"proxy_server_request":
                 {"headers": {"originator": "codex_cli_rs"}}}

# ═══════════════════════════════════════════════════════════════════════════
# GROUND TRUTH — hand-written canonical JSON. These literals, not the gateway,
# define the expected bytes: sorted keys, no whitespace.
# ═══════════════════════════════════════════════════════════════════════════

READ = ('{"description":"Read a file","input_schema":'
        '{"properties":{"path":{"type":"string"}},"required":["path"],'
        '"type":"object"},"name":"Read"}')
WORKFLOW = ('{"description":"' + ("x" * 500) + '","input_schema":'
            '{"properties":{},"type":"object"},"name":"Workflow"}')
LSP = ('{"description":"Hover","input_schema":{"properties":{},'
       '"type":"object"},"name":"mcp__lsp__get_hover"}')

READ_B = len(READ.encode())
WORKFLOW_B = len(WORKFLOW.encode())
LSP_B = len(LSP.encode())

print(f"\nGround truth: Read={READ_B}B  Workflow={WORKFLOW_B}B  lsp_hover={LSP_B}B")

TOOLS = [json.loads(s) for s in (READ, WORKFLOW, LSP)]

print("\nENCODER (the basis of every byte figure)")
check(tg.encode(json.loads(READ)) == READ,
      "encode() reproduces the hand-written canonical JSON exactly")
check(tg.tool_bytes(json.loads(READ)) == READ_B,
      f"tool_bytes(Read) == {READ_B} (literal length, computed independently)")
check(tg.encode({"b": 1, "a": 2}) == '{"a":2,"b":1}',
      "encode() sorts keys, so byte counts are order-independent")
check(len(tg.encode({"k": "é"}).encode()) == 10,
      "byte accounting counts UTF-8 bytes (10), not characters (9)")

print("\nTOOL NAMING (dialect-agnostic)")
check(tg.tool_name({"name": "Read"}) == "Read", "anthropic shape")
check(tg.tool_name({"type": "function",
                    "function": {"name": "Read"}}) == "Read",
      "openai nested function.name is unwrapped")
check(tg.tool_name({"type": "web_search"}) == "<web_search>",
      "an entry with no name gets a bracketed pseudo-name, still counted")
check(tg.tool_name({"junk": 1}) == "<unknown>", "no name and no type")
check(tg.tool_name("not-a-dict") == "<malformed>", "a non-dict does not crash")

print("\nNEGOTIATION: claude-code + a local agentic model")
data = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS))
rep, keep = gw.negotiate(data, "anthropic_messages")
check(rep["client"] == "claude-code", "client identified from headers")
check(rep["route"] == "/v1/messages", "route from call_type")
check(rep["model_class"] == "local_agentic", "model class resolved via registry")
check(rep["passthrough"] is False, "a local model is not passthrough")
check(rep["bytes_in"] == READ_B + WORKFLOW_B + LSP_B,
      f"bytes_in == {READ_B + WORKFLOW_B + LSP_B} (sum of the three literals)")
check(rep["dropped_names"] == ["Workflow"],
      "only the orchestration tool is negotiated away")
check(rep["dropped_groups"] == ["orchestration"],
      "the report names the GROUP dropped, not just the tool")
check(rep["bytes_dropped"] == WORKFLOW_B,
      f"bytes_dropped == {WORKFLOW_B}, the dropped literal's exact size")
check(rep["bytes_kept"] == READ_B + LSP_B, "the coding + lsp tools are kept")
check(rep["bytes_kept"] + rep["bytes_dropped"] == rep["bytes_in"],
      "the accounting closes")
# Consistency, not a literal: the report must carry the SAME window the registry
# resolved. Hardcoding 65536 here tested the number, not the wiring, and broke on
# a profile change.
check(isinstance(rep["max_context"], int) and rep["max_context"] > 0,
      f"the report carries a positive generated context window ({rep['max_context']})")

print("\nPASSTHROUGH: a frontier model is never filtered")
frontier = dict(CLAUDE_HEADERS, model="claude-3-5-sonnet-2024-10-22",
                tools=list(TOOLS))
fr, fkeep = gw.negotiate(frontier, "anthropic_messages")
check(fr["passthrough"] is True, "a dated frontier id is passthrough")
check(fr["tools_dropped"] == 0, "nothing is dropped for a frontier model")
check(len(fkeep) == 3, "every tool survives")
check("passthrough" in (fr.get("savings_claim") or ""),
      "the report states passthrough as the reason for zero savings")

print("\nA MODEL THAT CANNOT USE TOOLS")
fim = dict(CLAUDE_HEADERS, model="ailocal-completion", tools=list(TOOLS))
fr2, keep2 = gw.negotiate(fim, "anthropic_messages")
check(reg.supports("ailocal-completion", "tools") is False,
      "the registry declares no tool support for the FIM tier")
check(fr2["tools_dropped"] == 3,
      "a model with no tool support is sent no tool schemas")
check(keep2 == [], "nothing kept")

print("\nPROTECTED TOOLS SURVIVE EVERYTHING")
ws = dict(CODEX_HEADERS, model="ailocal-architecture", input="",
          tools=[{"type": "web_search"},
                 {"name": "Workflow", "description": "orchestrate",
                  "input_schema": {"type": "object"}}])
rw, kw = gw.negotiate(ws, "aresponses")
check("<web_search>" not in rw["dropped_names"],
      "an unnamed web_search entry is never dropped (SearXNG depends on it)")
check("Workflow" in rw["dropped_names"], "...but a named orchestration tool is")
fim2 = dict(CLAUDE_HEADERS, model="ailocal-completion",
            tools=[{"name": "WebSearch", "description": "s",
                    "input_schema": {"type": "object"}}])
r3, k3 = gw.negotiate(fim2, "anthropic_messages")
check(r3["tools_dropped"] == 0,
      "a protected tool survives even a model that supports no tools")

print("\nNO CREDIT FOR LITELLM'S OWN DROPS")
ns = {"type": "namespace", "name": "multi_agent_v1",
      "description": "spawn sub-agents", "tools": [{"name": "spawn"}]}
fn = {"type": "function", "name": "exec_command", "description": "run",
      "parameters": {"type": "object"}}
NS_B, FN_B = tg.tool_bytes(ns), tg.tool_bytes(fn)
rr, _ = gw.negotiate(dict(CODEX_HEADERS, model="ailocal-architecture",
                          input="", tools=[ns, fn]), "aresponses")
check(rr["route"] == "/v1/responses", "responses route detected")
check(rr["bytes_in"] == NS_B + FN_B, "bytes_in counts everything the client sent")
check(rr["bytes_reachable"] == FN_B,
      f"bytes_reachable == {FN_B}: only the function tool reaches the model")
check(rr["bytes_prefiltered_by_litellm"] == NS_B,
      "the namespace bytes are attributed to LiteLLM, not to this gateway")
check(rr["bytes_dropped_moot"] == NS_B,
      "dropping a namespace tool is booked as MOOT, not as a saving")
check(rr["bytes_dropped"] == 0,
      "no reachable bytes were saved here, so no saving is claimed")
check(rr["bytes_dropped"] + rr["bytes_dropped_moot"] + rr["bytes_kept"]
      == rr["bytes_in"], "saving + moot + kept == in")
# The field that makes before/after ratios honest. bytes_kept counts kept tools
# INCLUDING ones LiteLLM discards on this route, so comparing it against
# bytes_reachable produced a -133.7% "reduction" on a real Codex capture.
check(rr["bytes_kept_reachable"] <= rr["bytes_kept"],
      "bytes_kept_reachable excludes kept-but-unreachable entries")
check(rr["bytes_kept_reachable"] <= rr["bytes_reachable"],
      "the model never receives more than the route forwards — any ratio "
      "against bytes_reachable must use this field")
ns_kept = {"type": "namespace", "name": "mcp__lsp", "description": "bundle",
           "tools": [{"name": "hover"}]}
rk, _ = gw.negotiate(dict(CODEX_HEADERS, model="ailocal-architecture",
                          input="", tools=[ns_kept]), "aresponses")
check(rk["tools_dropped"] == 0, "an ungrouped namespace bundle is kept...")
check(rk["bytes_kept"] > 0 and rk["bytes_kept_reachable"] == 0,
      "...but contributes ZERO to what the model receives")
# The COUNT must tell the same story the BYTES already told. Reporting a
# translation-killed namespace as "kept" is what made Codex's mcp__grepai /
# mcp__lsp bundles read as delivered on every request while the model never
# saw them (measured 2026-07-29: tools_kept 14 with both bundles listed in
# `largest`). tools_kept now means FORWARDED.
check(rk["tools_kept"] == 0,
      "a namespace bundle LiteLLM will discard is NOT counted as kept")
check(rk["tools_kept_by_gateway"] == 1,
      "...while the pre-translation figure still records the gateway's own "
      "decision under its own name")
check(rk["tools_killed_by_translation"] == 1,
      "the entry is booked against the translation stage that removes it")
killed = rk["killed_by_translation"]
check(killed and killed[0]["name"] == "mcp__lsp"
      and killed[0]["type"] == "namespace" and "litellm" in killed[0]["reason"],
      "each killed entry names itself, its type, and the reason it vanished")
# <= 1 because encode([]) is the two-byte "[]", which tokenises to 1 — the
# floor for an empty forwarded set, not zero.
check(rk["tokens_est_kept"] <= 1 < rk["tokens_est_in"],
      "tokens the model pays for exclude tools it never receives")
# Claude Code's route keeps namespaces, so the two stages must agree there.
rc, _ = gw.negotiate(dict(CLAUDE_HEADERS, model="ailocal-architecture",
                          messages=[{"role": "user", "content": "hi"}],
                          tools=[ns_kept]), "acompletion")
check(rc["tools_kept"] == rc["tools_kept_by_gateway"]
      and rc["tools_killed_by_translation"] == 0,
      "on a route that drops nothing, forwarded == kept-by-gateway")

print("\nSCHEMA REWRITES (Phase C): shrink without removing")
rules = reg.rewrite_rules("claude-code")
check(rules["enabled"] is True, "rewrites enabled by default for claude-code")
check("$schema" in rules["strip_keys"], "$schema is stripped")
check(rules["max_description_chars"] is None,
      "description truncation is DISABLED by default — it is a bet on model "
      "behaviour, not a free byte win")

fat = {"name": "Thing", "description": "d" * 200,
       "input_schema": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object", "additionalProperties": False,
                        "properties": {"p": {"type": "string",
                                             "description": "x" * 100,
                                             "additionalProperties": True}}}}
before = tg.tool_bytes(fat)
out = tg.rewrite_tool(fat, rules)
check(tg.encode(out).find("$schema") == -1, "$schema removed from the schema")
check(tg.encode(out).find("additionalProperties") == -1,
      "additionalProperties removed recursively, at every depth")
check(out["description"] == "d" * 200,
      "the tool description is untouched while truncation is disabled")
check(tg.tool_bytes(out) < before, "the rewritten tool is smaller")
check(fat["input_schema"].get("$schema") is not None,
      "the ORIGINAL tool is not mutated — REPORT mode must measure without "
      "performing the rewrite")
check("function" not in out,
      "an Anthropic-shaped tool does not acquire a bogus function key "
      "(regression: an identity check against the copy instead of the original)")

nested = {"type": "function",
          "function": {"name": "T", "description": "d" * 200,
                       "parameters": {"$schema": "x", "type": "object"}}}
out_n = tg.rewrite_tool(nested, rules)
check("$schema" not in tg.encode(out_n), "openai-nested schema is stripped too")
check(out_n["function"]["name"] == "T", "the nested function survives intact")
check(tg.tool_name(out_n) == "T", "and is still nameable afterwards")

trunc = dict(rules, max_description_chars=20, max_param_description_chars=10)
out_t = tg.rewrite_tool(fat, trunc)
check(out_t["description"].endswith("..."), "truncation adds a marker")
check(len(out_t["description"]) <= 24, "the tool description is truncated")
check(out_t["input_schema"]["properties"]["p"]["description"].endswith("..."),
      "nested parameter descriptions are truncated independently")

off = dict(rules, enabled=False)
check(tg.rewrite_tool(fat, off) is fat,
      "disabled rewrites return the tool unchanged, by identity")

# Reported as its own figure, never merged into the drop saving.
rw = dict(CLAUDE_HEADERS, model="ailocal-architecture",
          tools=[fat, json.loads(WORKFLOW)])
rr2, kk2 = gw.negotiate(rw, "anthropic_messages")
check(rr2["rewrite_enabled"] is True, "the report says rewrites were considered")
check(rr2["bytes_saved_by_rewrite"] > 0, "rewrite saving is reported")
check(rr2["bytes_kept"] < rr2["bytes_kept_before_rewrite"],
      "bytes_kept reflects the post-rewrite payload")
check(rr2["bytes_kept_before_rewrite"] + rr2["bytes_dropped"]
      == rr2["bytes_in"],
      "the DROP accounting still closes against the pre-rewrite figure, so the "
      "two kinds of reduction are never conflated")

fr3, _ = gw.negotiate(dict(CLAUDE_HEADERS, model="claude-3-5-sonnet-2024-10-22",
                           tools=[fat]), "anthropic_messages")
check(fr3["rewrite_enabled"] is False,
      "a passthrough model is never rewritten either")
check(fr3["bytes_kept"] == tg.tool_bytes(fat),
      "and its payload is byte-identical to what the client sent")

print("\nTASK NEGOTIATION (Phase D) — off by default, subtractive only")
os.environ.pop(tg.TASK_ENV, None)
check(tg.task_negotiation_enabled() is False, "disabled unless explicitly on")

check(tg.first_user_text({"messages": [{"role": "user", "content": "fix the typo"}]})
      == "fix the typo", "first user text, anthropic/openai string content")
check(tg.first_user_text({"messages": [
    {"role": "user", "content": [{"type": "text", "text": "where is X"}]}]})
      == "where is X", "block content is flattened")
check(tg.first_user_text({"input": [
    {"type": "message", "role": "user", "content": "explain the parser"}]})
      == "explain the parser", "responses-route input[] is read")
check(tg.first_user_text({"messages": [
    {"role": "user", "content": "<system-reminder>refactor everything"
                                "</system-reminder>fix the typo"}]})
      == "fix the typo",
      "injected scaffolding is stripped — otherwise every session is classified "
      "by whatever words appear in AGENTS.md")
check(tg.first_user_text({"messages": [
    {"role": "assistant", "content": "I will refactor"},
    {"role": "user", "content": "where is X"}]}) == "where is X",
      "assistant turns never contribute — a model must not narrow its own tools")
check(tg.first_user_text({}) == "", "no messages -> empty, not a crash")

cls, groups, hits = reg.classify_task("fix the typo in parser.py")
check(cls == "simple_edit", f"a located edit classifies as simple_edit ({cls})")
check("edit_and_run" in groups, "the always-groups floor is included")
check("lsp" not in groups and "search" not in groups,
      "a simple edit does not need symbols or semantic search")

cls2, g2, _ = reg.classify_task("where is the retry logic handled?")
check(cls2 == "explore", f"a question classifies as explore ({cls2})")
check("search" in g2 and "lsp" in g2, "explore needs search + lsp")
check("edit_and_run" in g2, "but can still read and run")

cls3, g3, _ = reg.classify_task("design the sync service architecture")
check(cls3 == "architecture", f"design work -> architecture ({cls3})")
check("planning" in g3, "architecture gets planning")
check("orchestration" not in g3,
      "architecture still does NOT get orchestration — subagent spawning is "
      "what local models drive worst")

cls4, g4, _ = reg.classify_task("hello there")
check(cls4 is None and g4 is None,
      "an unclassifiable task returns None, NOT an empty set — 'no opinion' "
      "and 'needs nothing' must not be confused")

os.environ[tg.TASK_ENV] = "1"
check(tg.task_negotiation_enabled() is True, "enabled by env")

# A simple edit should now also shed lsp/search, which Phase B alone kept.
edit_req = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS),
                messages=[{"role": "user", "content": "fix the typo in a.py"}])
re1, k1 = gw.negotiate(edit_req, "anthropic_messages")
check(re1["task_class"] == "simple_edit", "the report names the task class")
check("mcp__lsp__get_hover" in re1["dropped_names"],
      "task negotiation sheds lsp for a simple edit, beyond Phase B's drops")
check("Read" not in re1["dropped_names"], "reading is never shed")

explore_req = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS),
                   messages=[{"role": "user", "content": "where is the parser?"}])
re2, _ = gw.negotiate(explore_req, "anthropic_messages")
check(re2["task_class"] == "explore", "explore classified")
check("mcp__lsp__get_hover" not in re2["dropped_names"],
      "lsp is KEPT for an exploration task")

# The safety property: classification may only subtract.
unmatched = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS),
                 messages=[{"role": "user", "content": "hello there"}])
re3, _ = gw.negotiate(unmatched, "anthropic_messages")
check(re3["task_class"] is None, "unmatched task")
check(re3["dropped_names"] == ["Workflow"],
      "an unmatched task falls back to Phase B exactly — no extra removal")

fr4, _ = gw.negotiate(dict(CLAUDE_HEADERS, model="claude-3-5-sonnet-2024-10-22",
                           tools=list(TOOLS),
                           messages=[{"role": "user", "content": "fix the typo"}]),
                      "anthropic_messages")
check(fr4["task_class"] is None and fr4["tools_dropped"] == 0,
      "a passthrough model is never task-negotiated")
os.environ.pop(tg.TASK_ENV, None)

print("\nNAMESPACE EXPANSION (Codex MCP reachability)")
ns_cfg_off = {"enabled": False}
bundle = {"type": "namespace", "name": "mcp__lsp", "description": "LSP bundle",
          "tools": [
              {"type": "function", "name": "get_hover", "description": "Hover",
               "strict": False, "parameters": {"type": "object"}},
              {"type": "function", "name": "get_definition", "description": "Def",
               "strict": False, "parameters": {"type": "object"}}]}

out, info = tg.expand_namespaces([bundle], ns_cfg_off)
check(out == [bundle] and info == [],
      "disabled: the bundle passes through untouched")

cfg = reg.namespace_expansion()
check(cfg["enabled"] is False,
      "registry ships expansion DISABLED — flattening changes the name the "
      "model emits and the client must be able to route it")
check(cfg["name_template"] == "{namespace}__{tool}",
      "default template reproduces the mcp__<server>__<tool> convention")

on = dict(cfg, enabled=True)
out, info = tg.expand_namespaces([bundle], on, reg.group_of)
names = [tg.tool_name(t) for t in out]
check(names == ["mcp__lsp__get_hover", "mcp__lsp__get_definition"],
      f"flattened to mcp__lsp__* names (got {names})")
check(all(t.get("type") == "function" for t in out),
      "every expanded tool is type=function, which the route does NOT drop")
check(not any(t.get("type") == "namespace" for t in out),
      "the bundle itself is removed — keeping it would pay its bytes twice and "
      "be dropped downstream anyway")
check(info and info[0]["expanded"] == 2, "expansion is reported")

# The whole point: expanded tools are REACHABLE where the bundle was not.
rb, _ = gw.negotiate(dict(CODEX_HEADERS, model="ailocal-architecture", input="",
                          tools=[bundle]), "aresponses")
check(rb["bytes_kept_reachable"] == 0,
      "baseline: an unexpanded bundle contributes ZERO reachable bytes")

# Group awareness: flattened lsp tools land in the lsp group, so task
# negotiation and client profiles apply to them like any other tool.
check(reg.group_of("mcp__lsp__get_hover") == "lsp",
      "an expanded tool is grouped by the registry like any other")

only_search = dict(on, only_groups=["search"])
out2, info2 = tg.expand_namespaces([bundle], only_search, reg.group_of)
check(any(t.get("type") == "namespace" for t in out2),
      "only_groups filters expansion: an lsp bundle is left alone when only "
      "search was requested")

many = {"type": "namespace", "name": "mcp__big", "tools": [
    {"type": "function", "name": f"t{i}", "parameters": {"type": "object"}}
    for i in range(50)]}
out3, info3 = tg.expand_namespaces([many], dict(on, max_tools_per_namespace=40),
                                   reg.group_of)
check(any(t.get("type") == "namespace" for t in out3),
      "a bundle over the limit is REFUSED, not truncated — a half-expanded "
      "bundle advertises some tools and hides others with no way to tell")
check(info3 and info3[0].get("skipped"), "and the refusal is reported")

empty = {"type": "namespace", "name": "mcp__empty", "tools": []}
out4, _ = tg.expand_namespaces([empty], on, reg.group_of)
check(out4 == [empty], "an empty bundle is left as-is, not dropped")

print("\nFAIL OPEN")
empty = cr.Registry(path="/nonexistent", caps_json="/nonexistent",
                    config_path="/nonexistent")
gw_empty = tg.ToolGateway(registry=empty)
re_, ke_ = gw_empty.negotiate(dict(CLAUDE_HEADERS, model="ailocal-architecture",
                                   tools=list(TOOLS)), "anthropic_messages")
check(re_["tools_dropped"] == 0, "with no registry, nothing is dropped")
check(re_["passthrough"] is True, "with no registry, everything is passthrough")
check(re_["bytes_in"] == READ_B + WORKFLOW_B + LSP_B,
      "measurement still works without a registry")

unknown_client = {"model": "ailocal-architecture", "tools": list(TOOLS)}
ru, _ = gw.negotiate(unknown_client, "anthropic_messages")
check(ru["client"] == "unknown", "no headers -> unknown client")
check(ru["tools_dropped"] == 0,
      "an unknown client gets no filtering — guessing is how a gateway "
      "breaks a load-bearing tool")

print("\nMODE GATING (mutation is what can break production)")
def run(d, ct="anthropic_messages", gateway=None):
    return asyncio.run((gateway or gw).async_pre_call_hook(None, None, d, ct))

os.environ[tg.MODE_ENV] = "off"
d = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS))
run(d)
check(len(d["tools"]) == 3, "OFF: request untouched")

os.environ[tg.MODE_ENV] = "report"
d = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS))
run(d)
check(len(d["tools"]) == 3, "REPORT: measures but never mutates")

os.environ[tg.MODE_ENV] = "filter"
d = dict(CLAUDE_HEADERS, model="ailocal-architecture", tools=list(TOOLS))
run(d)
check([tg.tool_name(t) for t in d["tools"]] == ["Read", "mcp__lsp__get_hover"],
      "FILTER: the negotiated-away tool is removed, order preserved")

d = dict(CLAUDE_HEADERS, model="claude-3-5-sonnet-2024-10-22", tools=list(TOOLS))
run(d)
check(len(d["tools"]) == 3,
      "FILTER + frontier model: still untouched, flags cannot override "
      "passthrough")

os.environ[tg.MODE_ENV] = "FILTER"
check(tg.mode() == "filter", "mode is case-insensitive")
os.environ[tg.MODE_ENV] = "typo"
check(tg.mode() == "off", "an unrecognised mode falls back to off, reported")
os.environ.pop(tg.MODE_ENV, None)
check(tg.mode() == "off", "unset defaults to off")

print("\nROBUSTNESS")
os.environ[tg.MODE_ENV] = "filter"
d = {"model": "ailocal-architecture", "tools": ["not-a-dict", {"junk": 1}],
     **CLAUDE_HEADERS}
run(d)
check(len(d["tools"]) == 2, "malformed entries are never dropped, never crash")
check(run({"model": "x"}) is not None, "a payload with no tools returns cleanly")

print("\nTOKEN FIGURES (approximate by construction, never fabricated)")
rep, _ = gw.negotiate(dict(CLAUDE_HEADERS, model="ailocal-architecture",
                           tools=list(TOOLS)), "anthropic_messages")
if REAL_LITELLM:
    check(isinstance(rep["tokens_est_in"], int) and rep["tokens_est_in"] > 0,
          "a token estimate is produced when litellm is present")
    check(rep["tokens_est_kept"] < rep["tokens_est_in"],
          "filtering lowers the token estimate")
else:
    check(rep["tokens_est_in"] is None,
          "no counter available -> None, never a fabricated 0")
check(rep["tokenizer"] == "cl100k-proxy",
      "every report names the tokenizer, so no figure reads as model-exact")

print("\nNATIVE LSP (client-side tool, NOT mcp__lsp__*)")
# Claude Code's native LSP tool is a bare name. It used to survive only because
# the gateway fails open on unclassified tools — correct by accident, and
# silently lost the moment fail-open is tightened. The registry now names it in
# a `native_lsp` group listed in the `always` floor. These tests pin BOTH the
# classification and the outcome, so neither half can regress alone.
NATIVE_LSP = json.loads('{"description":"Language server","input_schema":'
                        '{"properties":{},"type":"object"},"name":"LSP"}')

check(reg.group_of("LSP") == "native_lsp",
      "native LSP is explicitly classified, not left to fail-open")
check(reg.group_of("LSP") != reg.group_of("mcp__lsp__get_hover"),
      "native LSP is a separate group from the MCP lsp bridge")
check("native_lsp" in (reg.doc.get("task_classes") or {}).get("always", []),
      "native_lsp sits in the always floor")

os.environ[tg.MODE_ENV] = "filter"
# Task negotiation must be ON for these: with it off, classification never runs
# and nothing would be shed, so the assertions would pass vacuously.
os.environ[tg.TASK_ENV] = "1"
check(tg.task_negotiation_enabled() is True, "task negotiation on for these cases")

# The conversational opener is the harshest case — that class carries
# override_always, dropping BELOW the always floor to no tools at all. Native
# LSP is expected to go with it there; what must hold is that every class which
# does keep a floor keeps native LSP in it.
for prompt, label, want in (
        ("fix the failing auth test", "debug task", True),
        ("review this diff for security issues", "review task", True),
        ("show me an example of hello world in C++", "conversational opener", False)):
    d = dict(CLAUDE_HEADERS, model="ailocal-architecture",
             messages=[{"role": "user", "content": prompt}],
             tools=[NATIVE_LSP, json.loads(WORKFLOW)])
    run(d)
    kept = [t.get("name") for t in d["tools"] if isinstance(t, dict)]
    check(("LSP" in kept) is want,
          f"native LSP {'survives' if want else 'is shed with the floor'}: {label}")

os.environ.pop(tg.TASK_ENV, None)

sys.exit(_suite.report())
