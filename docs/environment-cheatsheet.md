# Local AI environment — operator & AI-session cheat sheet

One page for a new session (human or AI) on Claude Code, Codex, or VS Code.
Everything here was verified on 2026-07-27 unless marked otherwise. Where a thing
is *not* verified, it says so — treat unmarked confidence as a bug in this file.

## Who owns what

Two repos, two jobs. They are independent installations, not independent content.

| System | Owns | Does NOT own |
|---|---|---|
| **ailocal** | Local model serving: Ollama daemon (machine-wide), LiteLLM proxy, capabilities, personas, tool gateway, client configs for Claude/Codex/Continue/VS Code | MCP registration, LSP |
| **Cadence** | Repository intelligence + tool wiring: MCP registration for every client, the LSP bridge, grepai/Qdrant workspace index, agent overlays | Models, routing, proxies |

The seam that bites: **ailocal's installer rewrites Codex's `config.toml` wholesale**,
which used to erase Cadence's `[mcp_servers.*]` blocks on every run. `install-clients.sh`
now re-invokes `cadence mcp sync` itself, so the ordering is enforced in code. Cadence
still owns MCP; ailocal just refuses to leave it broken.

Second seam: ailocal owns the Ollama daemon, and Cadence's semantic index depends on it
for `nomic-embed-text`. Tearing down ailocal silently breaks Cadence indexing.

## The request path

```
client (Claude Code / Codex / VS Code)
  → LiteLLM :4000        model_group_alias maps client-facing names → ailocal-<capability>
  → persona injector     merges instructions/_core.md + <capability>.md server-side
  → tool gateway         measures/optionally trims the declared tool payload
  → Ollama :11434        the actual model
```

MCP and LSP do **not** pass through LiteLLM. They are client-side stdio servers:
the client talks to `grepai` and `mcpls` directly, and only the resulting *text*
enters a model request. A common misreading is to look for MCP traffic in LiteLLM
logs; it is not there and never was.

## Capabilities

Five chat tiers plus embeddings. **Always use capability names, never raw model tags.**

| Capability | Backend | ctx | Use for |
|---|---|---|---|
| `architecture` | qwen3-coder:30b-a3b | 64K | design, multi-step debugging, agentic tool loops |
| `implementation` | qwen2.5-coder:14b | 16K | everyday coding, edits, tests |
| `fast` | qwen3.5:2b | 32K | background summarisation, classification, short lookups |
| `review` | gpt-oss:20b | 16K | diff review, bug/security critique |
| `completion` | qwen2.5-coder:3b | 4K | **FIM autocomplete only** |
| `embeddings` | nomic-embed-text | 8K | retrieval (grepai/Qdrant) |

`completion` is a trap. It is FIM-only at 4096 tokens; any conversational turn routed
there returns a hard 400 (`No models have context window large enough`). It must never
be a slot target, an alias target, or a context-window fallback. `sync-models.py` now
fails the build if any Claude slot points at it.

**Claude Code slot mapping** (generated into `configure.zsh` from `config/clients.yaml`):
Opus→architecture, Sonnet→implementation, Haiku→fast, Fable→review. Each slot gets its
own capability on purpose — two slots sharing one capability lists it twice in `/model`.

## Editing config: the two source files

Everything downstream is generated. Never hand-edit a generated region.

- `config/profiles/64gb.yaml` — WHAT each capability is (backend, ctx, sampling, keep_alive).
  Key order here is the order `/model` displays.
- `config/clients.yaml` — WHICH capability each client surface uses.

Then: `./scripts/sync-models.sh` → `./scripts/install-clients.sh` → `./scripts/start.sh`.

`start.sh` fingerprints `config.yaml`, the hooks and the personas, and restarts LiteLLM
when they change. This matters: the config is bind-mounted and LiteLLM parses it only at
boot, so `docker compose up -d` alone leaves the proxy serving **stale routing with
nothing in the logs to say so**. That silent staleness was real and is now handled.

## How an AI session should find things

Use this ladder and stop as soon as you have the answer.

1. **Semantic search** — `grepai_search` for "where is X handled". Best for concepts.
2. **Symbol/call graph** — `grepai_trace_callers` / `trace_callees`.
3. **LSP** — exact questions: where is this defined, what references it.
4. **Grep/Glob** — when you already know the path or symbol.
5. **Reading files** — only the ranges the search identified.

### LSP: what actually works

Registered for `claude-local` and `codex-local` only. VS Code is deliberately excluded —
it has native language servers, and a bridge would duplicate them.

| Language | Server | Status |
|---|---|---|
| Python | pyright-langserver | verified |
| TypeScript / JavaScript | typescript-language-server | verified |
| Go | gopls | verified (in a real module) |
| Bash / POSIX sh | bash-language-server | verified |
| zsh | bash-language-server | navigation only — see below |
| C / C++ | clangd | configured |

**The routing limit you must know** (measured on mcpls 0.3.7, undocumented upstream):

- `get_definition` / `get_references` / `get_document_symbols` / `get_hover` route **by
  file extension** and work for every language above.
- `workspace_symbol_search` does **not** fan out. It goes to whichever language server
  became ready first, so it answers for one language and returns `{"symbols":[]}` for all
  the rest — which is byte-identical to "this symbol does not exist".

