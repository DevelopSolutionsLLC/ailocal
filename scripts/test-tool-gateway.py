#!/usr/bin/env python3
"""test-tool-gateway.py — known-answer tests for config/litellm/tool_gateway.py.

The gateway's whole purpose is to report a number ("you can save N bytes"). A
measurement layer that is merely self-consistent will happily report a confident
wrong number, so these tests do not check the gateway against itself. Every byte
assertion is pinned to a LITERAL canonical JSON string written out by hand below:
the test asserts both that the gateway's encoder reproduces that exact string and
that its reported total equals the literal's length. If the encoder ever changes
(separators, ensure_ascii, sort_keys), these fail loudly rather than drifting.

What is pinned here:
  - exact byte accounting, per tool and in aggregate, against hand-written JSON
  - all three route envelopes (/v1/messages, /v1/chat/completions, /v1/responses)
    normalise to the same logical tool names
  - non-function /v1/responses entries (type "namespace") are COUNTED, not
    silently dropped from the inventory — they are real payload weight
  - no policy loaded => inventory reported, savings explicitly disclaimed
  - allowlist semantics incl. prefix wildcards
  - OFF never mutates; REPORT never mutates; only FILTER mutates data["tools"]
  - an unrecognised mode value is reported, not silently coerced

Token estimates are deliberately NOT pinned: litellm selects the cl100k
tokenizer for a Qwen backend, so those figures are an approximation whose value
is not the gateway's to guarantee. The tests assert only that a token figure is
either an int or None — never a fabricated zero.

Run: python3 scripts/test-tool-gateway.py   (stdlib only; exit 1 on failure)
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types

# ── stub `litellm` ONLY when it is genuinely absent ─────────────────────────
# Unconditionally stubbing it would mean these tests never exercise the real
# token counter even when run inside the proxy image, where it does exist.
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["AILOCAL_TOOL_POLICY"] = "/nonexistent"     # default: no policy
os.environ["AILOCAL_CONFIG_PATH"] = "/nonexistent"     # default: no aliases
os.environ.pop("AILOCAL_TOOL_GATEWAY_CAPTURE", None)   # never write files

# The policy tests need PyYAML, which the host python does not have but the
# proxy image does. AILOCAL_GATEWAY_MODULE lets the same file run inside the
# container against /app/config/tool_gateway.py — the module production loads.
# See scripts/test-tool-gateway.sh, which runs it both ways.
MODULE = os.environ.get("AILOCAL_GATEWAY_MODULE",
                        os.path.join(ROOT, "config/litellm/tool_gateway.py"))
_spec = importlib.util.spec_from_file_location("tool_gateway", MODULE)
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

fails = 0
def check(cond, name):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails += 1


# ═══════════════════════════════════════════════════════════════════════════
# GROUND TRUTH
# Hand-written canonical JSON. These literals — not the gateway — define the
# expected bytes. Keys are in sorted order and there is no whitespace, which is
# exactly what tool_gateway.encode() must produce.
# ═══════════════════════════════════════════════════════════════════════════

ANTHROPIC_READ = (
    '{"description":"Read a file","input_schema":'
    '{"properties":{"path":{"type":"string"}},"required":["path"],'
    '"type":"object"},"name":"Read"}'
)
ANTHROPIC_BASH = (
    '{"description":"Run a shell command","input_schema":'
    '{"properties":{"cmd":{"type":"string"}},"required":["cmd"],'
    '"type":"object"},"name":"Bash"}'
)
# A deliberately fat tool: this is the shape of the thing worth filtering.
ANTHROPIC_FAT = (
    '{"description":"' + ("x" * 500) + '","input_schema":'
    '{"properties":{},"type":"object"},"name":"mcp__big__thing"}'
)

READ_BYTES = len(ANTHROPIC_READ.encode("utf-8"))
BASH_BYTES = len(ANTHROPIC_BASH.encode("utf-8"))
FAT_BYTES = len(ANTHROPIC_FAT.encode("utf-8"))

print(f"\nGround truth (hand-written canonical JSON):")
print(f"  Read = {READ_BYTES} B   Bash = {BASH_BYTES} B   "
      f"mcp__big__thing = {FAT_BYTES} B")

anthropic_tools = [json.loads(s) for s in
                   (ANTHROPIC_READ, ANTHROPIC_BASH, ANTHROPIC_FAT)]

print("\nENCODER (the basis of every byte figure)")
check(tg.encode(json.loads(ANTHROPIC_READ)) == ANTHROPIC_READ,
      "encode() reproduces the hand-written canonical JSON exactly")
check(tg.tool_bytes(json.loads(ANTHROPIC_READ)) == READ_BYTES,
      f"tool_bytes(Read) == {READ_BYTES} (literal length, computed independently)")
check(tg.encode({"b": 1, "a": 2}) == '{"a":2,"b":1}',
      "encode() sorts keys, so byte counts are order-independent")
# '{"k":"é"}' is 9 characters but 10 bytes — é is two bytes in UTF-8. The
# gateway must report the wire cost, so this pins bytes, not len(str).
check(tg.encode({"k": "é"}) == '{"k":"é"}', "encode() emits UTF-8, not \\uXXXX")
check(len(tg.encode({"k": "é"}).encode("utf-8")) == 10,
      "byte accounting counts UTF-8 bytes (10), not characters (9)")


# ═══════════════════════════════════════════════════════════════════════════
# ROUTE NORMALISATION
# ═══════════════════════════════════════════════════════════════════════════

openai_tools = [
    {"type": "function", "function": {
        "name": "Read", "description": "Read a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "Bash", "description": "Run a shell command",
        "parameters": {"type": "object",
                       "properties": {"cmd": {"type": "string"}},
                       "required": ["cmd"]}}},
]
responses_tools = [
    {"type": "function", "name": "Read", "description": "Read a file",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}}}},
    # No name at all — the /v1/responses entries LiteLLM logs as
    # "Dropping Responses API tool of type 'namespace'". Still bytes on the wire.
    {"type": "namespace", "namespace": {"name": "shell",
                                        "tools": [{"name": "exec"}]}},
]

gw = tg.ToolGateway(policy=tg.Policy("/nonexistent"), alias={})

print("\nROUTE NORMALISATION (same tool, three envelopes)")
r, _ = gw.measure({"model": "m", "tools": anthropic_tools},
                  "anthropic_messages")
check(r["route"] == "/v1/messages", "anthropic call_type -> /v1/messages")
check([n for n, _ in r["largest"]][:1] == ["mcp__big__thing"],
      "largest[] is sorted by cost, fattest first")
check(r["bytes_in"] == READ_BYTES + BASH_BYTES + FAT_BYTES,
      f"anthropic bytes_in == {READ_BYTES + BASH_BYTES + FAT_BYTES} "
      "(sum of the three literals)")

r_o, _ = gw.measure({"model": "m", "tools": openai_tools}, "acompletion")
check(r_o["route"] == "/v1/chat/completions",
      "no 'input' key -> /v1/chat/completions")
check(sorted(n for n, _ in tg.inventory(openai_tools)) == ["Bash", "Read"],
      "openai nested function.name is unwrapped to the logical name")

r_r, _ = gw.measure({"model": "m", "input": "x", "tools": responses_tools},
                    "aresponses")
check(r_r["route"] == "/v1/responses", "'input' key -> /v1/responses")
names = [n for n, _ in tg.inventory(responses_tools)]
check(names == ["Read", "<namespace>"],
      "unnamed /v1/responses entry is counted as <namespace>, not dropped")
check(r_r["tools_in"] == 2 and r_r["bytes_in"] > 0,
      "non-function entries contribute to the inventory and its byte total")

check(tg.tool_name({"junk": 1}) == "<unknown>",
      "a tool with neither name nor type degrades to <unknown>, still counted")
check(tg.tool_name("not-a-dict") == "<malformed>",
      "a non-dict tool entry does not crash the inventory")


# ═══════════════════════════════════════════════════════════════════════════
# NO POLICY => NO SAVINGS CLAIM
# ═══════════════════════════════════════════════════════════════════════════

print("\nUNGROUNDED SAVINGS ARE REFUSED")
# Which "no policy" state is correct depends on the environment, and the point
# of the state field is that those two situations stay distinguishable.
expected_state = "absent" if HAVE_YAML else "unavailable"
check(r["policy"] == expected_state,
      f"no policy here reports the specific reason: {expected_state}")
if HAVE_YAML:
    # A corrupt policy must be distinguishable from an absent one: the first is
    # an operator error to fix, the second is the shipped default.
    _bad = os.path.join(tempfile.mkdtemp(), "bad.yaml")
    with open(_bad, "w") as f:
        f.write("groups: [unclosed\n")
    check(tg.Policy(_bad).state == "error",
          "a malformed policy reports 'error', not 'absent'")
    check(tg.Policy(_bad).allowed_names("any", "any") is None,
          "a malformed policy denies nothing — it fails open, loudly")
check(r["tools_dropped"] == 0 and r["bytes_dropped"] == 0,
      "with no policy, nothing is counted as droppable")
check(str(r.get("savings_claim", "")).startswith("none —"),
      "with no policy, the report explicitly disclaims savings")
check(r["tools_kept"] == r["tools_in"],
      "no policy means allow-all, not deny-all (fail open, never silently strip)")


# ═══════════════════════════════════════════════════════════════════════════
# POLICY SEMANTICS
# ═══════════════════════════════════════════════════════════════════════════

POLICY_YAML = """
groups:
  core:
    - Read
    - Bash
  mcp:
    - "mcp__*"
