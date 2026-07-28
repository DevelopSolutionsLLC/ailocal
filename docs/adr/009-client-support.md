# ADR 009 — Per-client support levels

**Status:** Accepted · **Date:** 2026-07-28

## Problem

Five client surfaces with genuinely different capabilities. The temptation is to
force parity; the result would be duplicated functionality in some clients and
phantom features in others.

## Decision

**Do not force uniformity.** Share what can be shared (one MCP source of truth,
one capability registry, one persona layer), adapt interfaces per client, and
document the differences rather than paper over them.

See `docs/compatibility-matrix.md` for the current state of each surface.

## Per-client reasoning

**Claude Code (hosted)** — full capability, does not route through LiteLLM. It
DOES ship native LSP (confirmed 2026-07-28), so it uses that plus grepai. The
earlier caution here — "it may ship native LSP, so do not register a bridge" —
turned out to be correct, and the bridge was never added for it.

**Claude Code via LiteLLM (`claude-local`)** — the primary target and the only
surface where the whole stack is exercised. MCP (grepai + lsp), personas,
routing, tool gating and delegation all verified working end to end.

**Codex (hosted)** — grepai registered. Expected to work (no LiteLLM in path, so
namespace bundles survive) but **untested**: verifying it spends OpenAI credits.
Labelled as inference, not measurement.

**Codex local** — the one real capability gap. MCP servers are registered and
running, and the model still cannot call them: LiteLLM discards Codex's
`namespace`-typed tools, measured `bytes_prefiltered_by_litellm: 27239`.
Blocked upstream (openai/codex#20652; PR #17556 unreleased).

**VS Code** — grepai plus its own language extensions, **deliberately no bridge**: VS Code has native language
servers and a bridge would duplicate them. Model routing via the
`litellm-connector` extension. Instructions are layered global
(`~/.copilot/instructions/`, `applyTo: "**"`) plus repo `.github/`
— measured overlap 12 shared lines of 119/170, i.e. layering, not duplication.

## Tradeoffs

- Codex local is a second-class surface until upstream moves. Documenting that
  honestly is better than shipping tools the client will reject.
- Subagents exist only on Claude Code; Codex gets prompts and VS Code gets
  neither. That is the clients' shape, not a gap in this stack.

## Adding a new client

1. Add it to `cadence/config/mcp.yaml` `clients:` with its format and path.
2. Add a client profile to `registry.yaml` (`drop_groups` is intersected with the
   model class — an unknown client contributes an empty set, so it gets **no**
   filtering rather than maximal filtering; that is the safe default).
3. Add its surface to `config/clients.yaml` if it needs capability mapping.
4. Verify with a **real session** that tools arrive and are callable. Registration
   is not capability — codex-local is the proof.
5. Add a row to the compatibility matrix with a measured status.

## Revisit if

- Codex ships PR #17556 → re-run `scripts/validate-codex-e2e.sh`.
- Claude Code ships native LSP → drop the bridge for the hosted surface.
- A client's dialect changes how tools are declared.
