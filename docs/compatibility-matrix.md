# Client compatibility matrix

State of every supported client, as measured on **2026-07-28**.
Versions: Claude Code 2.1.220 · codex-cli 0.145.0 · LiteLLM 1.93.0 · mcpls 0.3.7
· SearXNG 2026.7.24.

## Legend

| Symbol | Meaning |
|---|---|
| **OK** | Supported — exercised in a real session, not inferred from config |
| **CAVEAT** | Works, with a documented limitation |
| **BLOCKED** | Blocked upstream; link given, nothing to fix locally |
| **EXP** | Experimental / implemented but disabled |
| **N/A** | Not implemented, deliberately |
| **?** | Expected but untested — treat as unverified |

## Matrix

| Client | Routes via LiteLLM | MCP | grepai | LSP | Web search | Delegation | Personas | Tool gating |
|---|---|---|---|---|---|---|---|---|
| **Claude Code** (hosted) | no | OK | OK | **OK (native)** | OK (native) | OK | N/A | N/A |
| **claude-local** (LiteLLM) | yes | **OK** | **OK** | **OK (native)** | CAVEAT | **OK** | OK | OK |
| **Codex** (hosted) | no | ? | ? | N/A | ? | N/A | N/A | N/A |
| **codex-local** (LiteLLM) | yes | **BLOCKED** | BLOCKED | BLOCKED | CAVEAT | N/A | OK | OK |
| **VS Code** | yes | OK | OK | N/A (native) | N/A | N/A | OK | OK |
| *New client (template)* | — | see ADR 012 | — | — | — | — | — | — |

## Notes per cell

**claude-local — the reference surface.** Everything below was verified by a real
`claude -p` session, not read from config:

- MCP/grepai/LSP: 31 MCP tools arrive and are callable. Model called
  `mcp__lsp__get_document_symbols` (32 real symbols) and
  `mcp__grepai__grepai_search` (correct file).
- Delegation: `TOOLS CALLED: Read, Agent, TaskOutput, Read`, with
  `ailocal-architecture → architecture` and `claude-fable-5 → review`. Parent on
  the 30B, reviewer subagent on gpt-oss:20b.
- Tool gating: conversational request measured **61 tools → 1**.

**codex-local — BLOCKED, the one real gap.** MCP servers are registered and
running (`codex mcp list` shows `grepai` and `lsp` enabled) and the model still
cannot call them. Codex declares them as `namespace` bundles; LiteLLM discards
every namespace-typed entry translating `/v1/responses` → Chat Completions.
Measured `bytes_prefiltered_by_litellm: 27239`; the model then reported "there
are no MCP resources or resource templates available".

