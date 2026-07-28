# ADR 007 — MCP registration fan-out

**Status:** Accepted · **Date:** 2026-07 · **Owner:** Cadence (not ailocal)

## Problem

Five client surfaces (claude, claude-local, codex, codex-local, VS Code) each
want MCP servers, in three different config formats, across two config roots.
Registering by hand drifts immediately.

## Decision

`cadence/config/mcp.yaml` is the single source of truth.
`generate_mcp_config.py` fans one declaration out to every surface in that
surface's own format, and owns **only** the server names declared there —
every other MCP server already present in those files is preserved byte-for-byte.

## Why

It mirrors ailocal's `profiles → sync-models.py → client configs` pattern
deliberately: one generation idiom across both systems, so neither needs
client-specific installer logic.

## Scope decisions, and why they are not uniform

| Surface | grepai | lsp | Reason |
|---|---|---|---|
| claude-local | yes | yes | local models have no native symbol tooling |
| codex-local | yes | yes | registered — but see the blocker below |
| claude (hosted) | yes | no | may ship native LSP; two symbol paths would compete |
| codex (hosted) | yes | no | same |
| VS Code | yes | **no** | it has native language servers; a bridge duplicates them |

**Not forcing uniformity is the decision.** Clients have genuinely different
capabilities and adding LSP to VS Code would be duplicated functionality, not
parity.

## Tradeoffs / the seam that bites

ailocal's `install-clients.sh` rewrites Codex's `config.toml` wholesale, which
**erased** the `[mcp_servers.*]` blocks on every run. The failure was invisible:
Codex simply starts with no tools rather than erroring. The documented fix was
"remember to re-run cadence afterwards", which is exactly the kind of step that
gets forgotten — so `install-clients.sh` now re-invokes `cadence mcp sync`
itself. Ownership stays with Cadence; only the ordering is enforced in code.

## Measurements

- `codex mcp list` shows `grepai` and `lsp` enabled in codex-local.
- Claude Code: all 31 MCP tools (11 grepai + 20 lsp) arrive in the payload and
  are callable — verified by a real session calling
  `mcp__lsp__get_document_symbols` and `mcp__grepai__grepai_search`.

## Known limitations — **codex-local cannot use MCP** (upstream)

Codex declares MCP servers to the model as `namespace` bundles. LiteLLM discards
every namespace-typed entry when translating `/v1/responses` down to Chat
Completions. Measured on a real `codex exec` run: `bytes_prefiltered_by_litellm:
27239` — all of `mcp__lsp` and `mcp__grepai` — after which the model reported
"there are no MCP resources or resource templates available".

Gateway-side flattening is implemented (`namespace_expansion`) and deliberately
**disabled**: Codex's dispatcher then rejects the flattened names
(`unsupported call: mcp__lsp__workspace_symbol_search`). Enabling it only spends
context on tools the client will refuse.

Two independent upstream tracks — either would fix this:

- **LiteLLM side:** [BerriAI/litellm#29854](https://github.com/BerriAI/litellm/issues/29854)
  — `type=namespace` tools from the Responses API are silently stripped in
  conversion. This is the layer that actually does the dropping, and LiteLLM's
  own docs confirm the filter forwards only function/mcp/web_search tools.
- **Codex side:** [openai/codex#20652](https://github.com/openai/codex/issues/20652),
  fix in unreleased [PR #17556](https://github.com/openai/codex/pull/17556).
  CLIProxyAPI#3298 is the same bug seen from another proxy.

## Revisit if

- Codex ships PR #17556 → re-run `scripts/validate-codex-e2e.sh`; this verdict
  is version-pinned, not permanent.
- Claude Code ships native LSP → drop the bridge for hosted Claude.
- A LiteLLM upgrade changes `drops_tool_types` for `/v1/responses`.
