# Local AI environment — operator & AI-session cheat sheet

One page for a new session (human or AI) on Claude Code, Codex, or VS Code.
Verified 2026-07-27. Anything not actually exercised says so — unmarked
confidence in this file is a bug.

## Who owns what

Two repos, two jobs. Independent *installations*, not independent *content*.

| System | Owns | Does NOT own |
|---|---|---|
| **ailocal** | Local model serving: Ollama daemon (machine-wide), LiteLLM proxy, capabilities, personas, tool gateway, client model configs | MCP registration, LSP |
| **Cadence** | Repository intelligence + tool wiring: MCP registration for every client, the LSP bridge, grepai/Qdrant index, agent overlays | Models, routing, proxies |

Two seams that bite:

1. ailocal's installer rewrites Codex's `config.toml` wholesale, which erased
   Cadence's `[mcp_servers.*]` blocks on every run. `install-clients.sh` now
   re-invokes `cadence mcp sync` itself — MCP ownership stays with Cadence, only
   the ordering is enforced in code.
2. ailocal owns the Ollama daemon, and Cadence's index depends on it for
   `nomic-embed-text`. Tearing down ailocal silently breaks Cadence indexing.

## The request path

```
client (Claude Code / Codex / VS Code)
  → LiteLLM :4000     model_group_alias maps client names → ailocal-<capability>
  → persona injector  merges instructions/_core.md + <capability>.md server-side
  → tool gateway      classifies the task, drops tool groups the task cannot use
  → Ollama :11434     the model
```

**MCP and LSP do not pass through LiteLLM.** They are client-side stdio servers;
the client talks to `grepai` and `mcpls` directly and only the resulting text
enters a model request. Looking for MCP traffic in LiteLLM logs finds nothing —
that is correct, not a fault.

## Capabilities

Five chat tiers plus embeddings. **Use capability names, never raw model tags.**
Picker order follows the key order of `config/profiles/64gb.yaml`.

| # | Capability | Backend | ctx | Role |
|---|---|---|---|---|
| 1 | `architecture` | qwen3-coder:30b-a3b-q4_K_M | 64K | planning, multi-step debugging, **the only tier that sustains a tool loop** |
| 2 | `implementation` | qwen2.5-coder:14b-instruct-q4_K_M | 16K | everyday coding; measured *non-agentic* |
| 3 | `review` | gpt-oss:20b | 16K | diff review, security; the one reasoning tier |
| 4 | `fast` | qwen3.5:2b | 32K | background summarisation, classification |
| 5 | `completion` | qwen2.5-coder:3b-instruct-q4_K_M | 4K | **FIM autocomplete only** |
| — | `embeddings` | nomic-embed-text | 8K | retrieval infrastructure |

`completion` is a trap: FIM-only at 4096, so any conversational turn routed
there is a hard 400. `sync-models.py` fails the build if a Claude slot points at
it. Claude Code slots: Opus→architecture, Sonnet→implementation, Haiku→fast,
Fable→review — one capability each, or `/model` lists a tier twice.

`review` returns a `thinking` block **plus** a `text` block. Reading
`content[0].text` shows empty and looks like a dead tier; concatenate all blocks.

## Tool activation (the thing that shapes behaviour most)

The gateway classifies each request and sends only the tool groups that class
needs. This is why a general question no longer triggers a repo crawl.

| Request | Class | Tools |
|---|---|---|
| "show me hello world in C++" | `conversational` | **none** (61 → 1 measured) |
| "fix the typo in README" | `simple_edit` | read/edit/run only |
| "where is X handled" | `explore` | + search, LSP |
| "why is it failing" | `debug` | + search, LSP, delegation |
| "review this for security" | `review` | + search, LSP, delegation |
| "refactor the auth module" | `architecture` | + search, LSP, planning, delegation |
| unmatched | *(none)* | everything (fails open) |

Two safety properties, both deliberate: an unmatched task keeps every tool
(losing tools mid-task strands an agent; spare schemas only cost tokens), and
the `conversational` class only fires on a genuine first turn — otherwise a
session opening with a chat question would stay tool-less forever.

