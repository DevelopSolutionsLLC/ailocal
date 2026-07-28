# ADR 003 — Server-side persona injection

**Status:** Accepted · **Date:** 2026-07

## Problem

Local models need far more behavioural steering than frontier models, and that
steering must apply identically to Claude Code, Codex and VS Code — none of
which share an instructions file.

## Alternatives considered

1. **Per-client instruction files.** Rejected: three copies to keep in sync, and
   nothing applies to a client added later.
2. **Bake instructions into a Modelfile.** Rejected: re-pulling/rebuilding a
   model to edit a sentence, and no per-capability variation.
3. **A LiteLLM pre-call hook.** Chosen.

## Decision

`config/litellm/persona_injector.py` merges `config/instructions/_core.md` +
`<capability>.md` into whatever system prompt the client sent — server-side, so
every alias and every client inherits it.

## Why

One interception point, applies to clients that do not exist yet, and edits are
a file change plus a restart.

## Tradeoffs

- Handles **both** request shapes: OpenAI (`system` lives in `messages[]`) and
  Anthropic `/v1/messages` (`system` is a **top-level field**) — the route
  Claude Code actually uses. Missing the second shape means silently no persona.
- Coupling: injection depends on model names resolving back to a capability key.
  The hook resolves the requested model through `model_group_alias` and uses that
  key to load the instruction file. **Any change to canonical names, aliases or
  routing layers must preserve this mapping or personas silently stop applying.**

## Measurements

- LiteLLM issue #27518 (hook bypassed on `/v1/messages`) was filed against
  **v1.83.10**. On the **1.92.0/1.93.0** image we run, the hook fires *and* its
  mutation reaches the backend on both routes — measured with a persona marker
  and a propagation probe, not assumed.
- Reasoners get **no** persona (DeepSeek's own guidance), temp 0.6 / top-p 0.95.
- `completion` and `embeddings` intentionally have no persona.

## Known limitations

- Personas are read at hook load, so editing a `.md` requires a proxy restart.
- Persona text is invisible in client-side logs; it only exists server-side.

## Revisit if

- LiteLLM is **downgraded** — re-verify #27518 before trusting the Anthropic
  route.
- Canonical model names or the alias layer change (see the coupling note).
