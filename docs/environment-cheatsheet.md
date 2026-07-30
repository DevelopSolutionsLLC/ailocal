# Local AI environment — operator & AI-session cheat sheet

One page for a new session (human or AI) on Claude Code, Codex, or VS Code.
Verified 2026-07-28. Anything not actually exercised says so — unmarked
confidence in this file is a bug.

## If you are a new AI session, read this first

**Where truth lives.** Two source files decide everything downstream:
`config/profiles/<tier>.yaml` (what each capability IS) and
`config/clients.yaml` (which capability each client uses) in ailocal; and
`cadence/config/mcp.yaml` (MCP registration) plus `cadence/config/mcpls.toml`
(the LSP bridge) in Cadence. Everything else is generated. **Never hand-edit a
generated region** — edit the source and re-run the generator.

**Do not duplicate tooling.** Before adding a capability, check whether the
client already has it natively. Three real examples: Claude Code ships native
LSP (so we retired our bridge for it), VS Code has an extension ecosystem (so it
uses that, not our bridge), and LiteLLM has its own web-search interception (so
we do not implement search). The rule is: use the client's official mechanism;
add ours only where none exists.

**First moves in a session**

```bash
./scripts/doctor.sh              # is the stack healthy?
./scripts/validate-deployment.sh # does it actually answer, end to end?
```

Then use the discovery ladder below (grepai → LSP → grep → read) rather than
opening files at random.

**Before you believe anything**, remember the rule this whole system is built
around: *presence is not capability, and empty is not absent.* A configured MCP
server, an enrolled repo, an installed plugin and a listed model all say nothing
about whether the thing answers. Most of the hard bugs here were something that
looked configured, returned empty, and was read as "not found".

**Before you change anything**, read `docs/adr/` — each record carries the
measurements behind a decision and the conditions that would justify revisiting
it. Several obvious-looking "improvements" were already tried and measured.

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
| "show me hello world in C++" | `conversational` | **none** on turn 1 (61 → 1); see caveat |
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
the delegation tool it was asking for.

Mode is `filter` (set in `.env`; the compose default is `off`).

**Caveat — the conversational class holds for turn 1 only.** Measured across 9
gateway turns of one session, classification drifts (`class=None`, 48/61 kept)
and the model resumes exploring. Open issue, root cause and non-fix rationale in
ADR 004; `benchmark-baseline.sh` fails on it deliberately.

## Subagents — WORKING (verified end to end)

`architecture` delegates to a subagent, the subagent runs on its own tier, and
results flow back. Measured through `claude -p`:

```
TOOLS CALLED: Read, Agent, TaskOutput, Read
MODELS USED:  ailocal-architecture  +  claude-fable-5 -> review (gpt-oss:20b)
```

**The tool is `Agent`, not `Task`.** Claude Code renamed it in v2.1.63; `Task`
survives only as an alias in settings and agent definitions. The live `Task*`
names are something else — `TaskCreate`/`TaskGet`/`TaskList`/`TaskUpdate`/
`TaskStop`/`TaskOutput` are BACKGROUND-TASK MANAGEMENT.

That rename cost two wrong conclusions here, both recorded so they are not
repeated: first "the local model won't delegate" (it had no tool), then
"headless mode does not expose subagents" (it does — a payload capture showed no
`Task`, but `Agent` was there and this gateway was dropping it, because only
`Task*` had been moved out of `orchestration`). Check the tool the client
actually sends, not the name the docs used two versions ago.

Delegation is not a goal in itself. Intended behaviour: simple question → answer
directly; small edit → implementation only; large architectural change →
architecture may delegate; risky change → implementation + review.

## Finding things (the ladder)

1. **Semantic search** — `grepai_search` for concepts. Verified working.
2. **Symbol/call graph** — `grepai_trace_callers` / `trace_callees`.
3. **LSP** — exact questions: where defined, what references it.
4. **Grep/Glob** — when the path is already known.
5. **Read** — only the ranges search identified.

### LSP

**Claude uses NATIVE LSP** (since 2026-07-28) — `ENABLE_LSP_TOOL=1` in the
deployed `settings.json` plus official `*-lsp` plugins. One `LSP` tool, nine
operations, and automatic diagnostics after every edit.

