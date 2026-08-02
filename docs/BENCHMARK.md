# Benchmarking local models

An optional developer extension. It is not installed by `install.sh` or
`update.sh`, it never runs in the request path, and nothing it produces is
committed. `docs/architecture.md` covers the runtime itself.

The question it answers: **which model should back each ailocal capability, at
which usable context, in which reasoning mode, at what cost in latency and
memory.**

## What ailocal owns

Orchestration, and nothing else:

- profile-driven model selection and tier ladders
- temporary authenticated LiteLLM aliases, created and torn down per run
- vendor sampling presets and reasoning-mode mapping
- crash-safe runtime restoration
- telemetry: residency, cold-load time, throughput, memory, thermals
- validity classification

Datasets, prompting, scoring and statistical treatment belong to external
tools — lm-evaluation-harness, EvalPlus, RULER. There is no ailocal scorer, and
adding one would be a regression.

## Transport

```
lm-eval → authenticated LiteLLM → temporary bench-* alias → Ollama → model
```

Every scored request goes through LiteLLM. Direct Ollama is permitted only for
`/api/ps`, `/api/show`, `/api/tags`, unload and health — never for anything
scored.

Vendor presets are **baked into the alias**, not sent per request, because
LiteLLM ignores client-supplied sampling parameters. Measured: `max_tokens` of
50 and of 300 both returned 1,492 tokens.

Three transport classes:

| Class | Aliases | Scored |
|---|---|---|
| `MODEL_COMPARISON` | temporary `bench-*` | yes |
| `PROFILE_CERTIFICATION` | production `ailocal-*` | yes |
| `OLLAMA_DIAGNOSTIC` | none | never |

## The output ceiling

Every alias requests `num_predict = 32768`. It is a maximum, not a target —
short answers still stop naturally. It is never reduced to make a model fit.
A model that cannot hold input + 32,768 output is classified
`UNSUPPORTED_TOTAL_WINDOW`; a response that actually reaches the ceiling is
`INVALID_TRUNCATED`. Neither is scored.

## Usage

```sh
scripts/benchmark-models plan   --profile 64gb --suites coding2
scripts/benchmark-models run    --models qwen3.5:4b --suites coding2 --limit 40
scripts/benchmark-models probe  --models gemma4:26b-mlx
scripts/benchmark-models client --scenario smoke
scripts/benchmark-models doctor
scripts/benchmark-models report
```

`plan` prints the full model × mode × suite × context matrix and its runtime
estimate before any inference happens. Run it first; it is the cheapest way to
notice a matrix that accidentally expanded into hundreds of cells.

Results land in `$XDG_STATE_HOME/ailocal/benchmark/runs/` — outside Git, by
design.

## Interpreting results

Three habits, each learned from a defect that cost real time:

**A zero is a harness bug until proven otherwise.** Six models scored 0.000 on
`humaneval_plus`. Every one of them was writing correct Python; the task is a
completion task being driven through a chat interface. MBPP+ failed separately,
against five open upstream issues. `gemma4:26b-mlx` — the current 64 GB
production model — scored 0.000 while emitting flawless code, because the
extractor cut at the first fence and gemma4 opens its own. Audit the raw
responses of any floor result before believing it.

**Overlapping error bars are not a ranking.** At n=40, 0.500 ±0.080 does not
beat 0.400 ±0.078. Report the interval or report nothing.

**Latency per correct answer, not per batch.** Use
`batch_wall / (samples × success_rate)`. A fast model with lower accuracy can
legitimately win a role; a slow accurate one can legitimately lose it.

Confidence in the *interface* is tracked separately from the score: `HIGH`
means the adapter was audited against raw responses for that task.

## Client integration

`lm-eval` bypasses the client entirely, so it cannot see session persistence,
compaction, tool routing, MCP or hooks. The `client` subcommand drives
`claude-local` and `codex-local` directly for that.

Those wrappers are zsh functions, not binaries, so they run via
`zsh -c 'source config/clients/configure.zsh; ...'`. Session resume always uses
the exact captured id — never `--continue` or `--last` — and fails closed if
the id cannot be read. Claude reports `session_id`; Codex reports `thread_id`.

Scenarios reuse one conversation on purpose: long-session and compaction bugs
are unreachable from a fresh session per turn.

`BASIC_SESSION_CONTINUITY_VERIFIED` means exact-id two-turn resume works. It
says nothing about tools, MCP, compaction or soak behaviour.

## Boundaries

- Profile YAML is never edited automatically. The benchmark produces
  recommendations; a human applies them.
- External engines are pinned and installed into a venv outside the repository.
  They are never vendored.
- Generated data stays outside Git.
