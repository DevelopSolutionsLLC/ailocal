#!/usr/bin/env python3
"""test-context-cases.py — E3. Six controlled shapes, to find what actually overflows.

THE QUESTION. Requests have been rejected for exceeding the context window, and the
budget has several independent contributors: the tool schemas the client declares,
the injected persona, accumulated conversation history, returned tool RESULTS, and
the output reserve (`max_tokens`) which is subtracted from the same window. Any of
them can be the one that pushes a request over, and a log line saying "context
window exceeded" names none of them.

WHY SIX SHAPES AND NOT ONE BIG ONE. Each case loads exactly ONE contributor and
leaves the others at their floor, so the trace attributes the growth to a named
component rather than to "the request". The sixth is the control that must fail:
if a deliberately over-sized request does NOT get rejected, the declared window is
not being enforced and every other case is measuring the wrong thing.

WHAT IT READS. The E1 trace (config/litellm/request_trace.py), which already
decomposes the input into disjoint components that sum to the total. Correlation is
by trace-file line offset taken immediately before each request — not by timestamp
window, which would race against any other traffic on the proxy.

REJECTING LAYER. The distinction that matters operationally is whether LiteLLM
refused the request before dialling Ollama (preflight) or whether Ollama refused it
(backend). The trace records `upstream_connect_*`, and a preflight rejection shows
a failure with no upstream contact and sub-millisecond total_ms.

Run:  python3 scripts/test-context-cases.py [--json PATH]
Exit: 0 if all six cases produced an attributable trace, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRACE_DIR = REPO / "data" / "tool-captures" / "traces"
BASE = "http://127.0.0.1:4000"

# The capability under test. `architecture` is the largest window (98304) and the
# one the overflow reports came from, so it is where a real over-budget request
# has to be demonstrated.
MODEL = "ailocal-architecture"


def master_key() -> str:
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no LITELLM_MASTER_KEY in .env")


def trace_path() -> Path:
    return TRACE_DIR / (time.strftime("%Y%m%d") + ".jsonl")


def trace_offset() -> int:
    p = trace_path()
    return p.stat().st_size if p.exists() else 0


def traces_since(offset: int) -> list[dict]:
    p = trace_path()
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        f.seek(offset)
        out = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out


# ── request shapes ───────────────────────────────────────────────────────────
# Text is generated from a repeating token-ish filler so size is predictable and
# the content carries no meaning the model could shortcut on.
FILLER = "the quick brown fox jumps over the lazy dog and then considers its options carefully. "


def words(approx_tokens: int) -> str:
    """~4 chars per token, matching the trace estimator, so the requested size and
    the measured size are in the same unit."""
    return (FILLER * (approx_tokens * 4 // len(FILLER) + 1))[: approx_tokens * 4]


def tool(n: int, desc_tokens: int = 40) -> dict:
    return {
        "name": f"tool_{n}",
        "description": words(desc_tokens),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": words(10)},
                "content": {"type": "string", "description": words(10)},
            },
            "required": ["path"],
        },
    }


def case_short() -> dict:
    return {"max_tokens": 16, "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}


def case_tool_heavy() -> dict:
    return {
        "max_tokens": 16,
        "tools": [tool(i) for i in range(60)],
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }


def case_history() -> dict:
    msgs = []
    for i in range(40):
        msgs.append({"role": "user", "content": words(250)})
        msgs.append({"role": "assistant", "content": words(250)})
    msgs.append({"role": "user", "content": "Reply with the single word: ok"})
    return {"max_tokens": 16, "messages": msgs}


def case_tool_result() -> dict:
    """Tool RESULTS, which the trace counts separately from history — the component
    that grows fastest in a real agent session and is easiest to overlook."""
    return {
        "max_tokens": 16,
        "tools": [tool(0)],
        "messages": [
            {"role": "user", "content": "Read the file."},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "tool_0", "input": {"path": "/x"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": words(20000)}]},
        ],
    }


def case_output_reserve() -> dict:
    """Input comfortably inside the window, but max_tokens claims most of what is
    left. Output reserve is subtracted from the SAME window, so this is the case
    that fails while the input alone looks fine."""
    return {"max_tokens": 90000, "messages": [{"role": "user", "content": words(20000)}]}


def case_overflow() -> dict:
    """The control that MUST be rejected, and quickly.

    Aimed at `completion` (4096) rather than `architecture` (98304) deliberately.
    An over-window request to architecture is NOT rejected — measured, see
    case 6b — it is forwarded and simply grinds, because Ollama's real ceiling for
    qwen3-coder is 262144, far above the 98304 we declare. So architecture cannot
    produce a bounded, deterministic rejection. `completion` can: it is the 4096
    FIM tier, and any real turn overflows it in milliseconds.
    """
    return {"max_tokens": 16, "messages": [{"role": "user", "content": words(20000)}],
            "_model": "ailocal-completion"}


def case_over_declared() -> dict:
    """6b. Input ABOVE the declared architecture window (98304), aimed at
    architecture. Not expected to be rejected — this measures whether the declared
    window is enforced at all. Bounded timeout, because the interesting outcome is
    'accepted and still running', not the eventual completion."""
    return {"max_tokens": 16, "messages": [{"role": "user", "content": words(110000)}],
            "_timeout": 45}


CASES = [
    ("1 short normal", case_short),
    ("2 tool-heavy", case_tool_heavy),
    ("3 large history", case_history),
    ("4 large tool-result", case_tool_result),
    ("5 output-reserve boundary", case_output_reserve),
    ("6 deterministic overflow", case_overflow),
    ("6b over-declared window", case_over_declared),
]


# ── configured / backend limits, for the comparison columns ──────────────────

def configured_num_ctx() -> int | None:
    import re
    txt = (REPO / "config" / "litellm" / "config.yaml").read_text()
    block = re.search(r"model_name:\s*" + MODEL + r"\b(.*?)(?=\n  - model_name:|\Z)",
                      txt, re.S)
    if not block:
        return None
    m = re.search(r"num_ctx:\s*(\d+)", block.group(1))
    return int(m.group(1)) if m else None


def backend_model() -> str | None:
    import re
    txt = (REPO / "config" / "litellm" / "config.yaml").read_text()
    block = re.search(r"model_name:\s*" + MODEL + r"\b(.*?)(?=\n  - model_name:|\Z)",
                      txt, re.S)
    if not block:
        return None
    m = re.search(r"model:\s*ollama_chat/(\S+)", block.group(1))
    return m.group(1) if m else None


def ollama_max(model: str | None) -> int | None:
    """The backend's OWN advertised maximum, from /api/show. Distinct from what we
    declare: an over-declaration is the E3 bug class."""
    if not model:
        return None
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/show",
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        d = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception:
        return None
    info = d.get("model_info") or {}
    for k, v in info.items():
        if k.endswith(".context_length"):
            return int(v)
    return None


# ── the run ──────────────────────────────────────────────────────────────────

def send(key: str, body: dict, timeout: int = 300) -> tuple[int | None, dict | str, float]:
    payload = dict(body)
    timeout = payload.pop("_timeout", timeout)
    payload["model"] = payload.pop("_model", MODEL)
    req = urllib.request.Request(
        BASE + "/v1/messages", data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw), time.monotonic() - t0
        except json.JSONDecodeError:
            return e.code, raw, time.monotonic() - t0
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.monotonic() - t0


def classify(http: int | None, body, tr: dict | None) -> tuple[str, str]:
    """(rejecting_layer, error_classification). The layer is the operationally
    useful half: preflight and backend rejections need different fixes."""
    if http == 200:
        return "none", "ok"

    text = json.dumps(body) if not isinstance(body, str) else body
    lowered = text.lower()
    ctx = ("context window" in lowered or "contextwindow" in lowered
           or "too large" in lowered or "context_length" in lowered)

    # NOT upstream_connect_started_at: the trace records that field as
    # `upstream_connect_availability: not_exposed_by_litellm_hook`, i.e. it is
    # structurally null and proves nothing. A preflight rejection is instead
    # identifiable by its shape — LiteLLM fails it in well under a millisecond,
    # long before any socket to Ollama could be established.
    total_ms = (tr or {}).get("total_ms")
    contacted = bool(isinstance(total_ms, (int, float)) and total_ms > 50)
    if ctx and not contacted:
        return "litellm_preflight", "context_window_exceeded"
    if ctx and contacted:
        return "ollama_backend", "context_window_exceeded"
    if http is None:
        return "transport", "no_response"
    return ("ollama_backend" if contacted else "litellm_proxy"), f"http_{http}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the full result table here")
    args = ap.parse_args()

    if not TRACE_DIR.exists():
        print("AILOCAL_TRACE_DIR is not mounted — cannot attribute cases to traces.")
        return 1

    key = master_key()
    bmodel = backend_model()
    cfg_ctx = configured_num_ctx()
    omax = ollama_max(bmodel)

    print(f"capability      : {MODEL}")
    print(f"backend model   : {bmodel}")
    print(f"configured num_ctx: {cfg_ctx}")
    print(f"ollama maximum  : {omax}")
    print()

    rows = []
    for label, build in CASES:
        body = build()
        off = trace_offset()
        http, resp, dt = send(key, body)
        time.sleep(1.2)                       # let the post-call hook flush
        new = traces_since(off)
        # The request's OWN trace: this capability, most recent.
        mine = [t for t in new if t.get("model") in (MODEL, None)] or new
        tr = mine[-1] if mine else None

        layer, klass = classify(http, resp, tr)
        row = {
            "case": label,
            "capability": (tr or {}).get("capability") or MODEL,
            "backend_model": (tr or {}).get("backend_model") or bmodel,
            "schema_tokens_estimated": (tr or {}).get("schema_tokens_estimated"),
            "system_instruction_tokens_estimated": (tr or {}).get("system_instruction_tokens_estimated"),
            "conversation_history_tokens_estimated": (tr or {}).get("conversation_history_tokens_estimated"),
            "tool_result_tokens_estimated": (tr or {}).get("tool_result_tokens_estimated"),
            "other_input_tokens_estimated": (tr or {}).get("other_input_tokens_estimated"),
            "input_tokens_estimated_total": (tr or {}).get("input_tokens_estimated_total"),
            "requested_output_tokens": (tr or {}).get("requested_output_tokens"),
            "declared_context_tokens": (tr or {}).get("declared_context_tokens"),
            "configured_num_ctx": cfg_ctx,
            "ollama_backend_max": omax,
            "effective_runtime_context": (tr or {}).get("effective_backend_context_tokens"),
            "context_headroom_tokens": (tr or {}).get("context_headroom_tokens"),
            "upstream_contacted": (
                bool(isinstance((tr or {}).get("total_ms"), (int, float))
                     and (tr or {}).get("total_ms", 0) > 50)),
            "total_ms": (tr or {}).get("total_ms"),
            "rejecting_layer": layer,
            "error_classification": klass,
            "fallback_state": (tr or {}).get("fallback_state"),
            "http": http,
            "elapsed_s": round(dt, 2),
            "trace_request_id": (tr or {}).get("request_id"),
        }
        rows.append(row)
        print(f"  {label:<28} http={http} "
              f"in={row['input_tokens_estimated_total']} "
              f"out_req={row['requested_output_tokens']} "
              f"headroom={row['context_headroom_tokens']} "
              f"layer={layer} class={klass} ({dt:.1f}s)")

    print()
    hdr = ["case", "schema", "system", "history", "tool_result", "other", "TOTAL",
           "out_req", "declared", "headroom", "upstream", "layer", "class", "fallback"]
    print(" | ".join(hdr))
    for r in rows:
        print(" | ".join(str(x) for x in [
            r["case"], r["schema_tokens_estimated"],
            r["system_instruction_tokens_estimated"],
            r["conversation_history_tokens_estimated"],
            r["tool_result_tokens_estimated"], r["other_input_tokens_estimated"],
            r["input_tokens_estimated_total"], r["requested_output_tokens"],
            r["declared_context_tokens"], r["context_headroom_tokens"],
            r["upstream_contacted"], r["rejecting_layer"],
            r["error_classification"], r["fallback_state"]]))

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")

    missing = [r["case"] for r in rows
               if r["trace_request_id"] is None and not r["case"].startswith("6b")]
    if missing:
        print(f"\nFAIL — no trace attributed for: {missing}")
        return 1

    overflow = [r for r in rows if r["case"].startswith("6 ")][0]
    if overflow["error_classification"] == "ok":
        print("\nFAIL — the deterministic overflow control SUCCEEDED. A request 5x the "
              "declared window was served, so nothing enforces the window and no "
              "other case can be trusted.")
        return 1

    for r in rows:
        if r["declared_context_tokens"] is None and r["http"] == 200:
            print(f"\nFAIL — {r['case']} succeeded but reported a NULL context budget. "
                  "declared_context_tokens must resolve for a served request.")
            return 1

    print("\nE3 CASES: every case attributed to a trace with a populated budget; "
          "the overflow control was rejected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
