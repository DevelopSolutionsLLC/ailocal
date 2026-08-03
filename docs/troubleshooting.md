# Runtime issues: root causes and evidence

Two client-visible failures, both investigated by instrumenting the wire rather
than by inference. Labels: **[REAL]** measured here, **[VERIFIED]** measured here
*and* matched to an upstream report, **[HYPOTHESIS]** not yet tested.

---

## 1. Claude Code: "API error" while every layer reports success

**Status: root cause [REAL]. Mitigated by the gateway, which must be enabled.**

### Evidence

Identical real 61-tool captured payload, same model, same warm cache, only
`AILOCAL_TOOL_GATEWAY` toggled:

| Gateway | Time to first streamed byte |
|---|---|
| `off` | **95.4 s, 88.6 s** |
| `filter` | **1.0 s, 0.18 s** |

Reproduce: time the first streamed byte against the proxy directly.

### What is happening

For ~90 seconds LiteLLM sends **nothing**. Ollama is prompt-evaluating 24,448
tokens of tool schemas before the model emits its first token. HTTP is 200, the
gateway completed, tool repair found nothing, and the SSE that eventually arrives
is well-formed. A client-side first-byte or idle timeout below that threshold
therefore surfaces as "API error" with **no failing component anywhere in the
stack** — which is exactly why this resisted diagnosis.

The intermittency is explained by the same mechanism: the wait scales with
payload size, whether the model is resident, and whether task negotiation
classified the request into a smaller tool set.

### Ruled out first, with positive evidence

- **SSE protocol on `/v1/messages` is correct.** Raw capture shows the full
  `message_start → content_block_start → content_block_delta →
  content_block_stop → message_delta → message_stop` sequence with
  `stop_reason: end_turn`.