`mention_overrides` re-adds a group the user names explicitly. Classification
reads the *topic* and is blind to instructions about *how* to work: "delegate
this to the reviewer subagent" matched `review` on the word "security" and lost
the Task tool it was asking for.

Mode is `filter` (set in `.env`; the compose default is `off`).

## Subagents — current status

`Task` was grouped with `Workflow`/`Cron`/`worktree` in `orchestration`, which
every local model class denies, so the gateway stripped it and claude-local could
not delegate at all. Task now has its own `delegation` group. **Verified: no
longer dropped.**

**Not verified end-to-end, and not verifiable with the current harness.**
`claude -p` (headless) does not offer the subagent `Task` tool at all — measured
by capturing the real payload: 47 tools arrive containing `TaskCreate`/`TaskGet`/
`TaskList`/… (background-task management) but no bare `Task`, and passing
`--agents` explicitly does not change that. So whether the 30B *chooses* to
delegate in an interactive session is untested. Do not claim it works or that the
model "won't delegate" — neither has been shown.

Delegation is not the goal in itself. Correct behaviour: simple question → answer
directly; small edit → implementation only; large architectural change →
architecture may delegate; risky change → implementation + review.

## Finding things (the ladder)

1. **Semantic search** — `grepai_search` for concepts. Verified working.
2. **Symbol/call graph** — `grepai_trace_callers` / `trace_callees`.
3. **LSP** — exact questions: where defined, what references it.
4. **Grep/Glob** — when the path is already known.
5. **Read** — only the ranges search identified.

### LSP

Registered for `claude-local` and `codex-local` only. VS Code is excluded on
purpose: it has native language servers, and a bridge would duplicate them.

| Language | Server | Status |
|---|---|---|
| Python | pyright-langserver | verified answering |
| TypeScript / JavaScript | typescript-language-server | verified answering |
| Go | gopls | verified in a real module |
| Bash / POSIX sh | bash-language-server | verified answering |
| zsh | bash-language-server | navigation only (see below) |
| C / C++ | clangd | configured |

**Verified through a real agent session**: the model called
`mcp__lsp__get_document_symbols` and correctly reported 32 symbols from
`persona_injector.py`. Full chain: Claude Code → LiteLLM → gateway → MCP → mcpls
→ pyright → result consumed.

**Routing limit (mcpls 0.3.7, undocumented upstream).** Document-scoped tools
(`get_definition`, `get_references`, `get_document_symbols`, `get_hover`) route by
file extension and work for every language. `workspace_symbol_search` does **not**
fan out — it goes to whichever server became ready first, so it answers for one
language and returns `{"symbols":[]}` for the rest, which is byte-identical to
"not found". Prefer document-scoped lookups; never read an empty result as proof
of absence. Same for cold starts: a server still indexing returns empty, not an
error.

**zsh** has no language server anywhere. bash-lsp parses it with the bash grammar
so navigation works on the compatible subset, but it skips shellcheck because
shellcheck does not support zsh. A clean `.zsh` result means nothing was linted.

**gopls is module-scoped** — in a repo with no `go.mod` it correctly reports
nothing.

### Repository intelligence

grepai → Qdrant (`workspace_cadence`, 1139 points, green) → Ollama
`nomic-embed-text`. Check `grepai_index_status` before trusting a negative: a
project without `.grepai/config.yaml` is listed but never indexed, and searches
then silently return results **from other projects**. Never delete
`.grepai/config.yaml` or `symbols.gob`; only `index.gob` is deprecated.

### Web search

SearXNG on `127.0.0.1:8080`, reachable from LiteLLM as `http://searxng:8080`
(verified from inside the container: 60–70 results).

**Engines** (`deploy/searxng/settings.yml`, `keep_only` decides what loads at
all): `google cse`, `duckduckgo`, `github`, `stackoverflow`, `wikipedia`.
Audited 2026-07-28 — availability drifts, so re-measure rather than trusting any
list:

