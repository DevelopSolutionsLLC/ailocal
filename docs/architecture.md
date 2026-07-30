# ailocal architecture

The runtime, the gateway, and the five mechanisms that carry most of this
repo's complexity. `README.md` covers install and use; `AGENTS.md` is the
AI bootstrap. Troubleshooting lives in `docs/troubleshooting.md`.

`AGENTS.md` carries the bootstrap and links here.

## The five non-obvious mechanisms

Most of this repo's complexity is in these; change them carefully.

1. **Generation.** Two source files drive everything: `config/profiles/<tier>.yaml` (WHAT each
   capability is — backend `active`, context, sampling, keep_alive, metadata) and
   `config/clients.yaml` (WHICH capability each client surface uses + the `claude-*`/`gpt-*`
   compat map). `sync-models.py` regenerates, between GENERATED markers or as whole managed
   files: the `model_list` **and** `model_group_alias` in `config/litellm/config.yaml`,
   `config/capabilities.generated.json`, `config/clients/model_catalog.json`, and the
   `claude/settings.json` · `codex/config.toml` (+ profiles) · `continue/config.json` · copilot
   tables. Never hand-edit a generated region — edit the two sources and run
   `ailocal sync`, then `ailocal clients` to deploy. Which tier is active
   is the one-line `config/active-profile` marker, written by `install.sh` from detected RAM
   (`config/profiles/{16,32,64,128}gb.yaml`); `--profile <tier>` overrides.
   `sync-models.py --resolve <capability>` prints the active backend (used by the shell scripts).
2. **Persona injection.** `config/litellm/persona_injector.py` is a LiteLLM pre-call
   hook merging `config/instructions/_core.md` + `<capability>.md` into whatever system
   prompt the client sent — server-side, so every alias inherits it. It handles **both**
   request shapes: OpenAI (`call_type` completion/acompletion — system lives in
   `messages[]`) and Anthropic `/v1/messages` (`call_type anthropic_messages` — system is
   the **top-level `system`** field), which is the route Claude Code uses. Reasoners get
   **no** persona (DeepSeek's guidance), temp 0.6 / top-p 0.95. LiteLLM issue #27518 (hook
   bypassed on `/v1/messages`) was filed against **v1.83.10**; on the **1.93.0** we run,
   the hook fires AND its mutation reaches the backend on both routes — measured, not
   assumed (persona marker + the propagation probe). Re-verify only after a LiteLLM version change — the image is now PINNED BY DIGEST
   (`deploy/litellm/docker-compose.yml`) and `scripts/check-litellm-version.sh` fails the
   regression gate on drift, because `main-stable` is a floating tag that already moved us
   from 1.92.0 to 1.93.0 with the docs left claiming the old version. Coupling: injection depends on model names resolving back to a
   capability key. The hook resolves the requested model through `model_group_alias` and
   uses that capability key to load `config/instructions/<capability>.md`. Any future change
   to canonical model names, aliases, or routing layers must preserve this mapping or
   personas silently stop applying.
   Completion and embeddings intentionally have no persona.
