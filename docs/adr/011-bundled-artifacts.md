# ADR 011 — The artifact capability ships inside ailocal

**Status:** Accepted · **Date:** 2026-08

## Problem

Claude Code's hosted `Artifact` tool publishes to claude.ai and authenticates as the user's
Anthropic account. A `claude-local` session has neither: `isArtifactToolRegistered()` does not
register the tool under `ANTHROPIC_API_KEY` auth, so the capability is simply absent — not
degraded. A local session could not produce a diagram, a page, or a report.

The renderer was first built as a separate repository. That worked, and it is why this ADR
exists: it made the capability real enough to see what its packaging cost.

## Constraints

- `dependencies = []` — ailocal is standard-library only, and the renderer needs `mcp`.
- `~/.claude` is never touched; hosted `claude` keeps the real `Artifact` tool.
- `.claude.json` belongs to the user; github, grepai and anything else must survive.
- No local inference may be routed to Anthropic to obtain this.

## Alternatives considered

1. **Keep it external, document the clone.** Rejected: it makes the capability optional in
   practice. A user gets it only by cloning a second repository, creating a second venv,
   running a second installer, and keeping two trees in sync — four ways to end up with a
   `claude-local` that silently cannot draw.
2. **Add `mcp` to ailocal's dependencies.** Rejected: it breaks the standard-library-only
   invariant for a component ailocal never imports.
3. **Vendor the component and give it its own interpreter.** Chosen.

## Decision

The renderer ships under `resources/integrations/local-artifacts/`, alongside `deploy/` and
`clients/`. `ailocal clients claude` provisions a venv for it in the state root, installs
`mcp` into that venv, and **merges** one `artifact` key into `.claude.json`.

`mcp` is a subprocess dependency, not an ailocal dependency — the same relationship LiteLLM
already has as a container. Nothing in `ailocal/` imports it, so `dependencies = []` holds.

## Why merge rather than write

`.claude.json` is the user's file in ailocal's own config root, and other servers live there.
Rewriting it would make provisioning destructive in a way no other target is.

## Tradeoffs

- **5.2 MB of vendored assets** (elkjs, mermaid, marked) enter the wheel. They are the reason
  the rendered page needs no network at all, which is what makes `connect-src 'none'`
  enforceable rather than aspirational. Provenance and licences travel with them.
- **Provisioning now needs the network once**, to install `mcp`. It is content-addressed
  against `requirements.txt`, so a second run is a no-op and the gate's idempotency suite
  covers it.
- **`architecture` needs `node`.** Absent, provisioning warns and every other format still
  works; the capability degrades by one format rather than failing.

## Measurements

- `ailocal clients claude` on a clean machine: runtime provisioned, MCP registered, skill
  written, `github`/`grepai` preserved. [REAL]
- Second run: no writes. Gate idempotency suite passes. [REAL]
- End to end through `claude-local -p`: the model called `mcp__artifact__publish` and the
  canonical source landed in the project's `.artifacts/`. [REAL]

## Revisit if

- Claude Code registers `Artifact` for API-key auth, which would supersede all of this —
  delete the component, its provisioning, its suite and this ADR together.
- The vendored bundle grows past what a wheel should carry.

## Deeper reference

- `src/ailocal/resources/integrations/local-artifacts/PROVENANCE.md` — upstream, licences,
  and what was changed.
- `tests/artifacts.py` — how the capability is exercised in the gate.
