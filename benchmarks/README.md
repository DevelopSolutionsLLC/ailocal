# ailocal model benchmark suite

Reusable, resumable measurement of the installed local models. Every number is
produced on this machine; nothing here is copied from a model card or a blog.

    ./scripts/benchmark-models plan                    # capability probe + run plan
    ./scripts/benchmark-models run --suite throughput  # Layer A raw inference
    ./scripts/benchmark-models resume                  # completed runs are skipped
    ./scripts/benchmark-models report                  # JSONL -> CSV + Markdown

Filter with `--model`, `--context`, `--reasoning`, `--variant`.

## Layout

    manifest.json      models, contexts, reasoning modes, fairness params, limits
    runner/common.py   native counters, machine safety, checkpointed result store
    runner/probe.py    capability discovery — what is actually SUPPORTED
    runner/fixtures.py deterministic repo-like prompts, calibrated to ±1%
    runner/throughput.py  Layer A
    runner/report.py   CSV + Markdown
    reports/           generated output (git-ignored except this README)

`manifest.json` is JSON rather than YAML deliberately: PyYAML is absent on this
host, the repo's yaml-dependent tests run inside the LiteLLM container to get
it, and this harness cannot — it must control Ollama residency and read host
memory and swap, neither of which is visible from a container.

## Measurement rules

**tok/s comes from native counters, never wall-clock.**
`prompt_eval_count / prompt_eval_duration` and `eval_count / eval_duration`.
Wall-clock is recorded separately as request latency.

**Warm means the MODEL is resident, never that the PROMPT was cached.** Each
repetition uses a novel prompt of the same calibrated size. Re-sending an
identical prompt reuses the KV cache: the first version of this harness measured
**837,866 tok/s** that way. This repository has already published a wrong
1705 tok/s figure from the same mistake (`config/profiles/64gb.yaml`).

**Token counts are measured, not estimated.** Characters-per-token varies by
model and content, so fixtures are calibrated against the backend's own
`prompt_eval_count` until within ±1% of target — per model, since tokenizers
differ. A cell that misses the target is reported as off-target rather than
compared as though it hit.

**Cold and warm never mix.** The model is evicted and its absence verified
before a cold run; `load_duration` is reported separately.

**Capability is proven, not assumed.** A reasoning mode counts as supported only
when changing it changes observable reasoning output. `reasoning_effort` was
accepted and silently dropped on this stack for months while the config claimed
thinking was controllable — the probe distinguishes *unsupported*, *ignored*,
and *effective*.

## Statistics

Median with min/max and dispersion; a cell with >25% spread is flagged
unstable rather than presented as precise. Errors and unsupported combinations
are reported separately from a score of zero.

## Safety

Runs on a 64 GB Apple Silicon machine. One large model at a time; models are
unloaded between cold tests; free-memory, swap-growth and disk thresholds abort
the run. The harness records `iogpu.wired_limit_mb` and other system settings but
never changes them.

## Benchmark sources

The public suites below define the task formats and scoring methodology used by
the quality tasks. Datasets are NOT vendored into this repository.

- SWE-bench / SWE-bench Verified — https://github.com/princeton-nlp/SWE-bench
- LiveCodeBench — https://github.com/LiveCodeBench/LiveCodeBench
- CRUXEval — https://github.com/facebookresearch/cruxeval
- RepoBench — https://github.com/Leolty/repobench
- Ollama API — https://github.com/ollama/ollama/blob/main/docs/api.md
- LiteLLM — https://docs.litellm.ai/

## Status

Layer A (raw inference) is implemented and validated. Layer B (agent) and the
quality task suites (architecture / implementation / fast coding / review /
understanding / self-repair) are specified in the parent task and **not yet
implemented**; see the run plan for what has actually been measured.