- Upstream, two tracks: [BerriAI/litellm#29854](https://github.com/BerriAI/litellm/issues/29854)
  (namespace tools stripped in conversion — the layer doing the dropping) and
  [openai/codex#20652](https://github.com/openai/codex/issues/20652)
  (flattened MCP names rejected by Codex's dispatcher),
  [PR #17556](https://github.com/openai/codex/pull/17556) (the fix, unreleased).
  Same class from another proxy: CLIProxyAPI#3298.
- Gateway-side flattening exists (`namespace_expansion`) and is **deliberately
  disabled** — enabling it only spends context on tools Codex will refuse.
- Trigger to re-test: any Codex upgrade → `scripts/validate-codex-e2e.sh`.

**Codex hosted — untested.** No LiteLLM in the path, so namespaces should
survive and MCP should work. That is inference from the architecture; verifying
it spends OpenAI credits. Do not record it as working.

**Web search — CAVEAT on both local surfaces.** SearXNG is healthy (49–70
results; `/v1/search` via `searxng-search` passes). But Claude Code's native
`WebSearch` is a *client-side* tool and never reaches SearXNG, and the local
model narrates instead of emitting a `web_search` tool_use even under
`tool_choice` forcing. Interception is verified **configured**, not verified end
to end. See ADR 010.

**LSP is NATIVE on both Claude surfaces since 2026-07-28.** `ENABLE_LSP_TOOL=1`
plus the official `pyright-lsp`/`typescript-lsp`/`gopls-lsp`/`clangd-lsp` plugins,
installed into both roots by `install-clients.sh`. One `LSP` tool (2,224 B)
replaced the 20-tool mcpls bridge (10,021 B); payload went 49 → 26 tools.
Native exposes no callable `diagnostics` operation, and diagnostics are NOT
auto-injected after edits — measured, contradicting a claim made from
documentation earlier in this project. Hosted
Claude previously had **no** LSP at all — that gap is closed, and both surfaces
now share one mechanism. Verified: the model called `LSP goToDefinition` and
`findReferences` and reported correct lines. Shell (.sh/.bash/.zsh) is the one
gap — no official plugin, and settings-level `lspServers` is ignored.

**VS Code — LSP is N/A by design**, not missing: it has native language servers
and a bridge would duplicate them (ADR 007). Model routing via the
`litellm-connector` extension; instructions layered global + repo.

**Delegation exists only on Claude Code.** Codex gets prompts
(`--profile plan/review`), VS Code neither. That is the clients' shape.

## LSP: mechanism and language coverage (all measured 2026-07-28)

Three different mechanisms, on purpose — each client gets its own officially
supported one. No client runs two.

| Client | Mechanism | Python | TS/JS | Go | C/C++ | Shell (.sh/.zsh) |
|---|---|---|---|---|---|---|
| Claude Code (hosted) | native `LSP` tool + official `*-lsp` plugins | OK | OK | OK | OK | **none** |
| claude-local | native `LSP` tool + official `*-lsp` plugins | **verified** | OK | OK | OK | **none** |
| codex-local | mcpls MCP bridge | OK | OK | OK | OK | **verified** |
| Codex (hosted) | none | — | — | — | — | — |
| VS Code | its own extensions | OK (Pylance) | OK (core) | OK (golang.go) | — | **none** |

**Why three mechanisms.** Claude has native LSP, so it uses it. Codex has none,
so the bridge is its only path. VS Code has an extension ecosystem, so it uses
that — handing it the bridge would have duplicated TS/JS and created a second
symbol path.

**VS Code was fixed, not just documented.** The "it has native language servers"
justification was only ever true for TS/JS (built into core); the install had
four extensions and no Python or Go at all. `install-vscode.sh` now installs
`ms-python.python` and `golang.go` — the client-native answer.

**Shell is genuinely unavailable to Claude and VS Code.** There is no first-party
shell language server for either. Only codex-local has it, via mcpls, verified:
`.sh` and `.zsh` both return real symbols. zsh is parsed with bash's grammar and
shellcheck is skipped there, so a clean `.zsh` result means nothing was linted.
Do not claim shell LSP where it does not exist.

**codex-local caveat:** its LSP is registered and the servers answer, but the
model cannot reach them — see the namespace blocker above. Registered ≠ usable.

**Native LSP operation notes (measured, not from docs):**
- Nine operations: goToDefinition, findReferences, goToImplementation, hover,
  rename, documentSymbol, workspaceSymbol, incomingCalls, outgoingCalls.
- There is **no callable `diagnostics` operation**, and diagnostics are **not**
  auto-injected after an edit — an `Edit` introducing a real type error returns a
  plain success. An earlier claim to the contrary in this project came from a
  blog post rather than measurement and is retracted.
- `workspaceSymbol` rejects a bare `query`, demanding `filePath`/`line`/
  `character`.
- `hover`/`findReferences` answer "may occur if the LSP server has not fully
  indexed" instead of erroring when the position is not on a symbol — so empty
  is ambiguous here too.

**mcpls note:** it only resolves files inside its workspace root. A path outside
it returns empty, which looks identical to "symbol not found".

## How to re-derive this table

```bash
./scripts/validate-deployment.sh        # connectivity + end-to-end through proxy
./scripts/capability-matrix.sh          # per-model capability, read from Ollama
./scripts/validate-claude-e2e.sh
./scripts/validate-codex-e2e.sh         # re-run after any Codex upgrade
./scripts/validate-vscode-e2e.sh
bash <cadence>/scripts/verify-lsp.sh    # per-language LSP
codex mcp list                          # MCP registration (per root via CODEX_HOME)
```

Run these on an **idle machine**. Contention produces phantom failures — it
false-failed client compatibility three times during this project's development.
