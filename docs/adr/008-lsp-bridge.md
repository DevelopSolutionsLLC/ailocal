# ADR 008 — LSP: native first, mcpls only where native cannot reach

**Status:** SUPERSEDED IN PART, 2026-07-28 · **Owner:** Cadence

> **Claude Code now ships native LSP.** For Claude clients the mcpls bridge is
> retired; it remains only for codex-local. The original bridge reasoning is kept
> below because it still governs that client, and because the measurements are
> the record of how we got here.
>
> **Native LSP (the officially supported path).** Enable with env
> `ENABLE_LSP_TOOL=1` (carried in the deployed `settings.json`) plus one official
> `*-lsp` plugin per language. Installed and enabled by `install-clients.sh` for
> BOTH `~/.config/ailocal/claude` and `~/.claude`, so hosted and local Claude use
> one mechanism — the "one source of truth" goal, finally reachable.
>
> Measured on Claude Code 2.1.220:
>
> | | tools | bytes |
> |---|---|---|
> | native `LSP` | 1 | 2,224 |
> | mcpls bridge | 20 | 10,021 |
>
> Same nine operations (goToDefinition, findReferences, goToImplementation,
> hover, rename, documentSymbol, workspaceSymbol, incomingCalls, outgoingCalls).
>
> **CORRECTION, 2026-07-28.** An earlier revision of this ADR claimed native LSP
> adds "automatic diagnostics after every edit". That came from a blog post, not
> from measurement, and it does NOT hold here. Measured: there is no callable
> `diagnostics` operation (the model tried and fell back to reading the file),
> and an `Edit` that introduces a real type error returns a plain success result
> with no diagnostics attached. The migration still stands on the token and
> standards arguments alone; it does not need a capability that was never
> observed.
>
> Two further operation quirks, measured: `hover` and `findReferences` return
> "may occur if the LSP server has not fully indexed" rather than an error when
> the position is not on a symbol, so an empty result here is as ambiguous as
> everywhere else in this stack; and `workspaceSymbol` rejects a bare `query`,
> demanding `filePath`/`line`/`character` — which makes it awkward for the one
> job its name implies.
> Whole-payload effect once the bridge was descoped: **49 tools / 43,403 B → 26
> tools / 31,555 B**. Verified answering through a custom `ANTHROPIC_BASE_URL` —
> it is client-side, unlike tool search (ADR 001).
>
> **Two things that do not work, both measured, so nobody retries them:**
> 1. A settings-level `lspServers` block is IGNORED. Only plugin manifests
>    declare servers. With `bash-language-server` configured that way the tool
>    still answers `No LSP server available for file type: .sh` — and the
>    manifest field is `extensionToLanguage`, not `extensions`.
> 2. Therefore **shell (.sh/.bash/.zsh) has no native coverage**, because no
>    official shell plugin exists. Accepted cost: Read/Grep still work and
>    shellcheck can be run directly. Authoring a Cadence-owned bash LSP plugin
>    would close it — recorded as future work.
>
> **Why not keep both?** Running native and bridge together was two competing
> symbol paths for the same languages at 10 KB of duplicate schema — the exact
> risk the original scope note worried about, which had quietly arrived.

---

## Original decision (still governs codex-local)

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
