# ADR 004 — Tool gateway and task classification

**Status:** Accepted · **Date:** 2026-07 · **The decision that most shapes daily feel**

## Problem

Claude Code declares **61 tools / 104 KB / 24,448 real Qwen tokens** on every
`/v1/messages` request. Roughly 70% is orchestration, scheduling and worktree
machinery a local model cannot drive. Worse than the token cost: a coding
persona holding `Read`/`Glob`/`Grep`/`Bash` *investigates*, so a plain question
like "show me an example of hello world in C++" made the agent crawl the
repository before answering.

## Constraints

- Never strip a tool the model genuinely needs — a stranded agent is far worse
  than a few wasted schemas.
- No model/client literals in code; the negotiator must ask the registry.
- Must work across all three API dialects.

## Alternatives considered

1. **Send everything.** Rejected: measured behaviour above.
2. **A fixed per-model allowlist.** Rejected: cannot distinguish "explain this
   language feature" from "refactor this module" for the same model.
3. **Ask the model to self-select tools.** Rejected: an extra round trip on a
   slow local model, and it is the component least able to judge.
4. **Registry-driven groups + task classification.** Chosen.

## Decision

`tool_gateway.py` is a pre-call hook. `registry.yaml` is the sole source of
facts about models, clients, routes and tool groups. Removable groups are the
**intersection** of what the client will drop and what the model class denies —
so neither side can unilaterally strip what the other needs. A task classifier
then narrows further.

Mode is `filter` (set in `.env`; the compose default is `off`).

## Why

Groups express *what a tool is for*, which is what lets a model or task select
by need instead of by name. An unmatched participant contributes an empty set,
so an unknown client or model results in **no** filtering rather than maximal
filtering.

## Tradeoffs and the guards they forced

- **Fail-open:** an unclassified task keeps every tool.
- **`conversational` is the only class allowed below the `always` floor**
  (`override_always: true`), and only on a genuine first turn. Classification
  reads the *first* user message, which never changes as a session grows — so
  without that guard, a session opening with a chat question would stay
  tool-less forever and "now fix this file" could never touch anything.
- **`mention_overrides`:** classification matches on *topic* and is blind to
  instructions about *how to work*. Measured: "delegate the security analysis to
  the reviewer subagent" matched the `review` class on the word "security", and
  `review` shed delegation — the gateway removed the very tool the request was
  asking for. An explicitly named mechanism now outranks an inferred class.
- Registered **last** so `websearch_interception` still sees the client's
  `web_search` tool; it never drops an entry it cannot name.

## Measurements

- Conversational: **61 tools → 1 kept**, end to end through `claude -p`.
- Codex's real gain is **18%, not 71%** — LiteLLM already discards its
  `namespace` tools before the backend, so removing them is not a saving.
- Gateway overhead: ~0.2–0.7 ms per request.

## Known limitations

- Substring classification is crude. It reads topic, not intent, which is
  exactly why `mention_overrides` exists.
- `registry.yaml` is read once at `gateway_init`; edits need a restart
  (`start.sh` fingerprints it).

### OPEN: the conversational class does not hold across a multi-turn loop

**Verified on turn 1, not across the loop.** Measured for
"show me an example of hello world in c++" (9 gateway turns in one `claude -p`):

```
turn 1  class=conversational  kept  1/61   <- correct
turn 2  class=explore         kept 44/64
turn 3  class=conversational  kept  1/61
turns 5-9 class=None          kept 48/61   <- full toolset back
```

The model then called `rg`, `Bash` x3 and `Write`. So the headline "61 → 1" is
true of the first request and **not** of the whole session.

Two guard bugs were found and fixed while chasing this, both real and both now
unit-tested — neither was sufficient:

1. Releasing the override on the agent's own continuation turn (required zero
   assistant messages).
2. Counting `role: "user"` naively — on the Anthropic route **tool results come
   back as user messages** carrying `tool_result` blocks, so the override lapsed
   the instant any tool ran.

The remaining cause is that `first_user_text()` resolves to something different
on later turns (subagent context — note `tools_in` shifting 61 → 64 — and/or
compaction), so the classifier stops seeing the original question. Fixing that
means classification that survives context changes, which is a design change,
not a patch — deliberately **not** attempted during close-out.

The `conversational` task class asserts the model calls *no*
tools and therefore **fails today**. That is intentional: it is a real open
issue, and a baseline that hides it would be worse than one that reports it.

## Revisit if

- A local model becomes reliably agentic across the full 61-tool surface.
- Classification produces a stranded agent in practice — the fail-open and
  first-turn guards exist to prevent that, and a real occurrence invalidates them.

## Deeper reference

- `docs/architecture.md` — full architecture: registry, negotiation,
  verification, metrics, client profiles, flags, recovery.
- `docs/architecture.md` — phase-2 history and the token-calibration discipline
  behind the byte/token numbers quoted here.

This ADR records *why*; those record *how*. Do not restate them here.