- `mojeek` **removed**: HTTP 403 on 3/3 direct requests, "Suspended: access
  denied" with 0 results via SearXNG. It 403'd on the first query of every fresh
  container and contributed only error noise.
- `duckduckgo` **added** in its place: previously excluded for CAPTCHA, now
  serving 2 of 3 queries. Strictly better than the 0/3 it replaced, and SearXNG
  degrades cleanly (44 results from the remaining engines when ddg CAPTCHAs).
- `qwant` stays excluded. Its CAPTCHA lines in older logs **predate** the
  `keep_only` list — it is no longer loaded and cannot fail. Check timestamps
  before acting on log lines; much of what looks live is history.

**Log noise, settled.** `deploy/searxng/limiter.toml` exists solely to quiet
bot-detection startup messages; it does **not** enable the limiter
(`server.limiter: false` governs that). Use
`botdetection.trusted_proxies`, not the deprecated `[real_ip] x_for`, which
emits its own deprecation errors on 2026.7.24. One line remains and is expected:

```
ERROR:searx.botdetection: X-Forwarded-For nor X-Real-IP header is set!
```

It fires **once per container start and never per request** (measured: 8
searches produced zero additional lines) and blocks nothing, because the limiter
is off. Do not "fix" it by injecting forwarded headers from LiteLLM — that turns
on machinery this private, loopback-bound instance does not need.

**Which path actually reaches SearXNG.** Claude Code's native `WebSearch` is a
*client-side* tool and never does. LiteLLM's interception accepts only
`litellm_web_search` and bare `web_search`, and deliberately refuses a
`WebSearch` carrying an `input_schema` so it cannot hijack the client's own
handler. Interception is verified *configured* — `search_tools: searxng-search`
registers at boot, `enabled_providers: [ollama_chat]` matches the backend, and
the `web_search` tool passes the gateway ungated (kept 1, dropped 0). It is
**not** verified end to end: the local model narrates instead of emitting a
`web_search` tool_use, even with `tool_choice` forcing it. SearXNG itself is
proven healthy; the unproven link is the model's tool emission.

### Which system answers which question

| Question | Use |
|---|---|
| "Where is retry handled?" (concept) | grepai |
| "Where is this defined / what calls it?" | LSP document-scoped |
| "What changed and why?" | git log / blame |
| "What does this library do?" (external) | SearXNG |
| "Summarise this file" | `fast` tier |

## Clients

| | claude-local | Claude (hosted) | codex-local | Codex (hosted) | VS Code |
|---|---|---|---|---|---|
| Routes via LiteLLM | yes | **no** | yes | **no** | yes |
| Tool gating applies | yes | no | yes | no | yes |
| MCP registered | grepai + lsp | grepai | grepai + lsp | grepai | grepai only |
| MCP **usable by the model** | **yes** (measured) | yes | **NO** — see below | expected yes (untested) | yes |
| Subagents | see status above | yes | prompts only | prompts only | no |

### The Codex divergence (measured 2026-07-28, the one real capability gap)

`codex-local` has grepai and lsp registered and running (`codex mcp list` shows
both enabled) — **and the model still cannot call either.**

Codex declares MCP servers to the model as `namespace` bundles
(`{"type":"namespace","name":"mcp__lsp","tools":[...]}`). LiteLLM discards every
namespace-typed entry when translating `/v1/responses` down to Chat Completions.
Measured on a real `codex exec` run: `bytes_prefiltered_by_litellm: 27239` — all
of `mcp__lsp` and `mcp__grepai` — after which the model reported "there are no
MCP resources or resource templates available".

