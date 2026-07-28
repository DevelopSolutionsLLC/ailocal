# ADR 006 — Subagent delegation

**Status:** Accepted · **Date:** 2026-07-28 · **The most-misdiagnosed area of the project**

## Problem

The repo ships five subagent definitions (planner, implementer, reviewer,
search, tester) and documentation telling agents to delegate. Delegation never
happened. Three separate investigations reached three wrong conclusions.

## The three wrong conclusions, in order

1. **"The local model won't reach for delegation — a model-capability limit."**
   Wrong. `Task*` was in the `orchestration` group, which every local model class
   denies, so with `AILOCAL_TOOL_GATEWAY=filter` the tool was stripped before the
   model saw it.
2. **"`claude -p` headless mode doesn't expose subagents at all."** Wrong, and
   this one looked airtight: a capture of the real payload showed 47 tools
   containing `TaskCreate`/`TaskGet`/`TaskList` but **no bare `Task`**, and
   passing `--agents` explicitly did not change it.
3. The actual answer: **Claude Code renamed the tool from `Task` to `Agent` in
   v2.1.63.** `Task` survives only as an alias in settings and agent
   definitions; the live `Task*` names are *background-task management*, a
   different feature. `Agent` was in that same payload all along — and this
   gateway was dropping it, because only `Task*` had been moved out of
   `orchestration`.

## Decision

`Agent` (plus `Task*` as alias/compat) lives in its own `delegation` group,
allowed for `local_agentic` and denied to the weaker tiers.

## Why

The token argument for denying orchestration never applied to delegation:
`Workflow` alone is **21,525 B**, `Agent` is ~1 KB. Grouping them traded the
entire delegation workflow for savings that came almost entirely from Workflow.
Only `architecture` gets it because it is the one tier measured able to sustain
a multi-step tool loop, so it is the only one that can usefully be a parent.

## Measurements

Verified end to end through `claude -p` against the proxy:

```
TOOLS CALLED: Read, Agent, TaskOutput, Read
MODELS USED:  ailocal-architecture  +  claude-fable-5
request_trace: ailocal-architecture -> architecture
               claude-fable-5       -> review
```

Parent on the 30B, `Agent` called, reviewer subagent ran on the review tier
(gpt-oss:20b), findings returned and were summarised.

## Tradeoffs

- Delegation costs a second full local inference. It is worth it to protect the
  parent's context, wasteful for a one-liner.
- `delegation` is deliberately absent from `simple_edit` and `conversational`.

## Known limitations

- **Only explicit delegation is tested.** Whether the model spontaneously
  delegates on a task that merely warrants it is unmeasured.
- Delegation is a capability, not a goal. Intended behaviour: simple question →
  answer directly; small edit → implementation only; large architectural change
  → architecture may delegate; risky change → implementation + review. Do not
  tune the system toward "always delegate".

## Revisit if

- Claude Code renames or restructures the tool again — **check the tool the
  client actually sends**, not the name the docs used two versions ago. That
  single rename cost two full investigations.
- Unprompted delegation is measured and turns out not to happen when warranted.
