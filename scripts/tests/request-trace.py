#!/usr/bin/env python3
"""test-request-trace.py — E1 trace schema, redaction and component reconciliation.

Two properties matter and neither is provable by reading the code:

  1. REDACTION. The hook reads prompts, system text, tool definitions and tool
     results in order to MEASURE them. Every one of those is a place a secret or a
     source file can enter a log. So this test pushes secret-shaped and
     prompt-shaped values through the real functions and asserts they are absent
     from the serialized record.

  2. RECONCILIATION. Token components are only useful if they are disjoint and sum
     to the reported total. If tool definitions were counted both as `schema` and
     again as `history`, an overflow investigation would blame the wrong component —
     which is exactly the mistake the stale 24,448-token figure caused.

Imports the module's pure helpers directly, so no proxy, no Ollama and no litellm
package are required.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TRACE = REPO / "config" / "litellm" / "request_trace.py"

failures: list[str] = []


def check(cond: object, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        failures.append(label)


def load_helpers():
    """Execute only the module-level helpers, skipping the litellm import.

    The module imports `litellm.integrations.custom_logger`, which exists only in the
    proxy image. Rather than skip the whole suite on the host, the pure helper
    definitions are extracted and exec'd — the functions under test are
    self-contained by design.
    """
    src = TRACE.read_text()
    start = src.index("# ── E1: schema version, process generation, token components")
    end = src.index("def emit(record):")   # helpers end where emit begins
    ns: dict = {}
    exec("import json, os, time\n" + src[start:end], ns)
    return ns


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


def main() -> int:
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

    print()
    if failures:
        print(f"REQUEST TRACE: {len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("REQUEST TRACE: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
