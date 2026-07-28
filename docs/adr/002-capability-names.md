# ADR 002 — Capability names, never model tags

**Status:** Accepted · **Date:** 2026-07

## Problem

Client configs that name `qwen3-coder:30b-a3b-q4_K_M` directly must all be
rewritten whenever a model changes, and they encode no intent — nothing says
*why* that model was chosen for that job.

## Constraints

- Swapping a backend must not touch a single client config.
- Clients hard-code names (`claude-opus-4-8`, `gpt-4o`) that cannot be removed.

## Alternatives considered

1. **Raw model tags everywhere.** Rejected: a model swap becomes a
   find-and-replace across four clients, and drift is invisible.
2. **A `local/*` namespace alongside capability names.** Rejected: two names for
   one thing is two things to keep in sync.
3. **One canonical `ailocal-<capability>` entry, everything else aliased.** Chosen.

## Decision

Each capability is exactly one `model_list` entry named `ailocal-<capability>`.
Client compat names are aliased onto those groups via `model_group_alias`,
generated from `config/clients.yaml`. Two source files drive everything:
`config/profiles/<tier>.yaml` (what a capability *is*) and `clients.yaml` (which
capability each client surface uses).

## Why

Capabilities decouple configs from models and carry intent. The router owns
context, sampling and residency, so those cannot drift per client.

## Tradeoffs

- An extra indirection when debugging: "which model actually served this?" needs
  `request_trace`, not the request body.
- Generated regions must never be hand-edited. This was violated once and caused
  the worst outage of the project — see Measurements.

## Measurements

- `configure.zsh` was hand-maintained while `clients.yaml` claimed to be the
  source of truth. It drifted: `ANTHROPIC_DEFAULT_HAIKU_MODEL` pointed at
  `ailocal-completion` (FIM, 4096 ctx), so every Claude Code background call and
  every haiku-slot subagent hard-400'd — measured `Max=4096, Got=10813`. After
  generating the block from `clients.yaml`, the same request succeeds at 11,510
  input tokens.
- `ANTHROPIC_DEFAULT_FABLE_MODEL` was absent entirely, so the review tier was
  unreachable from the Fable slot. The installed `claude` binary does support it
  (verified in its own strings).

## Known limitations

- The `/model` picker order is the key order of the profile file. That is
  implicit; changing order means reordering YAML blocks.
- Two slots pointing at one capability lists it twice in the picker. `sync`
  warns rather than fails, because a deliberate collapse is legal.

## Revisit if

- A client stops honouring `ANTHROPIC_DEFAULT_*_MODEL` (re-check against the
  binary's strings, which is how the Fable support was confirmed).
- More than one profile tier becomes active on a machine.