3. **Reasoning vs. non-reasoning.** No installed model currently thinks — the `deep-think*`
   reasoners (deepseek-r1) were removed, and `qwen3-coder` is Qwen's non-thinking variant. Every
   capability carries `additional_drop_params: ["thinking", "reasoning_effort"]` (so Claude Code
   sending `thinking` to a non-thinking backend doesn't 400) **plus** `think: false` (suppresses a
   backend's default reasoning, which otherwise hangs VS Code Copilot). Both are required — dropping
   either reintroduces a real, previously-hit bug. The reasoning path (`reasoning`/`merge` flags →
   `merge_reasoning_content_in_choices`, no drop, no `think:false`) is still in `sync-models.py`; a
   commented `reasoning` slot in `config/profiles/<tier>.yaml` restores the tier in one repoint.
4. **Client deployment is XDG-isolated.** Everything lands in `~/.config/ailocal/`;
   `~/.claude` and `~/.codex` are never touched, so cloud and local sessions coexist.
   `configure.zsh` defines the `claude-local` / `codex-local` / `ailocal-code` wrappers
   and is sourced from `.zshrc` between installer markers (`finalize.zsh` runs last).
   `CLAUDE_CONFIG_DIR` relocates `.claude.json` itself, so MCP registrations, history, and
   credentials are genuinely per-root — nothing leaks between local and cloud.
   **The local root inherits nothing from `~/.claude`, including its `AGENTS.md`.** So
   `config/clients/AGENTS.md` carries the shared engineering policy itself rather than
   pointing at one, and it is **composed by `sync-models.py`** from
   `config/clients/claude/instructions/{00-engineering-policy,10-ailocal-overlay}.md` with the
   capability and compat-alias tables substituted from the same sources as every other
   generated file. Edit the sources, not the composed file. It was hand-maintained until it
   drifted (a stale 16-32K context claim, a backend table four rows wrong, and a
   filesystem-first search rule contradicting the repository-intelligence ladder);
   `scripts/test-claude-instructions.py` now asserts those removals by string.

5. **Tool gateway.** `config/litellm/tool_gateway.py` is a pre-call hook that measures (and
   optionally trims) the tool payload clients declare. Measured: Claude Code sends **61 tools /
   104KB / 24,448 real Qwen tokens** on every `/v1/messages` request; **70.8%** of it is
   orchestration/scheduling/worktree machinery a local 30B cannot drive. Three modes via
   `AILOCAL_TOOL_GATEWAY` — `off` (compose default), `report`, `filter`. **`.env` sets `filter`,
   so filtering is live**; `off` is only the fallback when `.env` says nothing.

   **Tool activation is what shapes behaviour most, and it is task-classified.** The registry's
   `task_classes` decide which groups survive. A `conversational` class carries
   `override_always: true` so it drops BELOW the `always` floor to *no tools* — without it, "show
   me hello world in C++" arrived holding Read/Glob/Grep/Bash and the coding persona dutifully
   crawled the repo before answering (measured 61 tools → 1 after). Two guards, because losing
   tools mid-task strands an agent while spare schemas only cost tokens: an unmatched task keeps
   everything (fail-open), and the conversational override applies only to a genuine first turn —
   classification reads the FIRST user message, which never changes as a session grows, so without
   that guard a session opening with a chat question would stay tool-less forever.
   `mention_overrides` re-adds a group the user names explicitly; classification matches on TOPIC
   and is blind to instructions about HOW to work, so "delegate this to the reviewer subagent"
   matched `review` on the word "security" and lost the very delegation tool it asked for.

   **The subagent tool is `Agent`, and it lives in `delegation`, NOT `orchestration`.** Claude Code
   renamed it from `Task` in v2.1.63; the live `Task*` names are BACKGROUND-TASK management
   (`TaskCreate`/`TaskGet`/…), not delegation. `Agent` sat in `orchestration`, which every local
   class denies, so the gateway dropped it on every request — misread twice, first as "the model
   won't delegate" and then as "headless mode has no subagents". Both were the gateway. Verified
   working once `Agent` reached the model: it called `Agent`, and the reviewer subagent ran on
   `review` (`claude-fable-5 → review` in request_trace) while the parent stayed on `architecture`.
   The token argument never applied: Workflow alone is 21,525 B, `Agent` is ~1 KB.
   **MEASURED 2026-07-28 — Codex cannot use MCP tools at all, by either route.** Per-client
   gateway metrics show Claude Code receiving `mcp__lsp__get_hover` (flat function tools, which
   survive the `search`/`lsp` groups) while Codex's payload contains only
   `exec_command / multi_agent_v1 / apply_patch / <web_search>` — **no `mcp__*` entry**, with 104 B
   pre-filtered by LiteLLM before the gateway saw it. Codex declares MCP servers as `namespace`
   BUNDLES, which LiteLLM discards before the backend; enabling `namespace_expansion` instead makes
   the model emit flattened names that Codex's own dispatcher then refuses
   (`unsupported call: mcp__lsp__workspace_symbol_search`, openai/codex#20652). Both paths dead-end.
   **RE-VERIFIED 2026-07-29 on Codex 0.146.0** (the latest STABLE; `0.147.0-alpha.1` is
   schema-identical on every relevant field, so upgrading fixes nothing). Namespace wrapping is
   UNCONDITIONAL — it is NOT Code Mode, and no setting turns it off. These were each measured inert;
   do not re-propose them: `namespace_tools` does not exist in the binary (`ModelProviderInfo` has
   exactly 18 fields, none namespace-related); `[features.code_mode] direct_only_tool_namespaces`
   does nothing under any of the four plausible name forms; `[features] code_mode = false` does
   nothing; model-catalog `tool_mode = "direct"` does nothing — and the enum is
   `direct|code_mode|code_mode_only`, so `direct_only` is NOT a `tool_mode` value at all, it belongs
   only to `direct_only_tool_namespaces`. Gateway flattening clears **four of seven** boundaries:
   bundles expand (49 tools, zero namespaces left, zero killed by translation) and the model emits
   structured calls against them — then Codex's router refuses to dispatch BOTH
   `grepai_list_projects` and `mcp__grepai__grepai_list_projects`, with
   `[features.non_prefixed_mcp_tool_names]` enabled. The blocker is Codex's dispatcher, not the name
   shape and not the proxy, so `namespace_expansion` stays `enabled: false`.
   Schema claims about Codex must come from the NATIVE binary: the `codex` on
   `PATH` is a JS shim with none of the Rust config schema in it. Resolve it
   dynamically rather than hardcoding a version- or arch-specific path:
   `ls "$(npm root -g)"/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex`
   then `strings -a` that file (e.g. `struct ModelProviderInfo with N elements`
   enumerates every accepted provider key). If that glob resolves to zero or to
   more than one binary, STOP and report the ambiguity — do not pick one. Zero
   means the layout moved and the guidance is stale; several means multiple
   installs or architectures are present, and reading the wrong one yields a
   schema that looks authoritative while describing a binary you are not running.
   So MCP-delivered capability — grepai, the LSP bridge, and Cadence's intelligence server — is
   reachable from Claude Code and VS Code but NOT from Codex, regardless of registration. Do not
   "fix" this at the proxy; it is a client limitation with an upstream issue.

   ALL facts about models/clients/routes/tools live in `config/litellm/registry.yaml` (the
   capability registry); the negotiator contains no such literal and a test enforces that by
   grepping its code. `tool-policy.yaml` was superseded by the registry and removed. Frontier
   models are `passthrough` — measured, forwarded untouched, and feature flags cannot override it.
   Two traps the code encodes: it is registered
   **last** so `websearch_interception` still sees the client's `web_search` tool, and it never
   drops an entry it cannot name (Codex's bare `{"type":"web_search"}` normalises to
   `<web_search>`; dropping it kills SearXNG silently). It also refuses to book Codex's
   `namespace` tools as savings — LiteLLM already discards those before the backend, so Codex's
   real gain is 18%, not 71%. Full detail, including the token calibration against Ollama's
   `prompt_eval_count`, in `docs/local-agent-gateway.md` (full architecture: registry,
   negotiation, verification, metrics, client profiles, flags, recovery). Phase-2 history
   and the measurement discipline behind the numbers is in `docs/tool-gateway.md`.


---


> **Superseded as the architecture reference by**
> [`local-agent-gateway.md`](local-agent-gateway.md), which documents the registry,
> negotiator, task classification, verification pipeline and metrics.
> This file is kept because it records HOW the numbers were established and
> which measurement mistakes were made getting there — history the newer
> document summarises but does not replace.

The translation layer between a frontier agent protocol and a local model's
actual capability. It measures — and optionally trims — the tool payload every
client declares.

Status: **REPORT mode validated, FILTER mode implemented and off by default.**

---

## The problem, measured

Frontier agent clients declare their entire tool surface on turn 1, before they
know what the task needs. Against a frontier model that is amortised. Against a
30B on Apple Silicon it is paid in prompt-eval time on every request and it
competes for a 16K–64K context window.

Captured from real `claude-local` and `codex-local` runs against
`qwen3-coder:30b-a3b-q4_K_M` (`data/tool-captures/`, gathered by the gateway
itself in REPORT mode — not reconstructed):

| Client | Route | Tools | Bytes | Real Qwen tokens |
|---|---|---|---|---|
| Claude Code | `/v1/messages` | 61 | 104,202 | **24,448** |
| Codex | `/v1/responses` | 16 | 38,388 | 8,026 |

Token counts are Ollama's own `prompt_eval_count`, not an estimate — see
[Token honesty](#token-honesty).

Claude Code's payload by what the tools are *for*:

| Group | Tools | Bytes | Share |
|---|---|---|---|
| orchestration (`Workflow`, `Agent`, `Task*`, `Skill`, …) | 12 | 49,622 | 47.6% |
| scheduling (`Cron*`, `Monitor`, `ScheduleWakeup`, …) | 6 | 17,639 | 16.9% |
| worktree | 2 | 6,538 | 6.3% |
| lsp (`mcp__lsp__*`) | 20 | 11,161 | 10.7% |
| search (`mcp__grepai__*`, `Web*`) | 13 | 9,582 | 9.2% |
| edit_and_run (`Bash`, `Read`, `Edit`, `Write`, …) | 5 | 7,578 | 7.3% |
| mcp plumbing | 3 | 2,082 | 2.0% |

`Workflow` alone is 21,525 B — 20.7% of the payload, one tool.

**70.8% of what Claude Code sends is agent-runtime orchestration**: spawning
subagents, cron scheduling, git worktrees, a task board. That is both the bulk
of the cost and the part a local 30B is least able to drive — multi-step
delegation is exactly where a small model produces plausible, wrong calls.

---

## Design

`config/litellm/tool_gateway.py` is a LiteLLM `CustomLogger` pre-call hook,
registered in `litellm_settings.callbacks`. Three modes, from
`AILOCAL_TOOL_GATEWAY`:

| Mode | Behaviour |
|---|---|
| `off` | Returns on the first line. Nothing measured, nothing changed. **The committed default.** |
| `report` | Measures and logs. Never mutates the request. |
| `filter` | Measures, then applies the allowlist to `data["tools"]`. |

An unrecognised value is logged and treated as `off` — never silently coerced.

### Ordering, and why it is load-bearing

The hook is registered **last**, after `websearch_interception`. Interception
rewrites the client's `web_search` tool into a SearXNG call; if the gateway ran
first and removed that tool, there would be nothing to rewrite and web search
would fail silently. `tool_repair` reads `request_data["tools"]` post-call and
therefore validates against the post-filter list, which is correct — that is
what the model was actually told about.

### The policy

> HISTORICAL: `tool-policy.yaml` was superseded by
> `config/litellm/registry.yaml` in Phase A/B and no longer exists. The rules
> below describe the design at the time; the fail-open reasoning carried over
> unchanged into the registry.

`config/litellm/tool-policy.yaml`: named groups, and ordered `(client,
capability) → allowed groups` rules. First match wins. It lives in
`config/litellm/` because that directory is the read-only mount the proxy sees
at `/app/config`.

Three deliberate fail-open behaviours, each of which exists because the failure
mode of guessing wrong is worse than the cost of doing nothing:

- **No matching rule → the request is untouched.** An unrecognised client is a
  reason to change nothing, not to guess at what it needs.
- **A missing or malformed policy → allow-all**, with the specific reason
  (`absent` / `unavailable` / `error`) in the metric. A corrupt policy must not
  look like a deliberately empty one.
- **An entry the gateway cannot name is never dropped.** A policy is written in
  tool names, so it cannot have formed an intent about an entry that has none.

That last one is not theoretical. Codex declares web search as a bare
`{"type":"web_search"}` with no name. An earlier revision dropped it — which
would have killed SearXNG search silently, the worst possible failure for a
change sold as a performance win. It was caught by replaying a real captured
payload captured from a real client, not by a unit test.

---

## Two ways the numbers could lie, and what was done about them

### Credit for work LiteLLM already does

Codex's three largest declarations (`multi_agent_v1`, `mcp__lsp`,
`mcp__grepai` — 27,168 B of 38,388) are `namespace`-typed. LiteLLM discards
those during its Responses→Chat translation before the backend ever sees them
(`litellm/responses/litellm_completion_transformation/transformation.py:1309`,
dropping `computer_use`, `image_generation`, `namespace`, `shell`). Counting
them as "saved" would claim a ~71% Codex reduction that does not exist at the
model.

So the gateway reports two separate figures: `bytes_reachable` (the real cost
base) and `bytes_prefiltered_by_litellm`. Only reachable bytes are booked as a
saving; the rest is `bytes_dropped_moot`.

Measured against the real captures, this is the difference between a headline
of 71% and the truth of **18%** for Codex.

> **Consequence worth naming separately:** through Codex, the `mcp__lsp` and
> `mcp__grepai` namespaces never reach the local model at all. Codex being
> *configured* with them is not the same as the model being able to call them.
> This is a finding about the current stack, independent of the gateway.

### Token honesty

`litellm.token_counter` selects `openai_tokenizer` (cl100k) even for an
`ollama_chat/qwen3-coder` deployment — verified via
`litellm.utils._select_tokenizer`, not assumed. Qwen's tokenizer is not in the
proxy image, so every `tokens_est` is a proxy figure and is labelled
`tokenizer: "cl100k-proxy"`.

The error was measured by sending the real captured
payloads to Ollama and reading `prompt_eval_count` — the real tokenizer on the
real text:

| Payload | cl100k est | Real Qwen | Ratio |
|---|---|---|---|
| claude-code `/v1/messages` | 23,937 | 24,448 | 1.021 |
| codex `/v1/responses` | 7,953 | 8,026 | 1.009 |

cl100k under-counts Qwen by 1–2% on tool-schema JSON, so `tokens_est` is usable
and slightly conservative. Re-run the calibration after any model change.

---

## Measured effect

`./scripts/benchmark-tool-gateway.sh` with `RUNS=2`, on the real `claude-local`
against `qwen3-coder:30b-a3b-q4_K_M`. Task: *"Read the file sample.py in the
current directory and tell me exactly what it prints. Use your tools."*

| Round | Order | Arm | Tools the model received | Latency |
|---|---|---|---|---|
| 1 | report first | report | 61 / 104,202 B / ~23,937 tok | 136.4 s |
| 1 | | filter | 41 / 30,403 B / ~6,853 tok | 39.9 s |
| 2 | filter first | filter | 41 / 30,403 B / ~6,853 tok | 5.2 s |
| 2 | | report | 61 / 104,202 B / ~23,937 tok | 128.8 s |

**All four runs produced the correct answer** (`the answer is 42`), read from
the file via the model's own tools. No regression in tool execution — which is
the result that mattered, and the reason the runs' outputs are kept for
side-by-side reading rather than reduced to a pass/pass.

What can honestly be claimed from n=2 per arm: **every filtered run was faster
than every unfiltered run, by at least 3.2×**, and the arms' ranges do not
overlap (filter 5.2–39.9 s, report 128.8–136.4 s). Because the order was
flipped between rounds and filter was faster even when it ran first and cold,
the result is not the warm-up artefact that the first, fixed-order attempt
produced.

What cannot be claimed: a precise speedup factor. The filter arm's own spread
(5.2 s vs 39.9 s) is larger than many effects one might want to measure, so
treat this as "clearly and repeatably faster", not as "7× faster".

### A note on the four harness bugs

The first three attempts at this benchmark failed, every time in the measuring
apparatus rather than the thing measured. They are worth knowing about because
each produced a *plausible* result rather than an obvious error:

1. A fixed report-then-filter order gave 244 s → 81 s, which is exactly the
   shape cold-then-warm produces regardless of whether filtering helps.
2. `docker logs | tail -1` read a metric line from a previous container
   instance (`dc up -d` does not recreate when nothing changed), producing a
   baseline row with `bytes_reachable: 0` — a value the module cannot emit.
3. `local a="$1" b="$2" c="…$b…"` — bash expands every argument to `local`
   before assigning any, so `$b` was unbound under `set -u`.
4. `docker logs --since` was given a UTC stamp without the trailing `Z`, so
   Docker read it as local time, hours in the future, and returned **zero
   lines** — indistinguishable from "the run produced no metric".

The harness now discards a row whose metric is missing or stale rather than
coercing it to zero, restores the proxy's default mode from an `EXIT INT TERM`
trap, and warns that `RUNS=1` cannot support a latency claim. Validate the
plumbing with a fast `curl` before spending twenty minutes on the real client.

## Verification

The measurement validates itself before it is allowed to report savings.

```
./scripts/test-tool-gateway.sh        # known-answer tests, host + container
./scripts/benchmark-tool-gateway.sh   # A/B on the real client and model
```

`test-tool-gateway.py` pins every byte figure to **hand-written canonical JSON
literals**, not to the gateway's own encoder — if the encoding changes, the
tests fail loudly instead of drifting. It runs twice: on the host (stdlib, the
policy tests skip and say so) and inside the proxy image (real `litellm`, real
PyYAML, everything runs). The container pass is the one that counts; an
incomplete host-only run exits non-zero rather than reporting green over a
reduced set.

Cross-check worth knowing about: the hand-written ground truth for a `Read`
tool schema is 136 B, and the live production path independently measured the
real `Read` tool at 136 B.

### Capturing payloads

```bash
AILOCAL_TOOL_GATEWAY=report AILOCAL_TOOL_GATEWAY_CAPTURE=/app/captures \
  docker compose ... up -d
```

Captures land in `data/tool-captures/` (a writable mount; `/app/config` is
read-only). They hold tool schemas and the model name — not conversation
content.

---

## Session observation (Step 6)

Two halves, deliberately split across the container boundary:

- **`config/litellm/session_observer.py`** — proxy-side. Records what a session
  asked for and which tools it called, to a ledger under
  `data/tool-captures/sessions/`. Off unless `AILOCAL_SESSION_LEDGER` is set.
  Purely observational: it never mutates a request and never touches client
  conversation state.
- **`scripts/verify-session.py`** — host-side. Pairs the ledger with the git
  delta and an optional test command, and reports. The proxy container cannot
  see the repository, so a verification layer living entirely there could only
  check the model against its own claims.

The mechanism that makes the observer small: agent clients are stateless over
HTTP and re-send the whole conversation each turn, so **one pre-call
observation carries the complete history**. No response hooks, no stream
buffering, no turn correlation.

Validated on real sessions, in both directions:

| Session | Tools | Tree | Test | Verdict |
|---|---|---|---|---|
| no write permission granted | `Edit` ran, 2 errors | unchanged | fail | `SUSPICIOUS`, exit 2 |
| permission granted | `Read`+`Edit`, 0 errors | 1 file changed | passes | no findings, exit 0 |

Both runs had FILTER active (41 tools). The second completed the task in two
clean tool calls — evidence that filtering does not regress tool execution.

### What it refuses to conclude

- **It does not claim causation.** A delta proves the tree changed while the
  session ran, not that the session caused it.
- **`SUSPICIOUS` and `INCONCLUSIVE` are distinct.** `Bash`/`exec_command` with
  no delta is genuinely ambiguous — a session may legitimately only read.
- **"Not a git repo" returns nothing, not an empty delta.** *Cannot verify*
  must not render as *verified clean*.
- **On `/v1/chat/completions`, a tool result's status is `None`, not success.**
  That route has no error flag; absence of one is not evidence.

That restraint paid for itself immediately. The first real session showed
`Edit` running with an unchanged tree, which looks exactly like fabrication.
The report offered alternatives rather than a verdict — and the true cause was
a fourth one it had not listed: a non-interactive `claude -p` cannot be granted
write permission, so the tool was blocked by the harness. The model's `Edit`
call was correct on the first attempt, and it noticed the error and retried
rather than narrating success. A tool that had asserted "the model fabricated
this" would have been confidently wrong about both the model and the cause.

## Enabling FILTER

It is off in the committed default on purpose: FILTER changes what the model
can do, and that is a per-machine decision, not something to ship on.

```bash
echo 'AILOCAL_TOOL_GATEWAY=filter' >> .env
docker compose ... up -d
```

Before turning it on, run the replay and read the drop list. Twenty tool names
is something you can disagree with; a percentage is not.

Overhead: **0.33–0.53 ms** warm per request. The first request after a restart
costs ~42 ms because tiktoken loads its encoding lazily.

---


The runtime layer between a frontier agent client and whatever model is actually
serving it.

```
Claude Code ─┐
Codex ───────┼──▶ LiteLLM ──▶ [ GATEWAY ] ──▶ Ollama / local models
VS Code ─────┘                     │
                                   └──▶ passthrough ──▶ frontier / cloud
```

Every claim below is tagged. **[REAL]** = measured on the production path.
**[INFERRED]** = derived from something measured. **[HYPOTHESIS]** = not yet
tested.

---

## The problem

Frontier agent clients declare their entire tool surface on turn 1, before they
know what the task needs. Against a 200K-context frontier model that is
amortised. Against a 30B on Apple Silicon it is paid in prompt-eval time on
every request and competes for a 16K–64K window.

**[REAL]** Captured by the gateway itself from real `claude-local` /
`codex-local` runs against `qwen3-coder:30b-a3b-q4_K_M`:

| Client | Route | Tools | Bytes | Real Qwen tokens |
|---|---|---|---|---|
| Claude Code | `/v1/messages` | 61 | 104,202 | **24,448** |
| Codex | `/v1/responses` | 16 | 38,388 | 8,026 |

Token counts are Ollama's own `prompt_eval_count`.

**[REAL]** Claude Code's payload by purpose: orchestration 47.6%, scheduling
16.9%, worktree 6.3% — **70.8% is agent-runtime machinery**, and `Workflow`
alone is 21,525 B. By field: descriptions 61.1%, `input_schema` 35.9%.

---

## Design principle: the gateway is the subject, not the model

Earlier revisions were organised around "make the local model work", which put
model assumptions into code. This one is organised around the boundary.

Consequences that fall out of that choice:

- A model the registry marks `passthrough` — any frontier/cloud model — is
  measured and forwarded **untouched**, and **feature flags cannot override
  that**. Trimming a frontier model's tools removes capability for no benefit.
- Swapping a model is a registry edit, not a code change.
- The negotiator contains **no** literal naming any model, client, or tool. This
  is enforced: `test-capability-registry.py` greps its executable code and fails
  if one appears. It *did* fail before the Phase B refactor.

---

## Components

| File | Role |
|---|---|
| `config/litellm/registry.yaml` | Every fact about models, clients, routes, groups, tasks |
| `config/litellm/capability_registry.py` | The only module that knows the registry's shape |
| `config/litellm/tool_gateway.py` | The negotiator (pre-call hook) |
| `config/litellm/session_observer.py` | Records what was asked and which tools ran |
| `scripts/verify-session.py` | Host-side verification and classification |
| `scripts/gateway-metrics.py` | Aggregates the metric stream |

### Registry

Models advertise `supports_tools / mcp / parallel_tools / structured_output /
reasoning`, preferred and denied groups, routing hints. Routes describe the
transport, including which tool types LiteLLM drops itself and whether the route
even *has* a tool-result error flag. Clients declare detection, drop/keep groups,
schema rewrites, warmup.

Two ordering rules that are load-bearing:

1. **Capability match precedes model-name match.** ailocal's compat aliases
   (`claude-sonnet-4-6`, `gpt-4o`) resolve through `model_group_alias` to *local*
   capabilities. Matching names first would classify them as frontier and pass
   them through unfiltered — exactly backwards. Pinned by a test.
2. **`max_context` is read from `capabilities.generated.json`, never restated.**
   Two sources would drift after a profile change.

### Negotiation

```
inspect client → inspect model → inspect tools → select subset → forward
```

A tool is removed only when the model declares no tool support, or when its group
is removable for this `(client, model)` pair and it is not protected.

**Removal requires both sides to agree** — the *intersection* of what the client
profile will drop and what the model class denies, never the union. Neither side
can unilaterally strip a tool the other needs, and an unknown participant
contributes an empty set, so ignorance produces *no* filtering rather than
maximal filtering.

### Feature flags

| Flag | Default | Effect |
|---|---|---|
| `AILOCAL_TOOL_GATEWAY` | `off` | `off` / `report` (measure, never mutate) / `filter` |
| `AILOCAL_TASK_NEGOTIATION` | `off` | Phase D task classification |
| `AILOCAL_SESSION_LEDGER` | unset | Session observation |
| `AILOCAL_TOOL_GATEWAY_CAPTURE` | unset | Dump real payloads |

An unrecognised mode is **reported and treated as off**, never silently coerced:
a typo'd env var that quietly disables a safety layer is indistinguishable from
the layer working.

---

## Measured results

**[REAL]** Replaying the real captures through the real registry, bytes reaching
the model:

| Client | Reachable | After drop | After rewrite | Cut |
|---|---|---|---|---|
| Claude Code | 104,202 | 30,403 | **27,917** | **73.2%** |
| Codex | 11,220 | 9,198 | **8,879** | **20.9%** |

**[REAL]** Phase D, same payload, by task class:

| Task | Class | Tools | Bytes | Cut |
|---|---|---|---|---|
| *(Phase B only)* | — | 41 | 27,917 | 73.2% |
| "fix the typo in parser.py" | `simple_edit` | **9** | **9,808** | **90.6%** |
| "where is the retry logic?" | `explore` | 41 | 27,917 | 73.2% |

Honest reading: the Phase D gain is concentrated in `simple_edit` (~10× fewer
tokens). Other classes need `search`+`lsp`, which is most of what Phase B kept,
so **Phase D is not a general win.**

**[REAL]** End-to-end, real `claude-local`, 4 runs, order flipped between rounds:

| Arm | Tools received | Latency |
|---|---|---|
| unfiltered | 61 / 104,202 B / ~23,937 tok | 136.4 s, 128.8 s |
| filtered | 41 / 30,403 B / ~6,853 tok | 39.9 s, 5.2 s |

All four produced the correct answer via the model's own tools. Every filtered
run beat every unfiltered run by ≥3.2×, ranges non-overlapping, and filter won
even running first and cold. **Not claimable:** a precise factor — the filter
arm's own spread (5.2–39.9 s) is too wide at n=2.

**[REAL]** Overhead: 0.33–0.53 ms warm; ~42 ms on the first request (tiktoken
loads its encoding lazily).

**[REAL]** Warmup, genuinely cold: `ailocal-architecture` 3842→1364 ms,
`ailocal-completion` 1701→317 ms, grepai 164→47 ms.

---

## Two ways the numbers could lie

### Credit for work LiteLLM already does

Codex's three largest declarations (`multi_agent_v1`, `mcp__lsp`, `mcp__grepai` —
27,168 B of 38,388) are `namespace`-typed, and LiteLLM discards those before the
backend (`transformation.py:1309`). Counting them would claim a **71%** Codex
reduction that does not exist; the truth is **18%** from drops.

So the gateway reports `bytes_reachable`, `bytes_prefiltered_by_litellm`, and
`bytes_dropped_moot` separately, and **every ratio uses `bytes_kept_reachable`**.
Getting this wrong once produced a **−133.7%** "reduction".

The **counts** had the same defect until 2026-07-29: `tools_kept` was measured
before LiteLLM's route translation, so a Codex request read `tools_kept: 14` while
two of those entries (`mcp__grepai`, `mcp__lsp`) were discarded microseconds later
and never reached the model. That single misleading number cost two misdiagnoses.
`tools_kept` now means **forwarded** — survived the gateway *and* the translation:

| field | stage |
|---|---|
| `tools_in` | declared by the client |
| `tools_dropped` / `dropped_names` | removed by **this gateway**, with groups |
| `tools_kept_by_gateway` | survived the gateway (pre-translation) |
| `tools_killed_by_translation` / `killed_by_translation` | removed by **LiteLLM** afterwards — each entry names itself, its `type`, and the reason |
| `tools_kept` | **actually forwarded to the backend** |

`tokens_est_kept` counts forwarded tools only, for the same reason. On a route
that discards nothing, `tools_kept == tools_kept_by_gateway`.

> **[REAL] Independent finding:** through Codex, `mcp__lsp` and `mcp__grepai`
> never reach the local model at all. Configured ≠ available.

### Token honesty

`litellm.token_counter` selects cl100k even for an `ollama_chat/qwen3-coder`
deployment. The estimate was calibrated against Ollama's
real `prompt_eval_count`: **1.009–1.021**, so the estimate under-counts by 1–2%.
Every record is labelled `cl100k-proxy`.

---

## Verification pipeline

Two halves, split across the container boundary because the proxy cannot see the
repository. `session_observer.py` records the ask and the tool calls;
`verify-session.py` pairs that with the git delta and an optional test.

The observer is small because **agent clients are stateless over HTTP and resend
the whole conversation each turn** — one pre-call observation carries the complete
history. No response hooks, no stream buffering, no turn correlation.

| Verdict | Meaning | Exit |
|---|---|---|
| `VERIFIED` | Mutating tools ran, tree changed, test passed | 0 |
| `PARTIALLY_VERIFIED` | It happened; something is unresolved | 0 |
| `UNVERIFIED` | No evidence either way | **3** |
| `SUSPICIOUS` | A claim with no substance | 2 |

`UNVERIFIED` is **3, not 0**. A verification layer whose "could not check" is
scriptable as success is worse than no layer.

### What it refuses to conclude

- **No causation.** A delta proves the tree changed, not who changed it.
- **`SUSPICIOUS` names four causes**, including "blocked by a permission or
  sandbox layer", and says the last two are indistinguishable from here.
- **Ambiguous mutators** (`Bash`) with a clean tree are `UNVERIFIED`, never
  `SUSPICIOUS` — a shell command can legitimately be read-only.
- **"Not a git repo" returns nothing, not an empty delta.**
- **On `/v1/chat/completions` a tool result's status is `None`, not success** —
  that route has no error flag.

That restraint paid immediately. The first real session showed `Edit` running
against an unchanged tree, which looks exactly like fabrication. The true cause
was a fourth possibility: a non-interactive `claude -p` cannot be granted write
permission, so the tool was blocked by the harness. The model's `Edit` call was
correct on the first attempt, and it *noticed the error and retried* rather than
narrating success. A tool asserting "fabricated" would have been confidently
wrong about both the model and the cause.

---

## Recovery and failure modes

Everything fails open, and says why.

| Situation | Behaviour |
|---|---|
| Registry absent / malformed / no PyYAML | Distinct states; all forward unchanged |
| Unknown client | No filtering — guessing breaks load-bearing tools |
| Unknown model | Passthrough |
| Unnamed tool entry | Never dropped |
| Negotiation raises | Caught; request forwarded |
| Bad mode value | Reported, treated as `off` |

**Protected tools** survive every rule. `web_search` is protected because
LiteLLM's `websearch_interception` rewrites it into a SearXNG call — remove it and
search fails *silently*. Codex sends it as a nameless `{"type":"web_search"}`,
which is why unnamed entries are protected implicitly. An earlier revision dropped
it; caught by replaying a real capture, not by a unit test.

**Hook ordering:** registered **last**, after `websearch_interception`, so
interception always sees the client's full tool list.

---

## Known limitations

- **[REAL]** `async_pre_call_hook` does not fire for `/mcp/` tool calls —
  LiteLLM's local MCP registry dispatch bypasses all hooks
  ([#25011](https://github.com/BerriAI/litellm/issues/25011)). Harmless today
  (Claude Code drives MCP client-side), but a move to LiteLLM-hosted MCP would
  silently escape the gateway.
- **[REAL]** LSP cannot be warmed server-side: the language server is spawned by
  the *client* as an MCP subprocess.
- **[HYPOTHESIS]** The VS Code client profile is unvalidated. Route and headers
  are proven; no real session has been captured, so it drops nothing.
- **[HYPOTHESIS]** Description truncation is implemented and **disabled**.
  Descriptions are how a model knows what a tool does; enabling it needs an A/B on
  task success, not on bytes.
- **[REAL]** Sibling modules must be loaded by path relative to `__file__`.
  LiteLLM loads callbacks via `spec_from_file_location`, which does not put the
  module's directory on `sys.path` — a plain sibling import takes the container
  down at boot.

---

## Verification

**The gate:** `./scripts/test-all.sh` — ten checks, one exit code. Run it before
every commit; add `--full` for the end-to-end client benchmark. A stopped or
unhealthy proxy **fails** the gate rather than reducing it, because several suites
need PyYAML and the registry, which exist only inside the image. It also asserts
that every registered hook actually imports inside the proxy image — a
registered-but-unimportable callback takes the container down at boot, which has
happened here.

Individual suites:

```
./scripts/test-capability-registry.sh      # registry + the no-hard-coding assertion
./scripts/test-tool-gateway.sh             # negotiator, byte accounting, modes
python3 scripts/test-session-observer.py   # three dialects
./scripts/test-verify-session.sh           # all four classifications + exit codes
./scripts/test-client-compatibility.sh     # 3 dialects x 3 modes
python3 scripts/test-persona-injection.py
ailocal sync                   # must be idempotent
./scripts/preload-model.sh
python3 scripts/gateway-metrics.py --since 30m
./scripts/benchmark-tool-gateway.sh        # RUNS=2+ for a latency claim
```

Byte figures are pinned to **hand-written canonical JSON literals**, not to the
gateway's own encoder. Registry-dependent suites run **inside the proxy image**
and exit non-zero if PyYAML is missing rather than reporting green over a reduced
set.

---

## Future extensions

- Capture a real VS Code payload and validate that profile (closes the one
  `HYPOTHESIS` in the client layer).
- A/B description truncation on task success.
- More benchmark rounds to narrow the latency claim.
- Model routing: the registry already knows `local_nonagentic` is measured
  non-agentic, so a task needing a multi-step loop could be routed to
  `local_agentic` automatically. Deliberately not built — routing a request to a
  different model than the client asked for is a materially different contract.

---

## Client capability matrix


State of every supported client, as measured on **2026-07-28**; the Codex rows
re-verified **2026-07-29**.
Versions: Claude Code 2.1.220 · codex-cli 0.146.0 · LiteLLM 1.93.0 · mcpls 0.3.7
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
| *New client (template)* | — | see ADR 009 | — | — | — | — | — | — |

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
  [PR #17556](https://github.com/openai/codex/pull/17556) — the fix, still
  unreleased: absent from 0.146.0 (latest stable) and from 0.147.0-alpha.1.
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
to end. See ADR 007.

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
and a bridge would duplicate them. Model routing via the
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
ailocal models                          # capability -> backend -> status
ailocal e2e claude
ailocal e2e codex         # re-run after any Codex upgrade
ailocal e2e vscode
bash <cadence>/scripts/verify-lsp.sh    # per-language LSP
codex mcp list                          # MCP registration (per root via CODEX_HOME)
```

Run these on an **idle machine**. Contention produces phantom failures — it
false-failed client compatibility three times during this project's development.
