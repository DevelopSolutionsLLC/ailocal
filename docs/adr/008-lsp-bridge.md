# ADR 008 — LSP via the mcpls bridge

**Status:** Accepted · **Date:** 2026-07 · **Owner:** Cadence

## Problem

Local models guess at symbols. Semantic search finds *concepts* well and exact
answers poorly — "where is X defined", "what calls X" need a language server.

## Alternatives considered

1. **Implement LSP protocol handling in Cadence.** Rejected: large surface, no
   value added over an existing bridge.
2. **Per-language MCP servers.** Rejected: N servers to configure and keep alive.
3. **mcpls (github.com/bug-ops/mcpls)**, one bridge spawning language servers
   and exposing LSP operations as MCP tools. Chosen.

## Decision

One `lsp` MCP server (mcpls) for `claude-local` and `codex-local`. Languages are
declared in `cadence/config/mcpls.toml`.

| Language | Server | Status |
|---|---|---|
| Python | pyright-langserver | verified answering |
| TypeScript / JavaScript | typescript-language-server | verified answering |
| Go | gopls 0.23.0 | verified in a real module |
| Bash / POSIX sh | bash-language-server 5.6.0 | verified answering |
| zsh | bash-language-server | navigation only |
| C / C++ | clangd | configured |

## Standing rule: declare only what is verified present

A phantom entry is **worse** than a missing one — mcpls advertises the tool, the
spawn fails, and the empty result reads as "no references exist". `rust-analyzer`
is absent and therefore not declared.

## THE routing limit (measured, undocumented upstream)

- **Document-scoped tools** (`get_definition`, `get_references`,
  `get_document_symbols`, `get_hover`) route **by file extension** and work for
  every configured language. Verified against `.py`, `.sh`, `.zsh`, `.mjs`, `.go`.
- **`workspace_symbol_search` does NOT fan out.** It goes to whichever server
  became ready *first*, so it answers for one language and returns
  `{"symbols":[]}` for the rest — byte-identical to "does not exist".

**Proof:** in a real Go module the multi-language config returned
`{"symbols":[]}` for a function that plainly exists, while a go-only config
resolved the same query on the first attempt. It also *races*: it resolved
Python consistently until a `jsconfig.json` was added to the cadence repo, after
which typescript won the startup race and the same query went empty.

Consequence: `verify-lsp.sh` asserts capability with document-scoped probes and
reports `workspace_symbol_search` **without gating on it** — failing a suite on a
coin flip trains everyone to ignore the suite.

## Other measured behaviours

- **Cold start is not absence.** mcpls reports two distinct not-ready states: a
  JSON-RPC *error* ("still initializing"), and a successful `{"symbols":[]}`
  while still indexing. The second is the dangerous one. A 24×5s retry budget was
  too short for a cold pyright over the cadence repo and produced a **false
  FAILED** against a config that demonstrably works.
- **gopls is module-scoped** — no `go.mod`, no symbols. That is the correct
  answer, not a fault; reported as a skip.
- **zsh has no language server anywhere.** bash-lsp parses it with the bash
  grammar so navigation works on the compatible subset, but it deliberately
  skips shellcheck because shellcheck does not support zsh (bash-lsp #689,
  #1064). A clean `.zsh` result means **nothing was linted**.
- `jsconfig.json` exists in cadence solely so tsserver has a real project instead
  of an inferred one; without it workspace-wide JS symbol search is empty.

## Measurements

Real agent session: model called `mcp__lsp__get_document_symbols` and reported
32 real symbols from `persona_injector.py`. Full chain Claude Code → LiteLLM →
gateway → MCP → mcpls → pyright → consumed.

## Revisit if

- mcpls fans `workspace_symbol_search` across servers (then the caveat dies).
- A language is added — install the server, verify it *answers*, then declare it.
- VS Code ever needs it (it does not: native language servers).

## Deeper reference

- `docs/lsp-integration.md` — setup and per-language detail.
- `cadence/config/mcpls.toml` — the declarations themselves, with per-server notes.
- `cadence/scripts/verify-lsp.sh` — the probe that enforces the rule above.
