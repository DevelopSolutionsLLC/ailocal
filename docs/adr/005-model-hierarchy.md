# ADR 005 — Five-tier model hierarchy

**Status:** Accepted · **Date:** 2026-07

## Problem

64 GB of unified memory, a handful of jobs with very different shapes (design,
daily coding, review, background summarisation, autocomplete), and one shared
GPU. One model cannot serve all of them well, and every resident model pins its
full KV cache.

## Constraints

- Memory is the binding constraint; paging is the known failure mode.
- Autocomplete is latency-bound; architecture is context-bound.
- Capability must be **verified from Ollama metadata**, not inferred from a
  model's name or release date.

## Decision

| # | Capability | Backend | ctx | keep_alive |
|---|---|---|---|---|
| 1 | `architecture` | qwen3-coder:30b-a3b-q4_K_M | 64K | `-1` resident |
| 2 | `implementation` | qwen2.5-coder:14b-instruct-q4_K_M | 16K | 20m |
| 3 | `review` | gpt-oss:20b | 16K | 20m |
| 4 | `fast` | qwen3.5:2b | 32K | 20m |
| 5 | `completion` | qwen2.5-coder:3b-instruct-q4_K_M | 4K | 20m |
| — | `embeddings` | nomic-embed-text | 8K | `-1` resident |

Picker order follows the profile's key order.

## Why these, measured

- **architecture** is MoE: 128 experts, 8 used, ~3B active params per token.
  That is why it beats a dense 27B at 197 vs 90 chunks/s despite being nominally
  larger. Prompt eval 971 tok/s cold on a 4,013-token prompt vs 353 for the
  dense 14b — and prompt eval is the mechanism behind first-byte timeouts.
- **implementation** is measured **non-agentic**: it does not sustain a
  multi-step tool loop. Same task, same prompt: it described an edit and emitted
  the call as a fenced JSON block (not a `tool_use`), file unchanged. The 30B
  did it in 3 turns. This is why `architecture` is the launch default.
- **review** is the only tier that reasons. It returns a `thinking` block **plus**
  a `text` block — reading `content[0].text` shows empty and looks like a dead
  tier.
- **fast** is Q8_0 2B at 114 tok/s gen / 1056 tok/s prompt, beating the 4B's
  78/549: fewer params beat lighter quantization *on speed*. Thinking is
  deliberately off — with it on the model emitted 19,056 reasoning characters and
  returned **empty content** at both max_tokens 8 and 64.
- **completion** is FIM-only. It is the only small model with both `insert` and
  `tools`, verified across six candidates.

## Tradeoffs

- Two resident models (~25 GB + 370 MB) permanently pinned.
- `completion` is a trap: any conversational turn routed there hard-400s.
  `sync-models.py` fails the build if a Claude slot points at it.

## Measurement discipline this cost us

An early prompt-eval figure of 1705 tok/s was **wrong**: it came from a best-of-2
loop that selected the cached run. Re-sending an identical prompt reports the
same `prompt_eval_count` with duration collapsing 4134 ms → 16.8 ms, because the
KV cache is reused and nothing is evaluated. **Any prompt-eval measurement must
use a cold, large prompt.**

## Known limitations

- No installed model emits `<think>` except `review`; there is no reasoning tier
  for architecture work.
- `reasoning_effort` reaches the backend but maps unreliably (BerriAI/litellm
  #15059 — `none` produced *more* reasoning than `high`). Per-role defaults are
  the control that works.

## Revisit if

- A model with both large context and reliable agentic tool use fits in budget.
- `implementation` is re-measured as agentic (retest before promoting it).
- Memory pressure changes — the `-1` pins are the first thing to reconsider.