**Ownership split.** ailocal provides the minimum local-client compatibility
baseline required by the isolated profiles it creates: the **Python** plugin
(`pyright-lsp`) in `~/.config/ailocal/claude`, so `claude-local` has a working
LSP with Cadence absent. Cadence provides repository intelligence, broader
language tooling (TypeScript/Go/C, both roots), cross-client integration and
policy — and detects and REUSES the baseline rather than reinstalling it.
`scripts/test-lsp-baseline.py` proves the ailocal half by driving
pyright-langserver over stdio against a real repo file.

The mcpls MCP bridge was retained for **codex-local** (Codex has no native LSP),
but as of 2026-07-29 that path is **dead end-to-end** — Codex's router rejects
every MCP dispatch (see "The Codex divergence"). So mcpls currently serves no
working client: Claude uses native LSP, VS Code uses native language servers,
and codex-local cannot call it. It was 20 tools / 10,021 B against native's
1 tool / 2,224 B for the same operations; removing it took the claude-local
payload from 49 to 26 tools.

VS Code is excluded from both: it has native language servers.

Two dead ends, measured, so nobody retries them: a settings-level `lspServers`
block is ignored (only plugin manifests declare servers; the field is
`extensionToLanguage`), and there is no official shell plugin — so `.sh`/`.zsh`
have no native coverage. Read/Grep and running shellcheck directly still work.

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
all). Default-queried, all API-backed: `github`, `stackoverflow`, `mdn`,
`docker hub`, `pypi`, `npm`, `askubuntu`, `superuser`, `wikipedia`.
Opt-in via bang only: `google cse` (`!gcse`), `duckduckgo` (`!ddg`).

**Why the scraped ones are demoted.** Measured: google cse 10%→0%
("too many requests"), duckduckgo 30%→0% (CAPTCHA), while API-backed engines
ran 90–100%. SearXNG scrapes, so upstream classifies it as a bot — that is
upstream's judgement of our OUTBOUND traffic, and no header or limiter setting
touches it. Demoting them kept results identical (52.1/query, zero empty),
halved latency (1.46s → 0.73s) and took engine errors to zero.

**API-backed is not immunity** — github fell to 20% once unauthenticated API
quota was exhausted by measurement bursts. It removes bot detection, not rate
limits.

Check health with a burst, never a single query: `./scripts/search-health.sh`.
The signal to act on is **zero-result queries**, not an individual engine at 0%.
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
| Subagents (`Agent` tool) | **yes, verified** | yes | prompts only | prompts only | no |

### The Codex divergence (measured 2026-07-28, the one real capability gap)

`codex-local` has grepai and lsp registered and running (`codex mcp list` shows
both enabled) — **and the model still cannot call either.**

Codex declares MCP servers to the model as `namespace` bundles
(`{"type":"namespace","name":"mcp__lsp","tools":[...]}`). LiteLLM discards every
namespace-typed entry when translating `/v1/responses` down to Chat Completions.
Measured on a real `codex exec` run: `bytes_prefiltered_by_litellm: 27239` — all
of `mcp__lsp` and `mcp__grepai` — after which the model reported "there are no
MCP resources or resource templates available".

Flattening the bundles at the gateway is implemented (`namespace_expansion`),
kept **experimental, disabled, and test-covered**. Re-verified 2026-07-29 on
codex-cli **0.146.0** (the latest *stable*; `0.147.0-alpha.1` is schema-identical
on every relevant field). Per-boundary state — codex-local MCP/LSP is
**unavailable end-to-end**, and no proxy-side change can alter that:

| boundary | state |
|---|---|
| MCP registration generated (Cadence) | supported |
| Codex emits MCP tools | namespace-wrapped, unconditional |
| LiteLLM `/v1/responses` translation | drops namespace tools |
| experimental flattening reaches the model | proven (49 tools, 0 namespaces left) |
| model emits calls against flattened names | proven (structured calls) |
| Codex router dispatches flattened calls | **rejected** |
| end-to-end MCP/LSP in codex-local | **unavailable** |

