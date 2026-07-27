# The tool gateway

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
payload (`scripts/replay-tool-captures.py`), not by a unit test.

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

`scripts/calibrate-tokens.py` measures the error by sending the real captured
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
./scripts/replay-tool-captures.sh     # real captures through the real policy
python3 scripts/calibrate-tokens.py   # token estimate vs Ollama ground truth
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
