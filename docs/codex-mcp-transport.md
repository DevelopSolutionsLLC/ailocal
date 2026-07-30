# Codex MCP transport — verified boundaries

Authoritative note on why MCP/LSP works from Claude Code and VS Code but **not**
from `codex-local`. Measured, not inferred.

**Verdict: BLOCKED UPSTREAM** — [openai/codex#20652](https://github.com/openai/codex/issues/20652).
Verified 2026-07-27 on codex-cli 0.145.0, **re-verified 2026-07-29 on 0.146.0**
(the latest *stable*; `0.147.0-alpha.1` is schema-identical on every relevant
field, so upgrading changes nothing).

## The seven boundaries

The chain is `Codex → LiteLLM → tool gateway → Ollama → model → Codex → MCP server`.
Each link was measured independently. Reporting "MCP is configured" says nothing
about any of them — configuration is not capability.

| # | Boundary | Default | With `namespace_expansion` |
|---|---|---|---|
| 1 | Cadence registers the MCP servers | works | works |
| 2 | Codex enumerates them over stdio | works | works |
| 3 | Codex declares them to the proxy | `type=namespace` bundles | same |
| 4 | LiteLLM `/v1/responses` → Chat Completions | **drops all namespace entries** | gateway flattens first, so nothing is dropped |
| 5 | Model receives the tools | never sees them | **49 flat function tools, 0 namespaces left** |
| 6 | Model emits a call | n/a | **emits structured calls** |
| 7 | Codex dispatches back to the server | n/a | **REJECTED** |

Expansion moves the failure from boundary 4 to boundary 6. It does not remove it.

```
ERROR codex_core::tools::router: error=unsupported call: grepai_list_projects
ERROR codex_core::tools::router: error=unsupported call: mcp__grepai__grepai_list_projects
```

Both forms fail with `[features.non_prefixed_mcp_tool_names]` enabled, so **the
blocker is the dispatcher, not the name shape**. No naming convention fixes it,
and nothing in the proxy can reach it.

## Namespace wrapping is unconditional

It is **not** Code Mode. Each lever below was applied to a live session and
produced no change in the wire representation. Do not re-propose them.

| Lever | Result |
|---|---|
| provider `namespace_tools = false` | key does not exist — `ModelProviderInfo` has exactly 18 fields, none namespace-related |
| `[features.code_mode] direct_only_tool_namespaces` | no effect under `grepai` / `lsp` / `mcp__grepai` / `mcp__lsp` |
| `[features] code_mode = false` | no effect |
| model-catalog `tool_mode = "direct"` | no effect. Enum is `direct\|code_mode\|code_mode_only` — **`direct_only` is not a `tool_mode` value**, it belongs only to `direct_only_tool_namespaces` |
| `[features.non_prefixed_mcp_tool_names]` | no effect on wire shape; does not make flattened calls dispatchable |

Schema claims must come from the **native** binary — the `codex` on `PATH` is a
JS shim. Resolve it dynamically (see `AGENTS.md`); if the glob matches zero or
several, stop rather than reading the wrong one.

## Current state

`namespace_expansion` is **implemented, test-covered, and disabled by default**
(`config/litellm/registry.yaml`). Enabling it delivers tools the client will
refuse — strictly wasted context. Leave it off until #20652 changes.

MCP/LSP remain fully available to Claude Code and VS Code; only `codex-local` is
affected. Note that `mcpls` was retained *for* codex-local, so it currently
serves no working client.

## Observability

A tool the gateway keeps is not necessarily a tool the backend sees — LiteLLM's
translation runs after the hook. The metric distinguishes the stages:

| field | meaning |
|---|---|
| `tools_in` | declared by the client |
| `tools_dropped` / `dropped_names` | removed by **this gateway** |
| `tools_kept_by_gateway` | survived the gateway, pre-translation |
| `tools_killed_by_translation` / `killed_by_translation` | removed by **LiteLLM** after, each with name, type and reason |
| `tools_kept` | **actually forwarded to the backend** |

Reporting a translation-killed bundle as "kept" cost two misdiagnoses before it
was fixed.

## Re-testing after a Codex upgrade

1. Confirm the version is newer *stable* (`npm view @openai/codex version`).
2. Diff the native binary's schema for namespace/MCP changes.
3. Set `namespace_expansion.enabled: true`, `ailocal start`.
4. `codex exec "Call grepai_list_projects with no arguments."`
5. Grep the run for `codex_core::tools::router`. No `unsupported call` = fixed.
6. Restore `enabled: false` unless boundary 7 passes end-to-end.