Blocker: **openai/codex#20652**. Both name forms fail (`grepai_list_projects`
and `mcp__grepai__grepai_list_projects`), with `non_prefixed_mcp_tool_names`
enabled — so it is the dispatcher, not the name shape. Do not retry naming
variants or further LiteLLM routing changes; reopen only if that issue changes.

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

### The two Claude config roots, and why the local one repeats itself

There are two independent Claude Code configuration roots:

| Root | Used by | Instruction file | Owner |
|---|---|---|---|
| `~/.claude` | plain `claude` (hosted) | `~/.claude/AGENTS.md` | the user — ailocal never writes here |
| `~/.config/ailocal/claude` | `claude-local` (`CLAUDE_CONFIG_DIR`) | `~/.config/ailocal/claude/AGENTS.md` | ailocal, generated |

The isolation is the point, and it has a consequence people get wrong: the local
root **does not inherit `~/.claude/AGENTS.md`**. Nothing is layered, imported, or
followed across roots. So the local profile cannot be a thin overlay on the
shared engineering policy — the policy has to be deployed *into* it.

That is why `config/clients/AGENTS.md` is composed rather than hand-written.
`sync-models.py` concatenates two in-repo sources —
`config/clients/claude/instructions/00-engineering-policy.md` (the shared rules,
kept inside ailocal so there is no cross-repo ownership with Cadence) and
`10-ailocal-overlay.md` (local routing, tools, runtime) — and substitutes the
capability and compat-alias tables from the same profile/`clients.yaml` data
every other generated file uses. Edit the sources, never the composed file or the
deployed copy.

Regenerate and deploy:

```bash
./scripts/sync-models.sh          # recompose config/clients/AGENTS.md
./scripts/install-clients.sh claude   # deploy it to ~/.config/ailocal/claude/
```

Deployment is a full overwrite — the stale copy is never a source, which is how
the previous hand-maintained version rotted (a wrong context figure and a backend
table four rows out of date). `scripts/test-claude-instructions.py` asserts the
removed lines stay removed.

### Native LSP vs MCP LSP vs grepai vs Grep

Four different systems, routinely confused:

- **Native LSP** — a Claude Code *client-side* tool named `LSP`. The protocol never crosses LiteLLM; only its schema does. Exact answers: definition, references, type.
- **MCP LSP** (`mcp__lsp__*`) — the mcpls bridge, now `codex-local` only.
- **grepai** (`mcp__grepai__*`) — semantic and call-graph search over the Qdrant index. Good at "where is X handled"; weakest on prose.
- **Grep/Glob** — a literal filesystem scan. Last resort, when the path is already known.

The gateway now names native `LSP` explicitly in the registry's `native_lsp`
group, listed in the `always` floor. Before that it survived only because the
gateway fails open on tools matching no group — the right outcome for the wrong
reason, and one a future tightening of fail-open would have silently removed.

VS Code: MCP `grepai`, `litellm-connector` extension for model routing, native
LSP. Instructions are layered, not duplicated — `~/.copilot/instructions/`
(global, `applyTo: "**"`) plus the repo's `.github/copilot-instructions.md`.

## Latency: what to expect (measured, ADR 010)

TTFB is **prompt evaluation**, not the proxy and not model loading.

| layer | cost |
|---|---|
| LiteLLM + all hooks | ~70 ms (0.36 s vs 0.29 s direct) |
| model load, cold | 3.9 s (30B) / 2.6 s (2B); resident tiers never pay it |
| prompt eval, 30B | 0.7 s @ 694 tok · 5.8 s @ 5.5K · **27.6 s @ 16.5K** · 84.7 s @ 33K |

Throughput *degrades* with prompt size (989 → 390 tok/s), so a big first request
costs superlinearly. The KV cache makes it a **first-turn** cost: an identical
prompt re-evaluates in 0.03 s. So a slow first turn then fast follow-ups is
expected, not a fault.

The lever is prompt size — which is what the tool gateway attacks. Never measure
prompt eval on a warm repeat; use a cold, large, unique prompt.

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