- **Tool streaming is correct.** A real tool call produced a `tool_use`
  content block and a **complete** `input_json_delta`
  (`{"file_path": "/tmp/..."}`) with `stop_reason: tool_use`. This specifically
  rules out [litellm#25561](https://github.com/BerriAI/litellm/issues/25561),
  which drops tool arguments and would be fatal to Claude Code's local argument
  validation.
- **[claude-code#54434](https://github.com/anthropics/claude-code/issues/54434)
  is a different bug.** That one stalls with **no** `message_stop`; every capture
  taken here contains one. a first-byte timing check covers slowness and malformation
  separately so the two can never be conflated.

### Consequence

The tool gateway is not only an efficiency feature — it is the difference between
a ~90 s silent wait and a sub-second first byte. It ships **off** by default, so
a fresh clone reproduces the failure. Enable it:

```
AILOCAL_TOOL_GATEWAY=filter    # in .env
```

### Still open [HYPOTHESIS]

The user reports intermittent errors *with filtering enabled*. Filtering removes
the dominant cause but a residual one may exist — long sessions grow the message
history independently of tools. Not yet characterised; needs a failing-request
capture with the tracing described in `docs/architecture.md`.

---

## 2. Codex: finishes a task, then waits forever

**Status: root cause [VERIFIED] — an upstream LiteLLM spec bug. Not fixed here.**

### Evidence measured here

Raw SSE captured from both routes on the same proxy:

| Route | `event:` lines |
|---|---|
| `/v1/messages` | **6** |
| `/v1/responses` | **0** |

The `/v1/responses` stream carries `response.created`, `response.in_progress`,
`response.output_item.added/done`, `response.content_part.added/done`,
`response.output_text.delta/done`, `response.completed` and a final
`data: [DONE]` — but **every one arrives as a bare `data:` frame with no
preceding `event:` line.**

Confirmed in LiteLLM 1.93.0's own source: the Anthropic adapter emits
`payload = f"event: {event_type}\ndata: {json.dumps(chunk)}\n\n"`
(`llms/anthropic/experimental_pass_through/.../streaming_iterator.py:787`),
while the Responses endpoint has no equivalent.

### Matching upstream reports

- [litellm#27442](https://github.com/BerriAI/litellm/issues/27442) — "Streaming
  SSE output differs from upstream for /v1/messages and /v1/responses":
  LiteLLM omits the `event:` header for `response.output_text.delta` and
  `response.completed`, "affects clients (like Codex) that strictly validate SSE
  format".
- [litellm#20975](https://github.com/BerriAI/litellm/issues/20975) — "Responses
  API streaming omits necessary SSE event types".
- [litellm#29254](https://github.com/BerriAI/litellm/issues/29254) — Codex CLI
  disconnects on `/v1/responses` streaming: "non-standard SSE event types that
  Codex CLI cannot parse correctly".

### Why this produces the exact symptom

Codex receives and renders the assistant's text, because the payload is in the
`data:` frames. What it never receives is a **named terminal event**, so the turn
is never marked complete and the CLI sits waiting — no question pending, no tool
pending, no error. Typing `continue` starts a new turn, which is why that
appears to "unstick" it.

### Not fixed here, deliberately

The defect is in LiteLLM's SSE framing, not in this codebase. A local workaround
would mean rewriting the proxy's SSE output for `/v1/responses` from a streaming
hook. That is possible — `tool_repair.py` already rewrites Anthropic SSE — but it
would be a fork of upstream framing behaviour maintained here indefinitely, and
the project rule is to prefer an upstream fix over a bespoke one.

**Action:** track #27442. Re-test after any LiteLLM upgrade with:

```
curl -sN .../v1/responses -d '{"stream":true,...}' | grep -c '^event:'
```

Non-zero means the upstream fix has landed.

### What this does NOT affect

Codex's non-streaming path and its ability to edit files are unaffected —
`validate-codex-e2e.sh` shows it fixing a real bug across 10 `exec_command`
calls with the test suite passing afterwards.

## Architecture route stalls after 10-15 minutes

**Diagnose first:** `ailocal doctor` — the "Architecture route" section reports
whether the model is loaded, whether a generation is still running from a
disconnected client, memory, and swap.

This is **not** a crash, not an outage, and not memory. Measured on the 64 GB
profile: memory stayed 26-57% free, swap flat, no OOM, no jetsam, no container
restart, and Ollama never exited.

**What actually happens.** Cold prompt evaluation on this route is *super-linear*
— it gets slower per token as the prompt grows [REAL, measured]:

| Prompt tokens | Cold prompt eval | Throughput |
|---|---|---|
| 27,791 | 85 s | 326 tok/s |
| 57,791 | 341 s | 170 tok/s |
| 87,791 | **789 s (13.2 min)** | 111 tok/s |

A turn that **hits** the KV cache returns in under a second. A turn that **misses**
it at large context stalls for minutes. That asymmetry is the whole symptom: a
session works fine, grows, and then one cache miss takes 13 minutes.

The client used to give up first, on its own undocumented default, while LiteLLM
waited its full 900 s and Ollama kept generating into a closed socket —
`aborting completion request due to client closing the connection` in
`~/.ollama/logs/server.log`. `API_TIMEOUT_MS` is now pinned to the proxy's 900 s
so both ends wait the same bounded time, and a gate check enforces that they
never diverge again.

**Safe operating envelope (measured, 64 GB).**

| | Tokens | Cold first byte |
|---|---|---|
| Comfortable | up to ~28K | under ~90 s |
| Acceptable | up to ~58K | under ~6 min |
| At the limit | ~88K | ~13 min |
| Configured maximum | 98,304 | exceeds the 900 s budget |

The configured 98,304 is a real capability ceiling, not a working target. Long
architecture sessions are cheap while the cache holds and expensive the moment it
does not, so **the practical fix is to start a fresh session rather than let one
grow past ~60K tokens**.

**Parallelism is not the cause.** Measured at `OLLAMA_NUM_PARALLEL` 1 and 2, cold
prompt-eval throughput differed within noise (170 vs 182 tok/s at ~55K) and
resident size was 26.85 GB either way, so the configured value of 2 is unchanged.

**If a request is abandoned**, `ailocal doctor` names the task and how long it has
been evaluating. It holds the KV slot; either let it finish or
`ailocal stop && ailocal start`.

### Automatic compaction — measured, not assumed

Verified in a live `claude-local` session (Claude Code 2.1.220), not from
configuration:

| | Value |
|---|---|
| Session | `7b3d36fa` |
| PreCompact | `trigger: auto` |
| PostCompact | `trigger: auto`, 5,959-char summary |
| Compaction duration | 123 s |
| Context after | 41,617 tokens |
| Next-turn TTFT | **10.4 s** |

All controlled state survived verbatim — objective, the accepted decision *and*
the rejected one, modified file, `pytest -q` → `3 passed`, the unresolved task,
and the git branch. The rejected hypothesis stayed rejected.

**Compaction runs on the MAIN model, and cannot currently be handed to a faster
one.** Measured: every request during compaction went to `ailocal-architecture`
(20/20, then 14/14 in a second run). Setting `ANTHROPIC_SMALL_FAST_MODEL` and
`ANTHROPIC_DEFAULT_HAIKU_MODEL` to `ailocal-fast` routed **no** traffic there.
That 123 s is therefore a real, unavoidable cost of a 30B model summarising ~41K
tokens, and it is paid once per compaction rather than per turn.

The lever that does work is the threshold: compaction cost is dominated by
reading the conversation it summarises, so compacting **earlier** (a lower
`pct`) means less to read. Trading interactive headroom for a shorter pause is a
profile edit, not a code change.

**Do not diagnose a stalled `claude -p` as a trust prompt.** First-run project
trust is disabled in non-interactive `-p` mode, and a full `stream-json` trace
contains zero trust or permission events. A slow first turn is the cold model
load plus prompt eval — 126 s measured for a trivial prompt.

### The "~55K startup baseline" was FALSIFIED — startup is ~18.2K

A previous session reported a ~55,000-token startup baseline and concluded the
49,152 compaction trigger sat *below* it, i.e. that compaction could fire before
any useful conversation. **That premise was wrong and the investigation closed
`NO_DEFECT_FOUND` on 2026-08-03.** Do not re-derive it.

The 55,104 figure came from a **three-turn file-editing workflow** (read
`calc.py`, attempt an Edit, tool results) — conversation accumulation, which is
precisely what compaction exists to handle. It was never a startup measurement.

Measured startup, Claude Code 2.1.220, paired captures via
`AILOCAL_TOOL_GATEWAY_CAPTURE`:

| Condition | Input tokens |
|---|---|
| Minimal (`--strict-mcp-config`, no MCP) | **11,212** |
| **Normal default startup** | **18,189** |
| Trigger (`65536 × 75%`) | 49,152 — **2.7× above startup** |
| Architecture `context_input` | 81,920 |

Growth measured over a controlled 5-turn resumed session: **~65 tokens/turn**
(18,215 → 18,477), with the planted codeword recalled correctly at the end.
`COMPACTION_NOT_TRIGGERED`, `SESSION_STATE_PRESERVED`.

#### Startup token budget by owner (±2% tolerance)

| Source | Tokens | Owner |
|---|---:|---|
| Claude Code built-in tool schemas (18) | 6,628 | Claude Code |
| GitHub MCP (12 tools) | 4,594 | ailocal config |
| grepai MCP (11 tools) | 1,935 | ailocal config |
| Generic engineering instructions | ~2,172 | **Cadence** |
| Claude system prompt + environment | ~2,400 | Claude Code |
| SessionStart hook `additionalContext` | 82 | ailocal |
| Residual / tokenizer variance | ~368 | — |

Tool counts are the gateway's `cl100k-proxy`; the total is Anthropic's
authoritative count, so the residual absorbs tokenizer disagreement. Repo
`CLAUDE.md`/`AGENTS.md` were not loaded (test ran outside the repo); in-repo
sessions carry ~3,243 more from `AGENTS.md`.

#### The tool gateway is already doing the work

| | tools | bytes | tokens |
|---|---|---|---|
| Claude Code declares | 54 | 112,871 | **25,828** |
| After gateway | 41 | 50,819 | **11,279** |
| **Removed** | **13** | 59,966 | **14,549 (56%)** |

Dropped: `CronCreate/Delete/List`, `DesignSync`, `Enter/ExitWorktree`, `Monitor`,
`PushNotification`, `ReportFindings`, `ScheduleWakeup`, `SendMessage`, `Skill`,
`Workflow` — orchestration a local model cannot drive. **Working as designed.**
Do not remove GitHub, grepai or Cadence merely to reduce the number.

#### Cadence: available by design, NOT contaminating

`~/.config/ailocal/claude/CLAUDE.md` is header-stamped `Generated by Cadence |
owner: Cadence | safe to edit: NO`, matching the seam documented in `AGENTS.md`
("client instruction files — those are Cadence's"). Content observed was generic
engineering policy. **Zero** Cadence MCP servers (only `github`, `grepai`), zero
Cadence-named agents, zero Cadence mentions in hook output. This run did not
demonstrate contamination.

This is a separate question from the earlier benchmark filesystem-escape
finding, which concerned unconfined fixture access in an evaluation context and
says nothing about normal instruction content.

#### Status: `COMPACTION_CONFIGURATION_PRESENT_RUNTIME_TRIGGER_UNPROVEN`

The threshold was never reached in this investigation, so `65536 × 75%` is not
proven to be the governing runtime rule — though the live session above
(`7b3d36fa`) does prove auto-compaction fires and preserves state. **No settings
were changed.** Revisit only when: a real interactive session compacts
prematurely; a session loses state after compaction; a Claude Code version
changes compaction semantics; a profile context change requires regenerating the
threshold; or a reproducible workload reaches the trigger naturally. Do not
manufacture a long session to force it.
