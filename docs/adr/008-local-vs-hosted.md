# ADR 008 — Local and hosted models side by side

**Status:** Accepted · **Date:** 2026-07

## Problem

Local models are private and free but slower and weaker. Hosted models are the
opposite. Both are wanted on the same machine, often in the same hour — and a
half-migrated setup where `claude` sometimes means local is worse than either.

## Alternatives considered

1. **Replace hosted entirely.** Rejected: local models cannot yet carry all work
   (see ADR 005 — `implementation` is measured non-agentic).
2. **Env vars in `.zshrc` to switch modes.** Rejected: a shell-global mode is
   invisible state; you find out which model you were talking to afterwards.
3. **XDG-isolated wrappers.** Chosen.

## Decision

`claude-local`, `codex-local` and `ailocal-code` are shell functions that inject
**process-scoped** env only. Everything lands in `~/.config/ailocal/`;
`~/.claude` and `~/.codex` are never touched.

`CLAUDE_CONFIG_DIR` / `CODEX_HOME` relocate the config root itself, so MCP
registrations, history, credentials and session state are genuinely per-root —
nothing leaks between local and cloud.

## Why

Plain `claude` in the same terminal stays on the cloud. There is no mode to
forget you are in: the command name *is* the mode.

## Tradeoffs

- Two config roots to install into (hence the generation pipelines in ADR 002
  and 007).
- Cadence must install into both roots, and ailocal's installer must not clobber
  what Cadence wrote there.

## What this means for every other decision here

**Hosted clients do not route through LiteLLM.** So none of the routing, persona
injection, tool gating or task classification applies to hosted Claude or hosted
Codex. Every behavioural change in this project affects local paths only.

Frontier models reaching the proxy would be `passthrough` — forwarded untouched,
and feature flags cannot override it. Verified: every model name currently
served is non-passthrough, including the `claude-*`/`gpt-*` compat names, which
alias onto local capabilities.

## Measurements

- Wrapper env is process-scoped: verified that plain `claude` is unaffected.
- `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` — local backends cap at `num_ctx` (64K
  here), so letting the client request the 1M-context beta just overflows.

## Known limitations

- The wrappers are zsh functions sourced from `.zshrc` between installer
  markers; a shell that does not source them has no local commands.
- `~/.claude` and `~/.codex` accumulate their own state independently (Codex's
  plugin marketplace cache regrows to ~77 MB in each root — disk only, never
  loaded into context).

## Revisit if

- Local models close the capability gap enough that hosted is rarely needed.
- A client stops honouring its config-root env var.
