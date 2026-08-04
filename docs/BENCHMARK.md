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
scripts/benchmarks/models plan   --profile 64gb --suites coding2
scripts/benchmarks/models run    --models qwen3.5:4b --suites coding2 --limit 40
scripts/benchmarks/models probe  --models gemma4:26b-mlx
scripts/benchmarks/models client --scenario smoke
scripts/benchmarks/models doctor
scripts/benchmarks/models report
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

### Benchmark invariant: verify the served model

**A planner benchmark is INVALID unless the served model is independently
verified before candidate scoring begins.**

Client-reported identity is not evidence. A nine-turn, three-candidate run once
completed with clean sessions, zero errors and full restoration while every
request served the *production* alias — because `settings.json` pins `model`,
which outranks the `ANTHROPIC_DEFAULT_*` slot variables. The tests passed
because they asserted the override reached the client environment, which it did.
They never asserted which model answered.

Verified precedence (code.claude.com/docs/en/settings):

    --model  >  settings.json "model"  >  ANTHROPIC_DEFAULT_*_MODEL

`verify_routing()` sends one probe through the real client and then reads
LiteLLM's own log for the exact `bench-*` alias. A mismatch is
`INVALID_ROUTING`: abort, clean up, restore — never continue.

Related instrumentation note: `claude -p --output-format json` emits a single
result object and reports **zero tool calls even when the model read files**.
Do not read that as "the repository was not inspected" — determine repository
usage from response evidence and input-token counts instead.

### Role alias overrides (advanced diagnostic)

Point one client role at an explicit, already-existing LiteLLM alias for a
single process:

```sh
AILOCAL_ARCHITECTURE_ALIAS_OVERRIDE=bench-gemma4-26b-mlx-off-32k claude-local
```

Also `AILOCAL_IMPLEMENTATION_ALIAS_OVERRIDE`, `AILOCAL_REVIEW_ALIAS_OVERRIDE`
and `AILOCAL_FAST_ALIAS_OVERRIDE`.

- **Defaults stay profile-controlled.** With no override set, the generated slot
  block is used verbatim and behaviour is byte-identical to before.
- **Process-scoped, never persisted.** Nothing is written; a new shell is back
  to profile defaults.
- **The alias must already exist.** An override LiteLLM does not serve aborts
  the launch rather than falling back — a silent fallback would measure the
  production model while reporting the candidate's name.
- **Only one role changes.** Overriding architecture leaves implementation,
  review and fast untouched.

Useful for client-compatibility testing and model comparisons. It exists because
the slot names are generated and applied through `env`, which overrides the
inherited environment — so there was otherwise no supported way to run
`claude-local` with one role on a different model. Defining a second alias named
`ailocal-architecture` is not an alternative: LiteLLM would hold a duplicate
`model_name` and choose between two backends ambiguously.

## Boundaries

- Profile YAML is never edited automatically. The benchmark produces
  recommendations; a human applies them.
- External engines are pinned and installed into a venv outside the repository.
  They are never vendored.
- Generated data stays outside Git.
