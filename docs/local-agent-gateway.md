# The Local Agent Gateway

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
