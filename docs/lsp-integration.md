# LSP integration

`Claude Code → MCP (mcpls) → language server → symbols`

**Status: working end-to-end for Claude Code.** [REAL]

## What is configured

| Language | Server | Status |
|---|---|---|
| Python | `pyright-langserver` | present, **verified resolving symbols** |
| TypeScript/JS | `typescript-language-server` | present |
| C/C++ | `clangd` | present (ships with Xcode CLT) |
| Go | `gopls` | not installed — deliberately **not declared** |
| Rust | `rust-analyzer` | not installed — deliberately **not declared** |

`pyright-langserver` + `typescript-language-server` match the documented stable
defaults for mid-2026, so this is the mainstream configuration, not a bespoke one.

**Phantom entries are worse than missing ones.** A declared-but-absent server
makes mcpls advertise the MCP tool, the spawn fails, and the model reads the empty
result as "no references exist" rather than "server absent".

## The failure mode that matters

`workspace_symbol_search` returning `{"symbols":[]}` is **ambiguous**:

- the language server is still initializing → mcpls returns a JSON-RPC **error**
  saying "server is still initializing - wait and retry"
- there genuinely are no matches → empty result, **no error**

A probe reading `response["result"]` and ignoring `response["error"]` cannot tell
these apart. It sees `{}` and reports "0 symbols, no error" — which looks exactly
like "this language does not work".

**This produced a real false negative here.** `cadence/scripts/verify-lsp.sh`
reports `✗ symbol not resolved: {"symbols":[]}` and exits FAILED. The LSP is fine:
`scripts/verify-lsp-e2e.sh` gets an empty list on attempt 1 and resolves
`ToolGateway` at `config/litellm/tool_gateway.py:383` on attempt 2.

Always: retry with a real delay, and print `response["error"]` before concluding.

## Verify

```
./scripts/verify-lsp-e2e.sh [repo]
```

Retries 6× with 10 s delays, surfaces errors before results, and distinguishes
"still loading" from "genuinely empty".

## What each client can consume

| Client | LSP via MCP | Evidence |
|---|---|---|
| **Claude Code** | **YES** — 20 tools | configured → transmitted → emitted → accepted |
| **Codex** | **NO** | MCP arrives as `namespace` bundles, which LiteLLM drops; flattening is rejected by Codex's router ([codex#20652](https://github.com/openai/codex/issues/20652)). No fix in 0.145.0. |
| **VS Code** | via the editor's own LSP | does not use this bridge |

## A change that was tried and reverted

mcpls's troubleshooting guide recommends excluding build/vendor directories to
shorten cold-start indexing. Adding a `[files] excludeDirs = [...]` block took
mcpls from **20 tools to 0** and every call stopped responding — the key was taken
from a prose summary, not from the actual TOML schema. Reverted; 20 tools and
symbol resolution returned immediately.

The optimization may still be valid under the correct key name. It needs the real
schema (`mcpls --help`, or the repo's config reference) before being retried.
