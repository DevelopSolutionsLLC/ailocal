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
def load_helpers():
    """Execute only the module-level helpers, skipping the litellm import.

    The module imports `litellm.integrations.custom_logger`, which exists only in the
    proxy image. Rather than skip the whole suite on the host, the pure helper
    definitions are extracted and exec'd — the functions under test are
    self-contained by design.
    """
    src = TRACE.read_text()
    # Anchored on CODE, never on a comment banner: a prose edit must not be able
    # to break a behavioural test.
    start = src.index("EVENT_VERSION = ")
    end = src.index("def emit(record):")   # helpers end where emit begins
    ns: dict = {}
    exec("import json, os, time\n" + src[start:end], ns)
    return ns
def load_completion_helpers():
    """Same trick as load_helpers, for the completion-evidence renderer.

    _completion_fields lives above the E1 marker but references the availability
    constants defined below it, so both ranges are exec'd into one namespace,
    constants first. Source-range extraction keeps these tests runnable without
    litellm installed, which is what lets them run in the gate.
    """
    src = TRACE.read_text()
    e1 = src[src.index("EVENT_VERSION = "):src.index("def emit(record):")]
    fns = src[src.index("def _completion_fields(acc, saw_any_event):"):
              src.index("def _load_registry():")]
    ns: dict = {}
    exec("import json, os, time\n" + e1 + "\n" + fns, ns)
    return ns
def context_metadata_checks() -> None:
    """Configured context geometry must be reportable WITHOUT the registry.

    declared_context_tokens was permanently null for every bench-* alias: the
    registry does not know temporary aliases, and the fallback looked under
    litellm_params.model_info, which is a SIBLING of litellm_params rather than
    a child. So the one field that could have shown an over-admission was the
    one guaranteed to be empty.
    """
    print("\ncontext admission metadata")
    ns = load_helpers()
    budget = ns["_context_budget"]

    # The exact planner geometry.
    data = {"model": "bench-x", "max_tokens": 8192,
            "litellm_params": {"num_ctx": 40960, "num_predict": 8192},
            "model_info": {"max_input_tokens": 45875}}
    out = budget(None, data, {"input_tokens_estimated_total": 39157})
    check(out["configured_num_ctx"] == 40960, "configured num_ctx recorded (40960)")
    check(out["configured_num_predict"] == 8192,
          "configured num_predict recorded (8192)")
    check(out["usable_input_tokens"] == 32768,
          "usable input = num_ctx - reserved output (32768)")
    check(out["admission_limit_tokens"] == 45875,
          "admission limit read without the registry (45875)")
    check(out["admission_exceeds_usable_input"] is True,
          "admission ABOVE physical capacity is flagged in the record")
    check(out["model_native_context_tokens"] is None
          and out["model_native_context_availability"]
          == "not_exposed_by_litellm_hook",
          "the model's native window is not fabricated")

    # num_predict -1 is Ollama's INFINITE, not a reservation of 1 token.
    neg = budget(None, {"model": "m", "litellm_params":
                        {"num_ctx": 24576, "num_predict": -1}}, {})
    check(neg["configured_num_predict"] == -1, "negative num_predict is preserved")
    check(neg["usable_input_tokens"] is None
          and neg["usable_input_availability"] == "num_predict_unbounded_or_absent",
          "unbounded num_predict ⇒ usable input NOT computable, not the full window")
    check(neg["admission_exceeds_usable_input"] is False,
          "no usable figure ⇒ no over-admission claim")

    # A safe geometry must not be flagged.
    safe = budget(None, {"model": "m", "litellm_params":
                         {"num_ctx": 40960, "num_predict": 8192},
                         "model_info": {"max_input_tokens": 32768}}, {})
    check(safe["admission_exceeds_usable_input"] is False,
          "admission within capacity is not flagged")