rules:
  - match: {client: claude-code, capability: implementation}
    allow: [core]
  - match: {client: "*", capability: "*"}
    allow: [core, mcp]
"""

policy_file = os.path.join(tempfile.mkdtemp(), "tool-policy.yaml")
with open(policy_file, "w") as f:
    f.write(POLICY_YAML)

try:
    import yaml  # noqa: F401
    have_yaml = True
except ImportError:
    have_yaml = False

print("\nPOLICY SEMANTICS" + ("" if have_yaml else "  [SKIPPED - no PyYAML]"))
if have_yaml:
    pol = tg.Policy(policy_file)
    gwp = tg.ToolGateway(policy=pol, alias={
        "claude-sonnet-4-6": "ailocal-implementation"})

    check(pol.loaded, "policy file loads")

    data = {"model": "claude-sonnet-4-6", "tools": anthropic_tools,
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0"}}}
    rp, keep = gwp.measure(data, "anthropic_messages")

    check(rp["client"] == "claude-code", "client detected from user-agent header")
    check(rp["capability"] == "implementation",
          "compat alias resolved to its capability for policy matching")
    check(rp["policy"] == "loaded", "policy reported as loaded")
    check(rp["dropped_names"] == ["mcp__big__thing"],
          "the first matching rule wins: core-only drops the mcp tool")
    check(rp["bytes_dropped"] == FAT_BYTES,
          f"bytes_dropped == {FAT_BYTES}, the literal size of the dropped tool")
    check(rp["bytes_kept"] == READ_BYTES + BASH_BYTES,
          "bytes_kept == the two retained literals")
    check(rp["bytes_kept"] + rp["bytes_dropped"] == rp["bytes_in"],
          "kept + dropped == in (the accounting closes)")

    # A different client falls through to the wildcard rule, which allows mcp__*.
    data2 = {"model": "claude-sonnet-4-6", "tools": anthropic_tools,
             "proxy_server_request": {"headers": {"originator": "codex_cli_rs"}}}
    rp2, _ = gwp.measure(data2, "acompletion")
    check(rp2["client"] == "codex", "client detected from originator header")
    check(rp2["tools_dropped"] == 0,
          "prefix wildcard mcp__* admits mcp__big__thing for the wildcard rule")

    check(pol.permits("mcp__x", {"mcp__*"}) is True, "prefix wildcard matches")
    check(pol.permits("other", {"mcp__*"}) is False, "prefix wildcard is not a catch-all")
    check(pol.permits("anything", None) is True, "allowed=None means no opinion => permit")

    # Regression: Codex sends web search as a bare {"type":"web_search"} with no
    # name. It must survive any allowlist, or websearch_interception has nothing
    # to rewrite and SearXNG search dies silently.
    check(pol.permits("<web_search>", set()) is True,
          "an unnamed entry survives even a deny-everything allowlist")
    check(pol.permits("<namespace>", {"Read"}) is True,
          "unnamed entries are never filtered — policy is written in names")
    check(pol.permits("Workflow", {"Read"}) is False,
          "...but a NAMED tool absent from the allowlist is still dropped")

    rws, _ = gwp.measure(
        {"model": "claude-sonnet-4-6", "input": "",
         "tools": [{"type": "web_search"},
                   {"name": "Workflow", "description": "orchestrate",
                    "input_schema": {"type": "object"}}],
         "proxy_server_request": {"headers": {"originator": "codex_cli_rs"}}},
        "aresponses")
    check("<web_search>" not in rws["dropped_names"],
          "end to end: web_search is never in the drop list")


# ═══════════════════════════════════════════════════════════════════════════
# MODE GATING — what actually reaches the backend
# ═══════════════════════════════════════════════════════════════════════════

def run(data, call_type="anthropic_messages", gateway=None):
    return asyncio.run(
        (gateway or gw).async_pre_call_hook(None, None, data, call_type))

# ═══════════════════════════════════════════════════════════════════════════
# CREDIT FOR WORK LITELLM ALREADY DOES
# On /v1/responses, LiteLLM discards namespace/shell/computer_use/
# image_generation tools before they reach the backend. Those bytes never cost
# the model anything, so the gateway must not book them as a saving.
# ═══════════════════════════════════════════════════════════════════════════

print("\nBACKEND-REACHABLE ACCOUNTING (no credit for LiteLLM's own drops)")

# Shape taken from a real captured Codex payload: namespace entries DO carry a
# top-level name (multi_agent_v1, mcp__lsp, mcp__grepai), so they are nameable
# and therefore policy-addressable — unlike the bare {"type":"web_search"}.
ns_tool = {"type": "namespace", "name": "multi_agent_v1",
           "description": "Tools for spawning and managing sub-agents.",
           "tools": [{"name": "spawn"}]}
fn_tool = {"type": "function", "name": "exec_command",
           "description": "Run a command",
           "parameters": {"type": "object",
                          "properties": {"cmd": {"type": "string"}}}}
NS_BYTES = tg.tool_bytes(ns_tool)
FN_BYTES = tg.tool_bytes(fn_tool)

for t in tg.RESPONSES_DROPPED_TYPES:
    check(tg.reaches_backend({"type": t}, "/v1/responses") is False,
          f"/v1/responses: type '{t}' is known-dropped by LiteLLM")
    check(tg.reaches_backend({"type": t}, "/v1/messages") is True,
          f"/v1/messages: type '{t}' is NOT pre-dropped (rule is route-specific)")
check(tg.reaches_backend(fn_tool, "/v1/responses") is True,
      "/v1/responses: type 'function' does reach the backend")
check(tg.reaches_backend({"type": "custom"}, "/v1/responses") is True,
      "/v1/responses: type 'custom' is converted, not dropped")

rr, _ = gw.measure({"model": "m", "input": "x", "tools": [ns_tool, fn_tool]},
                   "aresponses")
check(rr["bytes_in"] == NS_BYTES + FN_BYTES,
      "bytes_in still counts everything the client sent, including namespaces")
check(rr["bytes_reachable"] == FN_BYTES,
      f"bytes_reachable == {FN_BYTES}: only the function tool reaches the model")
check(rr["bytes_prefiltered_by_litellm"] == NS_BYTES,
      "the namespace bytes are attributed to LiteLLM, not to this gateway")

if HAVE_YAML:
    # A policy that denies everything: the namespace tool is "dropped", but that
    # drop is moot — LiteLLM was discarding it anyway.
    deny_all = os.path.join(tempfile.mkdtemp(), "deny.yaml")
    with open(deny_all, "w") as f:
        f.write("groups:\n  none: []\nrules:\n"
                "  - match: {client: '*', capability: '*'}\n    allow: [none]\n")
    gwd = tg.ToolGateway(policy=tg.Policy(deny_all), alias={})
    rd, _ = gwd.measure({"model": "m", "input": "x",
                         "tools": [ns_tool, fn_tool]}, "aresponses")
    check(rd["tools_dropped"] == 2, "deny-all drops both entries")
    check(rd["bytes_dropped"] == FN_BYTES,
          "only the reachable tool's bytes count as this gateway's saving")
    check(rd["bytes_dropped_moot"] == NS_BYTES,
          "the namespace drop is booked as moot, not as a saving")
    check(rd["bytes_dropped"] + rd["bytes_dropped_moot"]
          + rd["bytes_kept"] == rd["bytes_in"],
          "saving + moot + kept == in (the accounting still closes)")

print("\nMODE GATING (mutation is the thing that can break production)")

os.environ[tg.MODE_ENV] = "off"
d = {"model": "m", "tools": list(anthropic_tools)}
run(d)
check(len(d["tools"]) == 3, "OFF: request untouched")

os.environ[tg.MODE_ENV] = "report"
d = {"model": "m", "tools": list(anthropic_tools)}
run(d)
check(len(d["tools"]) == 3, "REPORT: measures but never mutates")

if have_yaml:
    os.environ[tg.MODE_ENV] = "report"
    d = {"model": "claude-sonnet-4-6", "tools": list(anthropic_tools),
         "proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0"}}}
    run(d, gateway=gwp)
    check(len(d["tools"]) == 3,
          "REPORT with a policy that WOULD drop: still does not mutate")

    os.environ[tg.MODE_ENV] = "filter"
    d = {"model": "claude-sonnet-4-6", "tools": list(anthropic_tools),
         "proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0"}}}
    run(d, gateway=gwp)
    check([tg.tool_name(t) for t in d["tools"]] == ["Read", "Bash"],
          "FILTER: the disallowed tool is actually removed, order preserved")

os.environ[tg.MODE_ENV] = "FILTER"
check(tg.mode() == "filter", "mode is case-insensitive")
os.environ[tg.MODE_ENV] = "typo-mode"
check(tg.mode() == "off", "an unrecognised mode falls back to off (and is reported)")
os.environ.pop(tg.MODE_ENV, None)
check(tg.mode() == "off", "unset mode defaults to off")

print("\nTOKEN FIGURES (approximate by construction — never fabricated)")
os.environ[tg.MODE_ENV] = "report"
r, _ = gw.measure({"model": "m", "tools": anthropic_tools}, "anthropic_messages")
if REAL_LITELLM:
    check(isinstance(r["tokens_est_in"], int) and r["tokens_est_in"] > 0,
          "with real litellm present, a token estimate is produced")
    check(r["tokens_est_in"] < r["bytes_in"],
          "token estimate is below the byte count (sanity: >1 byte per token)")
else:
    check(r["tokens_est_in"] is None,
          "with no counter available, tokens are None — never a fabricated 0")
check(r["tokenizer"] == "cl100k-proxy",
      "every report names the tokenizer, so no figure is quoted as Qwen-exact")

print()
if fails:
    print(f"TOOL GATEWAY TESTS: {fails} FAILED")
    sys.exit(1)
print("TOOL GATEWAY TESTS: OK")
