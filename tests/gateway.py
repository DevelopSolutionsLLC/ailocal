#!/usr/bin/env python3
"""Gateway-side request handling: persona injection, tool-call repair, traces.

Three sections, separately addressable so the gate reports them as distinct
behaviours:

  persona   server-side persona injection across the OpenAI and Anthropic
            dialects, including compat aliases and idempotency.
  repair    recovery of fenced JSON tool calls, and the harder direction --
            refusing to fire on tutorial examples.
  trace     E1 request-trace schema, redaction, and token reconciliation.

Each section owns its statements; module level holds only imports, the litellm
import shims, loaded modules and fixtures. Sections are independent, emit no
checks at import, and may run in any order.

Usage: gateway.py [persona|repair|trace]   (default: all)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, REPO, Suite, load_module  # noqa: E402

_suite = Suite()
check = _suite.check

# ── persona ─────────────────────────────────────────────────────────────
import asyncio
import importlib.util
import os
import sys
import types
_clog = types.ModuleType("litellm.integrations.custom_logger")
class _CustomLogger:            # minimal stand-in for CustomLogger
    def __init__(self, *a, **k): pass
_clog.CustomLogger = _CustomLogger
sys.modules["litellm"] = types.ModuleType("litellm")
sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
sys.modules["litellm.integrations.custom_logger"] = _clog
from pathlib import Path
ROOT = str(REPO)
os.environ.setdefault("AILOCAL_INSTRUCTIONS_DIR", "/nonexistent")   # _load_personas → {} (we override)
pi = load_module("persona_injector",
                 os.path.join(RESOURCES, "deploy/litellm/hooks/persona_injector.py"))
inj = pi.PersonaInjector()
inj.personas = {"implementation": "IMPL_XYZ", "architecture": "ARCH_XYZ", "review": "REV_XYZ"}
inj.alias = {"claude-sonnet-4-6": "ailocal-implementation"}
P = "IMPL_XYZ"
def hook(data, call_type):
    return asyncio.run(inj.async_pre_call_hook(None, None, data, call_type))

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
TRACE = RESOURCES / "deploy" / "litellm" / "hooks" / "request_trace.py"
def load_trace_module():
    """Import request_trace for real, with litellm stubbed.

    The module imports `litellm.integrations.custom_logger`, which exists only
    in the proxy image. Stubbing that one symbol runs the ACTUAL module rather
    than exec'ing a slice of its source, so these checks cannot pass against
    code the proxy would not execute.
    """
    import importlib.util
    import sys
    import types
    if "litellm" not in sys.modules:
        pkg = types.ModuleType("litellm")
        integ = types.ModuleType("litellm.integrations")
        cl = types.ModuleType("litellm.integrations.custom_logger")
        cl.CustomLogger = type("CustomLogger", (), {})
        sys.modules.update({"litellm": pkg, "litellm.integrations": integ,
                            "litellm.integrations.custom_logger": cl})
    spec = importlib.util.spec_from_file_location("request_trace", TRACE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def historical_compatibility_checks() -> None:
    """Real records on disk must stay readable across every schema version.

    A reader tolerates fields older records carry and the current version no
    longer writes; that tolerance is what this proves.
    """
    print("\nhistorical record compatibility")
    import glob
    total = bad = 0
    versions = set()
    for path in glob.glob(str(Path.home() /
                              ".local/state/ailocal/captures/traces/*.jsonl")):
        for line in open(path, errors="replace"):
            total += 1
            try:
                versions.add(json.loads(line).get("event_version"))
            except Exception:  # noqa: BLE001
                bad += 1
    if not total:
        print("  - no historical records present; skipped")
        return
    check(bad == 0, f"all {total} historical records parse ({bad} failures)")
    check(len(versions) > 1,
          f"records from several schema versions coexist {sorted(v for v in versions if v)}")


SECRETS = [
    "ghp_REALLOOKINGTOKENVALUE0000000000",
    "github_pat_11ABCDEFG0000000000000",
    "sk-abcdef0123456789abcdef0123456789",
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
]
PROMPT_MARKERS = [
    "SECRET_BUSINESS_PLAN_PARAGRAPH",
    "def internal_pricing_algorithm(",
    "the customer's home address is",
]


def _trace_body() -> None:
    mod = load_trace_module()
    print("E1 SCHEMA")
    check(mod.EVENT_VERSION >= 5, f"event_version is set ({mod.EVENT_VERSION})")
    check(mod._PROCESS_GENERATION.startswith("pg-"),
          "a stable process generation identifier exists")

    print("\nREDACTION: SECRET- AND PROMPT-SHAPED VALUES NEVER SERIALIZE")
    # Built through the real record builder, on a request carrying a secret or
    # customer content in every field a trace touches.
    tracer = mod.RequestTrace()
    hostile = {
        "model": "ailocal-implementation",
        "tools": [{"name": "Read", "description": SECRETS[0] + PROMPT_MARKERS[1]}],
        "system": "You are a helpful assistant. " + SECRETS[2] + PROMPT_MARKERS[0],
        "messages": [
            {"role": "user", "content": PROMPT_MARKERS[2] + " " + SECRETS[1]},
            {"role": "user", "content": [
                {"type": "tool_result", "content": PROMPT_MARKERS[1] + SECRETS[3]}]},
        ],
        "litellm_params": {"api_base": f"http://{SECRETS[2]}@ollama:11434",
                           "model": "ollama_chat/qwen3-coder:30b"},
        # The user-agent IS recorded, deliberately and truncated — it is how a
        # record is attributed to a client. Authorization must never appear.
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/2.0.1",
                                             "authorization": SECRETS[3]}},
        "max_tokens": 100,
    }
    record = tracer._base(hostile, call_type="acompletion")
    blob = json.dumps(record, default=str)
    for secret in SECRETS:
        check(secret not in blob, f"no secret {secret[:12]}... in the record")
    for marker in PROMPT_MARKERS:
        check(marker not in blob, f"no prompt/source content {marker[:24]!r}")
    check(not re.search(r"(ghp_|github_pat_|sk-|Bearer )", blob),
          "no credential-shaped substring survives at all")
    check(all(isinstance(v, (int, float, str, bool, type(None)))
              for v in record.values()),
          "every emitted field is a bounded scalar")
    check(all(not isinstance(v, str) or len(v) <= 130 for v in record.values()),
          "no field carries an unbounded string")

    print("\nIDENTITY IS CARRIED, NEVER INFERRED")
    check(record["requested_alias"] == "ailocal-implementation",
          "the alias the client asked for is recorded")
    check(record["resolved_backend_model"] == "ollama_chat/qwen3-coder:30b",
          "the backend that served it is recorded separately")
    check(record["tools_declared"] == 1 and record["messages"] == 2,
          "payload shape is counted, never retained")
    # The alias alone tells us nothing about the backend; claiming otherwise
    # would manufacture evidence.
    bare = tracer._base({"model": "ailocal-fast"}, call_type="acompletion")
    check(bare["resolved_backend_model"] is None,
          "no backend is claimed when only the alias is known")

    historical_compatibility_checks()


def persona_checks() -> None:
    d = hook({"model": "ailocal-implementation", "messages": [{"role": "user", "content": "hi"}]}, "acompletion")
    sys0 = d["messages"][0]
    check(sys0["role"] == "system" and sys0["content"].startswith(P),
          "openai: persona inserted as system when none present")
    d = hook({"model": "ailocal-implementation",
              "messages": [{"role": "system", "content": "CLIENT_SYS"}, {"role": "user", "content": "hi"}]}, "acompletion")
    c = d["messages"][0]["content"]
    check(P in c and "CLIENT_SYS" in c and c.index(P) < c.index("CLIENT_SYS"),
          "openai: persona prepended to existing system, client text preserved")
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
    d = hook({"model": "ailocal-completion", "messages": [{"role": "user", "content": "hi"}]}, "acompletion")
    check(all(m["role"] != "system" for m in d["messages"]),
          "openai: no persona for a capability without a persona file (completion)")
    d = hook({"model": "ailocal-completion", "messages": [{"role": "user", "content": "hi"}]}, "anthropic_messages")
    check("system" not in d,
          "anthropic: no persona for a capability without a persona file (completion)")
    d = hook({"model": "ailocal-implementation", "input": "x"}, "embeddings")
    check("system" not in d and "messages" not in d,
          "embeddings call_type: request passes through untouched")
    data = {"model": "ailocal-implementation", "messages": [{"role": "user", "content": "hi"}]}
    hook(data, "acompletion"); hook(data, "acompletion")
    hook(data, "acompletion"); hook(data, "acompletion")
    check(data["messages"][0]["content"].count(P) == 1, "openai: injection is idempotent (no doubling)")
    data = {"model": "claude-sonnet-4-6", "system": "CLIENT_SYS", "messages": [{"role": "user", "content": "hi"}]}
    hook(data, "anthropic_messages"); hook(data, "anthropic_messages")
    hook(data, "anthropic_messages"); hook(data, "anthropic_messages")
    check(data["system"].count(P) == 1, "anthropic: injection is idempotent (no doubling)")
    print()


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


def trace_checks() -> None:
    _trace_body()


SECTIONS = {"persona": persona_checks, "repair": repair_checks,
            "trace": trace_checks}

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in SECTIONS:
        sys.exit(f"unknown section {which!r}; expected one of {sorted(SECTIONS)}")
    for name in ([which] if which else list(SECTIONS)):
        SECTIONS[name]()
    sys.exit(_suite.report())