def schema_and_dialect_checks() -> None:
    """The two schema faults that each cost a wasted run."""
    print("\nschema normalization and stream dialects")
    src = TRACE.read_text()
    ns: dict = {}
    exec("import json, os, time\n"
         + src[src.index("def _classify_event(item)"):src.index("def _completion_fields(")],
         ns)
    classify, resolved = ns["_classify_event"], ns["_resolved_backend"]

    # /v1/messages delivers raw SSE bytes. Every branch used to assume a mapping,
    # so first_visible_text_ms was silently null on the Anthropic route.
    text_frame = (b'event: content_block_delta\ndata: {"type": '
                  b'"content_block_delta", "index": 0, "delta": '
                  b'{"type": "text_delta", "text": "Sure"}}\n\n')
    check(classify(text_frame) == "text", "raw SSE bytes classify as visible text")
    check(classify(b'event: message_stop\ndata: {"type": "message_stop"}\n\n') is None,
          "a non-text SSE frame is not miscounted as text")
    check(classify(b'data: {"type": "input_json_delta"}\n\n') == "tool",
          "raw SSE tool frames classify as tool")

    # The object dialects must keep working unchanged.
    check(classify({"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "x"}}) == "text",
          "dict content_block_delta still classifies as text")
    check(classify({"choices": [{"delta": {"content": "x"}}]}) == "text",
          "OpenAI delta still classifies as text")

    # `model` was overloaded: alias on pre-call, backend on completion.
    check(resolved({"requested_alias": "bench-x"}, {"model": "ollama/real"})
          == "ollama/real", "backend is reported when it differs from the alias")
    check(resolved({"requested_alias": "bench-x"}, {"model": "bench-x"}) is None,
          "the alias is never repeated as a backend model")
    check(resolved({}, {"model": "bench-x"}) is None,
          "no alias recorded ⇒ no backend claim")
def historical_compatibility_checks() -> None:
    """Real records on disk must stay readable across every schema version."""
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
    check(None in versions or 1 in versions or 2 in versions,
          f"pre-v4 records are present and readable {sorted(v for v in versions if v)}")
def completion_evidence_checks() -> None:
    """What makes a completion record usable as evidence.

    Each check encodes a way the previous schema misled an investigation: nulls
    that meant four different things, and a partially observed response that
    looked complete. The EXTRACTION of these values is LiteLLM's job (it hands
    us an assembled ModelResponse); what is ours, and therefore what is tested,
    is how presence and absence are reported.
    """
    print("\ncompletion evidence")
    fields = load_completion_helpers()["_completion_fields"]

    # Ollama via ollama_chat hitting its ceiling -- the case the whole
    # output-limit investigation turns on.
    out = fields({"completion_tokens": 128, "prompt_tokens": 41,
                  "finish_reason": "length"}, True)
    check(out["completion_tokens"] == 128, "completion_tokens retained (128)")
    check(out["prompt_tokens"] == 41, "prompt_tokens retained (41)")
    check(out["finish_reason"] == "length", "finish_reason retained ('length')")
    check(out["completion_evidence"] == "EVIDENCE_COMPLETE",
          "count + reason \u21d2 EVIDENCE_COMPLETE")
    check(out["completion_tokens_availability"] is None,
          "a present value carries no availability reason")

    out = fields({"completion_tokens": 8192, "stop_reason": "max_tokens"}, True)
    check(out["stop_reason"] == "max_tokens", "anthropic stop_reason retained")
    check(out["completion_evidence"] == "EVIDENCE_COMPLETE",
          "stop_reason counts as a termination reason")

    # Absence is explained, never inferred.
    out = fields({}, True)
    check(out["completion_tokens"] is None
          and out["completion_tokens_availability"] == "not_sent_by_provider",
          "provider sent nothing \u21d2 not_sent_by_provider")
    check(out["completion_evidence"] == "EVIDENCE_NONE",
          "no telemetry at all \u21d2 EVIDENCE_NONE")
    check(fields({}, False)["completion_tokens_availability"] == "no_backend_response",
          "no response \u21d2 no_backend_response, distinct from an empty reply")

    # Partial telemetry must NEVER read as complete.
    check(fields({"completion_tokens": 500}, True)["completion_evidence"]
          == "EVIDENCE_PARTIAL",
          "count without a termination reason \u21d2 EVIDENCE_PARTIAL")
    check(fields({"finish_reason": "stop"}, True)["completion_evidence"]
          == "EVIDENCE_PARTIAL",
          "reason without a count \u21d2 EVIDENCE_PARTIAL")

    # Extraction failure is its own state, not silent absence.
    out = fields({"extraction_error": "boom"}, True)
    check(out["completion_tokens_availability"] == "extraction_failed",
          "parse failure reported as extraction_failed, not as absence")
    check(out["completion_extraction_error"] == "boom",
          "the extraction error is preserved for diagnosis")

    # Provider evidence is never manufactured from a mapped value.
    check(fields({"finish_reason": "length"}, True)["provider_done_reason"] is None,
          "provider_done_reason is NOT defaulted from finish_reason")

    # Still bounded scalars, so the record stays safe to serialize.
    check(all(isinstance(v, (int, float, str, bool, type(None)))
              for v in fields({"completion_tokens": 5,
                               "finish_reason": "stop"}, True).values()),
          "completion fields are bounded scalars")
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
    ns = load_helpers()
    print("E1 SCHEMA")
    check(ns["EVENT_VERSION"] >= 2, f"event_version is set ({ns['EVENT_VERSION']})")
    check(ns["_PROCESS_GENERATION"].startswith("pg-"),
          "a stable process generation identifier exists")

    print("\nUPSTREAM HOST IS RECORDED WITHOUT CREDENTIALS")
    host = ns["_upstream_host"]({"litellm_params":
                                 {"api_base": "http://host.docker.internal:11434"}})
    check(host == "host.docker.internal:11434", f"plain host recorded ({host})")
    dirty = ns["_upstream_host"]({"litellm_params":
        {"api_base": "https://user:sk-supersecret@ollama.internal:11434/v1?key=sk-two"}})
    check(dirty is not None and "sk-supersecret" not in dirty and "sk-two" not in dirty,
          f"user-info and query string are stripped, not trimmed ({dirty})")
    check(dirty == "ollama.internal:11434", f"only host:port survives ({dirty})")

    print("\nTOKEN COMPONENTS RECONCILE AND ARE DISJOINT")
    data = {
        "tools": [{"name": "Read", "description": "x" * 400, "input_schema": {}},
                  {"name": "Bash", "description": "y" * 400, "input_schema": {}}],
        "system": "s" * 800,
        "messages": [
            {"role": "user", "content": "u" * 1200},
            {"role": "assistant", "content": "a" * 1200},
            {"role": "user", "content": [
                {"type": "tool_result", "content": "r" * 4000}]},
        ],
        "max_tokens": 4096,
    }
    c = ns["_token_components"](data)
    parts = ("schema_tokens_estimated", "system_instruction_tokens_estimated",
             "conversation_history_tokens_estimated", "tool_result_tokens_estimated",
             "other_input_tokens_estimated")
    check(all(isinstance(c[k], int) for k in parts), "every component is an integer")
    check(sum(c[k] for k in parts) == c["input_tokens_estimated_total"],
          f"components sum to the reported total "
          f"({sum(c[k] for k in parts)} == {c['input_tokens_estimated_total']})")
    check(c["schema_tokens_estimated"] > 0, "tool DEFINITIONS are counted")
    check(c["tool_result_tokens_estimated"] > 0, "tool RESULTS are counted")
    check(c["conversation_history_tokens_estimated"] > 0, "history is counted")
    # The disjointness that matters: a 4000-char tool result must not also inflate
    # history, or an overflow gets blamed on conversation growth.
    check(c["tool_result_tokens_estimated"] > c["conversation_history_tokens_estimated"],
          "a large tool result lands in tool_result, NOT in history")
    check(c["token_estimate_exactness"] == "estimated",
          "the record admits these are estimates, not exact counts")
    check(c["token_estimate_method"] == "chars_div_4",
          "the estimation method is named so components stay comparable")

    print("\nCONTEXT BUDGET: DECLARED IS NOT CLAIMED AS EFFECTIVE")
    b = ns["_context_budget"](None, data, c)
    check(b["effective_backend_context_tokens"] is None,
          "effective backend context is NULL, not the declared number")
    check(b["effective_backend_context_availability"] == "not_measured_by_this_hook",
          "and the null carries an explicit reason")
    check(b["requested_output_tokens"] == 4096, "the output reserve is recorded")

    b2 = ns["_context_budget"](None, dict(data, litellm_params={
        "model_info": {"max_input_tokens": 98304}}), c)
    check(b2["declared_context_tokens"] == 98304,
          f"declared context is read from model metadata ({b2['declared_context_tokens']})")
    expected = 98304 - c["input_tokens_estimated_total"] - 4096
    check(b2["context_headroom_tokens"] == expected,
          f"headroom subtracts BOTH input and the output reserve ({expected})")

    print("\nREDACTION: SECRET- AND PROMPT-SHAPED VALUES NEVER SERIALIZE")
    hostile = {
        "tools": [{"name": "Read", "description": SECRETS[0] + PROMPT_MARKERS[1]}],
        "system": "You are a helpful assistant. " + SECRETS[2] + PROMPT_MARKERS[0],
        "messages": [
            {"role": "user", "content": PROMPT_MARKERS[2] + " " + SECRETS[1]},
            {"role": "user", "content": [
                {"type": "tool_result", "content": PROMPT_MARKERS[1] + SECRETS[3]}]},
        ],
        "litellm_params": {"api_base": f"http://{SECRETS[2]}@ollama:11434"},
        "max_tokens": 100,
    }
    comps = ns["_token_components"](hostile)
    budget = ns["_context_budget"](None, hostile, comps)
    record = {**comps, **budget, "upstream_host": ns["_upstream_host"](hostile)}
    blob = json.dumps(record)
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

    completion_evidence_checks()
    schema_and_dialect_checks()
    context_metadata_checks()
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
