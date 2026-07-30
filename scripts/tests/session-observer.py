#!/usr/bin/env python3
"""test-session-observer.py — tests for config/litellm/session_observer.py.

The payload shapes here are taken from a REAL captured session (Claude Code
against ailocal-architecture on /v1/messages, ledger 63ebd9b636ca917f): five
tool calls in the order Read, Edit, Read, Bash, Bash with three errored results,
and a first user message whose text began with the client's injected
<system-reminder> block carrying the whole of the user's global AGENTS.md. That
last detail is why _strip_injected exists, and it is pinned here.

Run: python3 scripts/tests/session-observer.py   (stdlib only; exit 1 on failure)
"""

import importlib.util
import os
import sys
import types

try:
    from litellm.integrations.custom_logger import CustomLogger  # noqa: F401
except ImportError:
    _clog = types.ModuleType("litellm.integrations.custom_logger")
    class _CustomLogger:
        def __init__(self, *a, **k): pass
    _clog.CustomLogger = _CustomLogger
    sys.modules["litellm"] = types.ModuleType("litellm")
    sys.modules["litellm.integrations"] = types.ModuleType("litellm.integrations")
    sys.modules["litellm.integrations.custom_logger"] = _clog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("AILOCAL_SESSION_LEDGER", None)      # never write during tests
MODULE = os.environ.get("AILOCAL_OBSERVER_MODULE",
                        os.path.join(ROOT, "config/litellm/session_observer.py"))
_spec = importlib.util.spec_from_file_location("session_observer", MODULE)
so = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(so)

fails = 0
def check(cond, name):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails += 1


# ── the real thing: injected scaffolding must not become "the ask" ───────────
REMINDER = ("<system-reminder>\nContents of AGENTS.md (user's private global "
            "instructions):\n# ailocal\nsecret-ish operating directives\n"
            "</system-reminder>")
ASK = "There is a bug in calc.py: add() subtracts instead of adding. Fix it."

print("\nINJECTED CONTEXT (measured failure: the ledger recorded AGENTS.md)")
check(so._strip_injected(REMINDER + "\n" + ASK) == ASK,
      "a leading <system-reminder> block is stripped, the ask survives")
check(so._strip_injected(ASK + "\n" + REMINDER) == ASK,
      "a trailing reminder block is stripped too")
check(so._strip_injected(REMINDER) == "",
      "a message that is ONLY scaffolding yields empty, not the scaffolding")
check("secret-ish" not in so._strip_injected(REMINDER + ASK),
      "injected instruction text never reaches the ledger")

anthropic = {
    "model": "ailocal-architecture",
    "messages": [
        {"role": "user", "content": [{"type": "text",
                                      "text": REMINDER + "\n" + ASK}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "calc.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "is_error": False}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"old": "a - b"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "2", "is_error": True}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "3", "is_error": True}]},
    ],
}

print("\n/v1/messages EXTRACTION")
req, calls, results = so.extract(anthropic)
check(req == ASK, "requested_change is the ask, not the injected reminder")
check([c["name"] for c in calls] == ["Read", "Edit", "Bash"],
      "tool_use blocks are extracted in order")
check(len(results) == 3 and sum(1 for r in results if r["error"]) == 2,
      "tool_result blocks counted, is_error respected")

print("\n/v1/chat/completions EXTRACTION")
openai = {"model": "m", "messages": [
    {"role": "user", "content": ASK},
    {"role": "assistant", "tool_calls": [
        {"function": {"name": "exec_command", "arguments": "{\"cmd\":\"ls\"}"}}]},
    {"role": "tool", "content": "output"},
]}
req_o, calls_o, res_o = so.extract(openai)
check(req_o == ASK and [c["name"] for c in calls_o] == ["exec_command"],
      "OpenAI tool_calls array is extracted")
check(res_o == [{"error": None}],
      "an OpenAI tool result has UNKNOWN status — no error flag exists to read")

print("\n/v1/responses EXTRACTION")
responses = {"model": "m", "input": [
    {"type": "message", "role": "user", "content": ASK},
    {"type": "function_call", "name": "apply_patch", "arguments": "{}"},
    {"type": "function_call_output", "output": "ok"},
    {"type": "function_call", "name": "exec_command", "arguments": "{}"},
    {"type": "function_call_output", "output": "Error: no such file"},
]}
req_r, calls_r, res_r = so.extract(responses)
check(req_r == ASK, "the flat input[] user message is the ask")
check([c["name"] for c in calls_r] == ["apply_patch", "exec_command"],
      "function_call items are extracted in order")
check([r["error"] for r in res_r] == [False, True],
      "function_call_output is inspected for an error string")

print("\nLEDGER SHAPE")
obs = so.SessionObserver()
led = obs.build(anthropic)
check(led["tool_calls_by_name"] == {"Read": 1, "Edit": 1, "Bash": 1},
      "per-tool counts are aggregated")
check(led["tool_call_sequence"] == ["Read", "Edit", "Bash"],
      "the call ORDER is retained (Edit-before-verify is a real pattern)")
check(led["tool_results_errored"] == 2, "errored results are counted")
check(led["verdict"] is None,
      "the proxy-side ledger reaches NO verdict — it cannot see the filesystem")
check("verify-session" in led["verdict_note"],
      "the ledger names where a verdict must come from")
check(led["session"] == obs.session_id(ASK, "ailocal-architecture"),
      "session id derives from the STRIPPED ask, so it is stable across turns")

# Arguments must not be stored: they carry file contents and command lines.
# Checked against the ledger MINUS requested_change — the ask legitimately
# mentions calc.py, and asserting over the whole record only proved that.
without_ask = {k: v for k, v in led.items() if k != "requested_change"}
blob = repr(without_ask)
check("calc.py" not in blob, "tool file paths are not stored verbatim")
check("pytest" not in blob, "tool command lines are not stored verbatim")
check(all(len(c["args"]) == 12 for c in so.extract(anthropic)[1]),
      "each argument set is reduced to a 12-char digest")

print("\nFAILURE ISOLATION")
check(so.extract({}) == ("", [], []), "an empty payload yields empty, not a crash")
check(so.extract({"messages": "not-a-list"}) == ("", [], []),
      "a malformed messages field does not raise")
check(so.SessionObserver().build({"model": None})["tool_calls_total"] == 0,
      "a payload with no history builds a zero ledger")

print()
if fails:
    print(f"SESSION OBSERVER TESTS: {fails} FAILED")
    sys.exit(1)
print("SESSION OBSERVER TESTS: OK")
