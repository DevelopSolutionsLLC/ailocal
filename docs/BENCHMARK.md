# Benchmarking

A developer utility for comparing models and measuring the effect of runtime
changes. It is not installed by `install.sh`, never runs in the request path,
and never modifies a profile.

```sh
ailocal benchmark <models|planner|gateway> [options]
```

---

## Suites

### `models` — evaluate candidate models

Runs external evaluation engines against candidate models through the proxy,
using temporary `bench-*` aliases that never touch production capabilities.

```sh
ailocal benchmark models plan       # what would run, and against which models
ailocal benchmark models setup      # install the evaluation tooling
ailocal benchmark models run        # score the candidates
ailocal benchmark models probe      # operational rates: cold load, prefill, decode
ailocal benchmark models report     # render a completed run
ailocal benchmark models doctor     # check the benchmark environment
```

`probe` measures throughput at one fixed prompt size. It does not sweep context
sizes, so it cannot characterise how prefill degrades as a prompt grows.

### `planner` — blinded planner comparison

Runs candidate models over a fixed planning scenario with the model identities
hidden until scoring is locked.

```sh
ailocal benchmark planner --dry-run          # verify without touching a model
ailocal benchmark planner --candidate a      # run one candidate
```

It refuses to act with no arguments: a benchmark that infers what you meant is
a benchmark you cannot trust. Prompts and the rubric are hash-locked, so an
edit mid-run fails closed rather than silently changing what was measured.

### `gateway` — tool-gateway A/B

Runs the same task through a real client twice, once with the gateway
measuring only and once filtering, so the payload reduction can be compared on
identical work.

```sh
ailocal benchmark gateway
RUNS=2 ailocal benchmark gateway     # interleaved rounds
```

---

## Results

Everything lands under `$AILOCAL_STATE/benchmark/`:

```
runs/        one directory per run: scores, timings, manifest
evidence/    captured proxy logs, hashed
tool-gateway/  gateway A/B output
```

Nothing is written into the repository, and nothing is committed.

Evidence is redacted before it is persisted: keys, bearer tokens and model
identities used for blinding are removed, and a marker is left where content
was replaced.

---

## Reproducibility

A result is only meaningful alongside the conditions that produced it. Every
run records the manifest that fixes them: profile tier, alias geometry, prompt
and rubric hashes, permission contract, and the resolved candidate set.

To reproduce a run, use the same profile tier and the recorded run manifest.
Changing the profile changes the geometry, which changes the result.

Benchmark candidates live in `benchmarks/benchmark.yaml`. Production model policy
lives in `profiles/` and is never modified by a benchmark.

---

## Interpreting results

**Rates are end-to-end.** They include HTTP, queueing and the proxy. Ollama's
internal counters are not exposed through LiteLLM, so these are not native
model throughput and must not be quoted as such.

**Prefill degrades super-linearly with prompt size.** A rate measured on a short
prompt does not predict behaviour at large context; the same model can be fast
at 8K and take minutes at 80K. Compare only measurements taken at comparable
context sizes.

**Results are hardware- and runtime-specific.** They describe one machine, one
Ollama version and one profile. They are not portable performance claims.

**Correctness before speed.** A faster model that answers incorrectly does not
win; efficiency only separates candidates whose correctness is comparable.

**One run is not a measurement.** Use repetitions, and treat a single number as
a hint rather than a result.

---

## Boundaries

- Benchmarks never modify a profile, an alias served to a client, or any
  generated production artifact.
- Temporary `bench-*` aliases are installed for a run and removed afterwards;
  a routing check verifies the candidate actually served the request before a
  score is recorded.
- The planner suite runs candidates inside confined worktrees with a read-only
  permission contract; it refuses to run where confinement cannot be verified.
- Benchmark tooling installs under `~/.local/share/ailocal/benchmark/`, separate
  from runtime state.