Flattening the bundles at the gateway is implemented (`namespace_expansion`) and
is deliberately **disabled**: Codex's own dispatcher then rejects the flattened
names (`unsupported call: mcp__lsp__workspace_symbol_search`,
openai/codex#20652), so enabling it only spends context on tools the client will
refuse. Upstream PR #17556 is the fix and is not in codex-cli 0.145.0.

Consequence for the "one client-agnostic stack" goal: it holds for Claude Code
(local and hosted) and VS Code, and does **not** hold for codex-local. Hosted
Codex talks to OpenAI directly with no LiteLLM in the path, so namespaces
survive and MCP is expected to work there — that is inference from the
architecture, not a measurement, because testing it spends OpenAI credits.

Re-run `scripts/validate-codex-e2e.sh` after any Codex upgrade; this verdict is
version-pinned, not permanent.

Hosted Claude and hosted Codex never touch the proxy, so none of the routing,
persona or tool-gating work affects them. XDG isolation (`CLAUDE_CONFIG_DIR`,
`CODEX_HOME`) means local and cloud coexist without sharing history, MCP
registrations or credentials.

VS Code: MCP `grepai`, `litellm-connector` extension for model routing, native
LSP. Instructions are layered, not duplicated — `~/.copilot/instructions/`
(global, `applyTo: "**"`) plus the repo's `.github/copilot-instructions.md`.

## Startup

```bash
docker ps            # ailocal-litellm, ailocal-searxng, cadence-qdrant (healthy)
ollama ps            # nomic-embed-text + qwen3-coder resident (keep_alive -1)
./scripts/doctor.sh  # 0 = healthy, 2 = degraded
```

Nothing to export by hand: the `claude-local` / `codex-local` wrappers inject
base URL, key, config dir and the slot map per process, so plain `claude` /
`codex` in the same terminal stay on the cloud. `.env` holds
`LITELLM_MASTER_KEY` and is gitignored.

**LiteLLM parses its config once at boot and the config is bind-mounted**, so
editing `config.yaml`, `registry.yaml`, a hook or a persona changes nothing in
the running proxy and `docker compose up -d` will not restart it. `start.sh`
fingerprints those files and restarts when they change. Without that, the proxy
serves stale routing with nothing in the logs to say so.

## Dependencies

**Required:**

| Tool | Why |
|---|---|
| Docker | LiteLLM, SearXNG, Qdrant |
| Ollama | model runtime |
| pyright-langserver | Python LSP |
| typescript-language-server | TS/JS LSP |
| bash-language-server | bash/sh/zsh LSP |
| **shellcheck** | bash-lsp's diagnostics come from it — without it there are no shell diagnostics **and no error saying so** |
| grepai, mcpls (`~/.cadence/bin/mcpls`) | MCP servers |
| Python venv `~/.cadence/venv` | Cadence tooling (PyYAML, tomlkit) |

**Optional:** `gopls` + go toolchain (Go, installed here), `clangd` (ships with
Xcode CLT), `shfmt` (shell formatting), `rust-analyzer` (**not installed, not
declared**).

Only declare a server in `mcpls.toml` after verifying the binary exists. A
phantom entry is worse than a missing one: mcpls advertises the tool, the spawn
fails, and the empty result reads as "no references exist".

## Verify

```bash
./scripts/test-all.sh                 # 13-check regression gate
./scripts/doctor.sh                   # service health
./scripts/sync-models.sh              # must be a fixed point (zero diff on rerun)
bash <cadence>/scripts/verify-lsp.sh  # per-language LSP, not just "installed"
cadence doctor --root ~/.config/ailocal/claude
```

Debugging:

```bash
docker logs ailocal-litellm | grep tool_gateway_metric | tail -1   # what was dropped and why
docker logs ailocal-litellm | grep request_trace                   # capability routing (STREAMING only)
AILOCAL_TOOL_GATEWAY_CAPTURE=/app/captures                         # dump real tool payloads
```

**Run verification on an idle machine.** Latency-sensitive probes false-fail
while local inference or a package install competes for CPU/GPU — a cold pyright
stays in the empty-result state far longer under load. A failure seen during
concurrent work must be re-run idle before it is believed.

Presence is not capability. A configured MCP server, an enrolled repo and a
listed model all say nothing about whether the thing answers. Report the level
you actually reached.