So: prefer document-scoped lookups, and never treat an empty `workspace_symbol_search`
as proof of absence. Same applies to cold starts — a server that is still indexing
returns an empty result, not an error.

**zsh** has no dedicated language server anywhere. bash-lsp parses zsh with its bash
grammar, so navigation works on the bash-compatible subset, but it deliberately skips
shellcheck on zsh because shellcheck does not support zsh. A clean result on a `.zsh`
file does not mean "no problems" — nothing linted it.

**gopls is module-scoped.** In a repo with no `go.mod` it reports nothing. That is the
correct answer, not a fault.

### Repository intelligence

grepai → Qdrant (`workspace_cadence`) → embeddings via Ollama `nomic-embed-text`.
Verified: 590 chunks indexed for ailocal, 149 symbols, retrieval returns correctly ranked
files. Check with `grepai_index_status` before trusting a negative result: a project
without `.grepai/config.yaml` is listed but never indexed, and searches then silently
return results **from other projects** rather than failing.

Do not delete `.grepai/config.yaml` or `.grepai/symbols.gob`; both are load-bearing.
Only `index.gob` is the deprecated local vector store.

### Web search

SearXNG at `127.0.0.1:8080`, reachable from LiteLLM as `http://searxng:8080`. Verified
returning real results (58 for a live query), and `doctor.sh` probes the JSON API the
way LiteLLM actually uses it.

**Claude Code's native `WebSearch` never reaches SearXNG.** It is a client-side tool.
LiteLLM's interception accepts only `litellm_web_search` and bare `web_search`, and
deliberately refuses a `WebSearch` carrying an `input_schema` so it does not hijack the
client's own handler. SearXNG is healthy; it is simply unreachable from that tool.

## Which system answers which question

| Question | Use |
|---|---|
| "Where is retry logic handled?" (concept) | grepai semantic search |
| "What calls `sync_models`?" (exact) | LSP references, or grepai trace |
| "Where is this symbol defined?" | LSP `get_definition` |
| "What changed and why?" | git log / blame |
| "What does this library do?" (external) | SearXNG / web |
| "Summarise this file" | `fast` tier |

## Startup requirements

Services (all bind `127.0.0.1` only):

```bash
docker ps           # expect ailocal-litellm, ailocal-searxng, cadence-qdrant (healthy)
ollama ps           # expect nomic-embed-text + qwen3-coder resident (keep_alive -1)
./scripts/doctor.sh # 0 = healthy, 2 = degraded
```

Required models: `qwen3-coder:30b-a3b-q4_K_M`, `qwen2.5-coder:14b-instruct-q4_K_M`,
`gpt-oss:20b`, `qwen3.5:2b`, `qwen2.5-coder:3b-instruct-q4_K_M`, `nomic-embed-text`.

Env: nothing to export by hand. The `claude-local` / `codex-local` wrappers inject
`ANTHROPIC_BASE_URL`, the key, `CLAUDE_CONFIG_DIR` and the slot map per process, so
plain `claude` / `codex` in the same terminal stay on the cloud. `.env` holds
`LITELLM_MASTER_KEY` and is gitignored.

## Dependencies

**Required** — the workflow breaks without these:

| Tool | Why |
|---|---|
| Docker | LiteLLM, SearXNG, Qdrant |
| Ollama | model runtime |
| pyright-langserver | Python LSP |
| typescript-language-server | TS/JS LSP |
| bash-language-server | bash/sh/zsh LSP |
| shellcheck | bash-lsp's diagnostics come from it; without it there are **no** shell diagnostics and no error saying so |
| grepai + mcpls | MCP servers (`~/.cadence/bin/mcpls`) |

**Optional** — install per language actually used:

| Tool | For |
|---|---|
| gopls + go toolchain | Go LSP (installed here) |
| clangd | C/C++ (ships with Xcode CLT) |
| rust-analyzer | Rust — **not installed, not declared** |

Only declare a server in `mcpls.toml` after verifying the binary exists. A phantom entry
is worse than a missing one: mcpls advertises the tool, the spawn fails, and the empty
result reads as "no references exist".

## Client differences

| | Claude Code (`claude-local`) | Codex (`codex-local`) | VS Code |
|---|---|---|---|
| Config root | `~/.config/ailocal/claude` | `~/.config/ailocal/codex` | Code User dir |
| MCP | grepai + lsp | grepai + lsp | grepai only (native LSP) |
| Subagents | yes (`agents/`) | prompts only | no |
| Models | `/model` picker, gateway-discovered | profiles (`--profile plan/review`) | litellm-connector extension |

Baseline `claude` and `codex` (cloud) get grepai only and are otherwise untouched.
XDG isolation means local and cloud sessions coexist without leaking history, MCP
registrations, or credentials.

## Verify before believing

```bash
./scripts/test-all.sh              # 13-check regression gate
./scripts/doctor.sh                # service health
./scripts/sync-models.sh           # must be a fixed point (zero diff on 2nd run)
bash <cadence>/scripts/verify-lsp.sh   # per-language LSP, not just "installed"
cadence doctor --root ~/.config/ailocal/claude
```

Presence is not capability. A configured MCP server, an enrolled repo, a written plist
and a listed model all say nothing about whether the thing answers. Check the level you
actually reached and report that, not the level you assume.
