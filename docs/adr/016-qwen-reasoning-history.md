# ADR 016: Qwen tool-loop history keeps the thinking-mode signature

**Status:** Accepted · **Date:** 2026-10-29 · **Scope:** #38

## Context

With thinking enabled, Qwen3.5 4B/9B could finish a correct native tool loop with `done_reason: stop`, its final answer inside the open think block and no visible content. Clients showed an empty reply; delegation reported `DELEGATE_EMPTY`.

[REAL production inference; APPROX one captured Codex conversation, 2026-10-29] Every earlier assistant turn of the loop reached the model as an empty `<think>\n\n</think>` block, which is the non-thinking prompt signature. Replayed through the stock renderer, the three affected backends ended empty in 9 of 12 runs; restoring the turns' actual reasoning, or omitting the empty blocks, ended empty in none. The 9B wrote `DONE` as two thinking tokens and stopped. f16 KV, MTP on/off, sampling presets, closing-tag instructions and a cleaned template had not removed it.

Two mechanisms produced the empty blocks:

- Ollama's `Qwen35Renderer`, shared by `qwen3.5`, `qwen3.8` and `ornith`, writes a think block for every assistant turn after the last user query, empty or not, matching Qwen's reference template. Gemma 4 and every other installed renderer write one only when reasoning exists; Gemma completed the same replay without an empty final.
- Clients did not replay reasoning. LiteLLM 1.106.2's non-streaming Responses bridge writes a reasoning item as `content: [output_text]` without `summary`. Codex 0.165 requires `summary` and accepts only `reasoning_text`/`text` content, so it dropped the item. Codex requests that declare web search reach that bridge because LiteLLM de-streams them and ailocal's `tool_repair` re-streams the finished response.

## Decision

Fix both halves at the layer that owns each, and keep the thinking mode, the model and the client's content unchanged.

1. **Rendering (all clients).** A reviewed Ollama patch omits the think block of a completed assistant turn in the current tool loop when thinking is enabled and the turn carries no reasoning. Turns before the last user query, an assistant prefill and non-thinking prompts keep the reference rendering, so Qwen3.8/Ornith reference flows are unchanged. A turn whose reasoning was replayed keeps its block. `prepare()` runs the upstream renderer suite before it records the runtime; the three history regressions must execute even when optional upstream parity tests skip. Build tests use a temporary model store and a separate listener address to protect the operator's store and serving daemon.
2. **Replay (per client).** `tool_repair` gives each de-streamed reasoning item the `summary_text` form LiteLLM's streaming path already emits, with its content emptied. LiteLLM's input side reads that summary back as `reasoning_content`, which its Ollama adapter sends as `thinking`, so Codex's later turns carry their real reasoning.

Rendering an absent field differently is not a history rewrite: no message the client sent is dropped, reordered, summarised or invented, and reasoning never becomes visible content. `DELEGATE_EMPTY` stays an error for any residual empty turn; there is no automatic retry, fallback model or reasoning-to-answer promotion.

## Consequences

Thinking-mode Qwen tool loops produce a genuine final reply or a further tool call where they previously ended empty on the replay. Clients that replay reasoning get the reference rendering; clients that cannot are no longer shown the non-thinking signature. The patch must be re-applied and its regression re-run on any new Ollama base, and retires when upstream renders the case so or every client replays reasoning. The `tool_repair` normalisation retires when LiteLLM's non-streaming bridge emits `summary`.

## Alternatives

- **A decoder grammar forbidding end-of-generation inside thinking** (the earlier `qwen35-channel.patch`) forced a closing tag on the same replay, but treats the symptom, can extend thinking to the output cap and changes sampling for every Qwen3.5 thinking request. It is kept out unless a residual failure survives this fix.
- **Disabling thinking, retrying, or promoting reasoning into content** hides the defect and changes behaviour the caller selected.
- **Replay only** depends on every client and on two third-party hops; rendering alone leaves the reasoning-restored path unused where a client can replay it.

## Sources

- [Qwen3.5 chat template](https://huggingface.co/Qwen/Qwen3.5-9B) — think block on every post-query assistant turn
- [ollama/ollama#18916](https://github.com/ollama/ollama/issues/18916) — the same symptom on stock 4B, reproduced across parser, Jinja and MLX, all of which render the same empty blocks
- [vllm-project/vllm#55420](https://github.com/vllm-project/vllm/issues/55420), [QwenLM/Qwen3.8#216](https://github.com/QwenLM/Qwen3.8/issues/216) — a related long-reasoning end-of-sequence hazard, not shown to be this trigger
- Codex 0.165.0 `codex-rs/protocol/src/models.rs` — `ResponseItem::Reasoning`
