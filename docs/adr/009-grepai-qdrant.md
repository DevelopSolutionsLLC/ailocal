# ADR 009 — grepai + Qdrant for repository intelligence

**Status:** Accepted · **Date:** 2026-07 · **Owner:** Cadence

## Problem

A local model with a 16–64K window cannot read a repository to answer "where is
X handled". Grepping blindly burns context on the wrong files.

## Decision

grepai as the retrieval layer, backed by a **shared Qdrant workspace index**
(`workspace_cadence`), embeddings via Ollama `nomic-embed-text`. Exposed to
every client as the `grepai` MCP server.

## Why shared, not per-project

One index across the workspace means cross-project questions work and one
service is maintained. The per-project local vector store (`index.gob`) is
deprecated in favour of it.

## Load-bearing files — do not delete

- `.grepai/config.yaml` — **a project without it is never indexed at all**, even
  though it is listed as a workspace member.
- `.grepai/symbols.gob` — the symbol table behind `trace_*`, per-project even
  when vectors live in the shared store.
- Only `index.gob` is the deprecated local store.

## The failure mode this design has

**An unindexed repo returns other projects' results, confidently scored.**
Nothing looks broken. So: verify with `grepai_index_status` before trusting a
negative, and if a result set contains nothing from the repository you asked
about, treat that as infrastructure failure rather than evidence the code does
not exist.

Empty is not absent — a missing symbol store answers a call-graph query with "no
callers", which reads exactly like "this function has no callers".

## Measurements

- Qdrant `workspace_cadence`: 1139 points, status green.
- ailocal: 590 chunks, 149 symbols, `symbols_ready: true`.
- Verified through a real agent session: the model called
  `mcp__grepai__grepai_search` and got the correct file for "persona injection
  hook".

## Known limitations

- **Semantic search is weakest on prose.** In a Markdown-heavy index, generic
  queries return plausible documentation rather than implementation. Prefer
  distinctive identifiers over conceptual phrasing when hunting code.
- Symbol tracing needs a supported language; `symbols_ready: false` /
  `total_symbols: 0` is the *correct* state for a prose-and-shell repo, not a
  misconfiguration.
- One-way dependency: ailocal owns the Ollama daemon, and this index depends on
  it for `nomic-embed-text`. **Tearing down ailocal silently breaks indexing.**

## grepai vs LSP — when to use which

grepai answers *concepts* ("where is retry handled"). LSP answers *exact*
questions ("where is this symbol defined", "what references it"). Use grepai to
locate, LSP to confirm, and read only the ranges either one identified.

## Revisit if

- The workspace outgrows a single Qdrant collection.
- A repo's results start coming from a neighbour (check `config.yaml` first).

## Deeper reference

- `docs/repo-intelligence.md` — retrieval boundary, CLI vs MCP, and why that
  distinction is not cosmetic.
- `cadence/config/mcp.yaml` — where the grepai server is declared for each client.
