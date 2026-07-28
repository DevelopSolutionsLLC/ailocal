# ADR 013 — Where latency actually comes from

**Status:** Accepted · **Date:** 2026-07-28 · **Measured, not modelled**

## Problem

`request_trace` showed TTFB up to ~39 s. Before optimising anything, decompose
it: proxy overhead, hook overhead, model load, prompt evaluation, or generation.

## Method

Each layer measured in isolation against the production stack — direct Ollama vs
through LiteLLM, cold vs warm, and prompt size swept.

## Findings

**1. The proxy is not the problem.** Same request, warm:

| path | latency |
|---|---|
| direct Ollama | 0.29 s |
| through LiteLLM (all hooks) | 0.36 s |

≈ **70 ms** for routing, persona injection, tool gateway, tool repair and
tracing combined. The gateway's own metric reports 0.2–0.7 ms.

**2. Model load is small, and only on a cold model.**
qwen3.5:2b — 2.58 s cold, 0.20 s warm. qwen3-coder:30b — 3.92 s cold, 0.10 s
warm. Resident models (`keep_alive: -1`) never pay it.

**3. Prompt evaluation is the whole story, and it is SUPERLINEAR.**
qwen3-coder:30b-a3b:

| prompt tokens | prompt eval | throughput |
|---|---|---|
| 694 | 0.70 s | 989 tok/s |
| 5,512 | 5.79 s | 952 tok/s |
| 16,512 | 27.58 s | 599 tok/s |
| 33,012 | 84.66 s | 390 tok/s |

Throughput **degrades** as the prompt grows — 989 → 390 tok/s — so doubling the
prompt more than doubles the wait. A ~16–20K-token first request lands squarely
in the observed 27–39 s band.

**4. KV cache makes it a FIRST-TURN cost.** Re-sending an identical prompt:

```
run 1: prompt_eval_count=16512  prompt_eval=0.16s
run 2: prompt_eval_count=16512  prompt_eval=0.03s
```

The count is unchanged but the time collapses: the prefix is cached and nothing
is re-evaluated. In a real agent loop each turn appends, so later turns only pay
for new tokens.

## Verdict

**Expected behaviour for a 30B on this hardware at this prompt size** — not a
cold-start bug, not a configuration fault, not a model defect. The controllable
variable is **prompt size**, which is exactly what the tool gateway (ADR 004)
attacks. Removing the mcpls bridge cut ~11.8 KB (~3K tokens) of tool schema;
at ~600 tok/s that is roughly 5 s off first-turn TTFB.

## Consequences

- Cutting tool/context bytes buys latency **more than proportionally** at large
  prompts. That is the lever; model choice is not.
- Do not chase TTFB on a warm repeat — the KV cache makes it meaningless. Any
  prompt-eval measurement must use a **cold, large, unique** prompt. (An earlier
  1705 tok/s figure in the profile was wrong for exactly this reason.)
- `keep_alive: -1` on architecture/embeddings is justified: it removes the
  3.9 s load, though that was never the dominant term.

## Revisit if

- A model with faster prompt eval at 16K+ fits the memory budget.
- Ollama ships prefix-cache reuse across *different* requests (today the win
  only applies to a growing conversation).
- Measured TTFB stops tracking prompt size — that would mean a new bottleneck.
