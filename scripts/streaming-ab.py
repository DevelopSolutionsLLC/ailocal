#!/usr/bin/env python3
"""streaming-ab.py — does declaring a web_search tool destroy streaming?

A CONTROLLED EXPERIMENT, not a bug hunt. Two requests differing in ONE variable:
whether the client declares a `web_search` tool. Everything else — route, model,
prompt, stream flag — is held identical.

WHY THIS EXPERIMENT EXISTS
Trace analysis showed Codex requests delivering SSE events at physically
impossible rates: 708 events in 51.9 ms after a 97.8-second wait (13,642
events/sec). A local 30B cannot generate 708 tokens in 51 ms, so the response was
fully generated and then flushed. LiteLLM's websearch_interception handler
contains an explicit `stream=True -> stream=False` conversion, which would
produce exactly that signature.

But the debug line that would prove it fired is behind verbose logging, so the
mechanism was UNPROVEN. This measures the effect directly instead.

WHAT IS MEASURED, PER REQUEST
  first_event_ms          wall-clock to the first SSE event
  first_visible_text_ms   to the first event carrying assistant TEXT — the number
                          that corresponds to "the UI started moving"
  event_gap_max_ms        longest silence between events — what "frozen" means
  events_per_second       over the emission window
  impossible_flush        >200 events/sec, i.e. faster than any local model
                          could generate; the signature of a buffered replay

Run against BOTH dialects, because a difference between them is itself evidence:
Claude Code's WebSearch carries an input_schema and is refused by interception,
while Codex sends a bare {"type": "web_search"} which is accepted.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request

BASE = os.environ.get("AILOCAL_BASE", "http://127.0.0.1:4000")
KEY = os.environ.get("LITELLM_MASTER_KEY") or ""

PROMPT = ("List three considerations when designing a configuration file format. "
          "Answer in prose, about 150 words.")


def _key() -> str:
    if KEY:
        return KEY
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        for line in open(env, encoding="utf-8"):
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def stream(path: str, payload: dict, timeout: float = 300.0) -> dict:
    """Issue one streaming request and time every event as it ARRIVES.

    Timing is taken on the client side of the socket on purpose: that is where a
    user's "it looks frozen" actually lives. Proxy-side numbers cannot see it.
    """
    req = urllib.request.Request(
        f"{BASE}{path}", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_key()}",
                 "Accept": "text/event-stream"})
    t0 = time.time()
    stamps: list[float] = []
    first_text: float | None = None
    n_text = 0
    error = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                now = time.time()
                stamps.append(now)
                if first_text is None:
                    try:
                        evt = json.loads(body)
                    except ValueError:
                        continue
                    if _carries_text(evt):
                        first_text = now
                        n_text += 1
    except Exception as exc:  # noqa: BLE001
        error = repr(exc)

    total = (time.time() - t0) * 1000
    if not stamps:
        return {"events": 0, "error": error, "total_ms": round(total, 1)}

    first = (stamps[0] - t0) * 1000
    window = (stamps[-1] - stamps[0]) * 1000
    gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])] or [0.0]
    eps = (len(stamps) / (window / 1000)) if window > 1 else float(len(stamps))
    return {
        "events": len(stamps),
        "first_event_ms": round(first, 1),
        "first_visible_text_ms": round((first_text - t0) * 1000, 1) if first_text else None,
        "emission_window_ms": round(window, 1),
        "event_gap_max_ms": round(max(gaps), 1),
        "events_per_second": round(eps, 1),
        # A local model cannot emit 200 tokens/sec through this stack. Above that,
        # the events were produced earlier and replayed.
        "impossible_flush": eps > 200,
        "total_ms": round(total, 1),
        "error": error,
    }


def _carries_text(evt: dict) -> bool:
    """Does this event put visible assistant text on screen?

    Both dialects, because the whole question is whether the UI moved:
      Responses   response.output_text.delta
      Anthropic   content_block_delta with a text_delta
    """
    t = evt.get("type") or ""
    if t.endswith("output_text.delta") and evt.get("delta"):
        return True
    if t == "content_block_delta":
        return bool((evt.get("delta") or {}).get("text"))
    if evt.get("choices"):
        return bool((evt["choices"][0].get("delta") or {}).get("content"))
    return False


# ── the two arms ────────────────────────────────────────────────────────────

def responses_payload(model: str, with_search: bool) -> dict:
    p: dict = {"model": model, "input": PROMPT, "stream": True, "max_output_tokens": 400}
    if with_search:
        # Codex's shape: a bare typed tool with no schema. Interception ACCEPTS
        # this one.
        p["tools"] = [{"type": "web_search"}]
    return p


def messages_payload(model: str, with_search: bool) -> dict:
    p: dict = {"model": model, "max_tokens": 400, "stream": True,
               "messages": [{"role": "user", "content": PROMPT}]}
    if with_search:
        # Claude Code's shape: carries an input_schema, which interception is
        # documented to REFUSE so the client's own handler keeps working.
        p["tools"] = [{"name": "WebSearch", "description": "Search the web",
                       "input_schema": {"type": "object",
                                        "properties": {"query": {"type": "string"}},
                                        "required": ["query"]}}]
    return p


def show(label: str, r: dict) -> None:
    if r.get("error") and not r.get("events"):
        print(f"  {label:34s} ERROR {r['error'][:70]}")
        return
    flag = "  <-- IMPOSSIBLE FLUSH" if r.get("impossible_flush") else ""
    print(f"  {label:34s} events={r['events']:>5}  first_event={r['first_event_ms']:>8.0f}ms  "
          f"first_text={str(r['first_visible_text_ms'] or '-'):>9}  "
          f"max_gap={r['event_gap_max_ms']:>8.0f}ms  eps={r['events_per_second']:>8.1f}{flag}")


def main() -> int:
    model = os.environ.get("AB_MODEL", "gpt-5.2-codex")
    anth_model = os.environ.get("AB_ANTH_MODEL", "claude-sonnet-4-5")
    repeats = int(os.environ.get("AB_REPEATS", "2"))

    print("STREAMING A/B — does declaring web_search destroy streaming?")
    print(f"  base={BASE}  responses_model={model}  messages_model={anth_model}  n={repeats}\n")
    print("  ONE variable differs between arms: whether a web_search tool is declared.\n")

    results: dict[str, list[dict]] = {}
    for dialect, path, builder, m in (
            ("responses", "/v1/responses", responses_payload, model),
            ("messages", "/v1/messages", messages_payload, anth_model)):
        for with_search in (False, True):
            label = f"{dialect}  search={'ON ' if with_search else 'OFF'}"
            runs = []
            for _ in range(repeats):
                r = stream(path, builder(m, with_search))
                runs.append(r)
                show(label, r)
            results[label] = runs
        print()

    print("  SUMMARY (median across runs)")
    summary = {}
    for label, runs in results.items():
        ok = [r for r in runs if r.get("events")]
        if not ok:
            print(f"    {label:34s} no successful runs")
            summary[label] = None
            continue
        med = {k: statistics.median([r[k] for r in ok if r.get(k) is not None])
               for k in ("first_event_ms", "event_gap_max_ms", "events_per_second")
               if any(r.get(k) is not None for r in ok)}
        flushes = sum(1 for r in ok if r.get("impossible_flush"))
        summary[label] = {**med, "impossible_flushes": f"{flushes}/{len(ok)}"}
        print(f"    {label:34s} first_event={med.get('first_event_ms', 0):>8.0f}ms  "
              f"eps={med.get('events_per_second', 0):>8.1f}  "
              f"impossible={flushes}/{len(ok)}")

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "captures", "streaming-ab-isolated.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"model": model, "anthropic_model": anth_model,
                   "runs": results, "summary": summary}, fh, indent=2)
    print(f"\n  written: {out}")

    print("\n  READING THIS")
    print("    If search=ON shows an impossible flush and search=OFF does not, the")
    print("    web_search declaration is CAUSAL. If both flush, the cause is elsewhere")
    print("    and websearch_interception is not the explanation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
