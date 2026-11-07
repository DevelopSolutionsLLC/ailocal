# Maintenance

For maintainers changing dependencies, installation, models, skills or client/tool integrations, and for cutting a release. Findings and unfinished work go in GitHub issues, never a local tracker. Cadence supplies the shared procedures (`tool-maintenance`, `security-review`, `work-tracking`); ailocal owns its runtime and integration contract.

## Keeping the stack current

**Discover before changing.**

1. Inspect the worktree and the installed source; preserve operator configuration, credentials and unrelated edits. A matching version string does not prove checkout/installed parity.
2. `ailocal update --json` (or `--upstream-only`) lists newer candidates under the [version policy](architecture.md#version-policy); `cadence update --check --json` covers Cadence-owned tooling. Neither authorizes an upgrade. The reporter covers service images only; check Ollama, models and clients separately.
3. `ailocal audit` checks every pin against live advisories: `clean`, `affected`, `yanked` or `unverified` (a source could not answer; never read as clean). It records each run at `~/.local/state/ailocal/audit/local.json` for `cadence audit --local`; a record older than a week is unverified there. An advisory with `NO FIXED RELEASE` needs reachability triage; an OSV range that is unbounded while the GitHub advisory bounds it below the pin is database staleness, recorded as such.
4. For each affected component read the official documentation, stable releases, advisories, and open and recently closed issues for the exact version and the behaviour we rely on (streaming completion, tool-call names and arguments, deferred discovery, search interception, authentication, persistence). Read the fix, not the issue title; confirm it is in the candidate. No matching issue is not proof of compatibility. Record links and the date checked in the issue or PR.

**Weekly.** Start with `cadence doctor` and `ailocal audit`, then discovery for the components in scope. Report authentication, instruction-authority and index failures separately from application health. For every retained Ollama patch, check the upstream issue and released source: retain it with dated evidence, or validate the native replacement and remove the patch and its adapter together. A candidate release or clean version check alone does not justify changing a pin. Nothing here schedules jobs or updates applications.

**Security review of a candidate.** Cover the whole toolchain, not ailocal's empty dependency list: installed versions and container digests, MCP venvs, editor extensions, vendored JavaScript, plugins, native libraries and interpreters. Scan the running image ID with a fresh database (`trivy image --image-src docker --scanners vuln --format json IMAGE_ID`, sequentially, recording `trivy --version`), compare with a live OSV query and the publisher's advisories, and establish reachability for each match. Signatures prove publisher identity, not safe dependencies. An audit is dated evidence, never certification.

**Choose the smallest integration.** Native client capability, then a configured maintained dependency, then a narrow adapter for a demonstrated unmet requirement. Before adopting an MCP package, inspect its license, provenance, dependency graph, releases, advisories, permissions, network destinations and tools. Keep dependencies pinned by version and integrity/digest; an install must never fetch a moving branch or promote `latest`. A server useful without a local runtime belongs to Cadence, not ailocal.

**Validate the affected path.**

| Change | Required evidence |
|---|---|
| Package, Python, installer, resource layout | Build and install the artifact; `tests/installed-runtime.py`; reinstall idempotence, credential preservation, owned-file removal. A temporary-root test is not a pristine-machine install. |
| LiteLLM or client integration | A real request through each affected client → proxy → provider path: streaming terminates, tool name and arguments survive, the result returns, the model completes. Label mocks. |
| MCP server or skill | Discover and call the actual tool in each client; missing or disabled tools fail honestly; measure whether a representative model uses the skill. Schema lint is not behaviour. |
| Search | Engine selection, relevant results, domain filtering, bounded output, explicit upstream errors; SearXNG health, proxy search and a client tool call are three different things. Paid probes opt-in. |
| Models, Ollama, geometry | `measure_geometry` on the target hardware with nothing else loaded; latency, memory and tool behaviour. |
| Browser/artifact libraries | Artifact and headless-browser suites against the built assets; fail closed when validation is unavailable. |

Never fix a failing check by loosening it. Report exact commands, outcomes, omissions, versions and hardware scope.

**Promote.** Record candidate → evidence → accept/defer with a reason in the issue or PR; change only the canonical pin and regenerate; reinstall before restarting when package resources change; verify the running artifact matches the declaration; keep the previous pin as the rollback. Before pushing, run the credential sweep from Cadence's `security-review` skill. A documentation-only change is never presented as a validated dependency promotion.

## Out-of-gate suites

These take minutes, load and unload models, must run alone, and assert nothing a human does not read.

| Suite | Run when | Reads |
|---|---|---|
| `tests/installed-runtime.py` | packaging, resources or provisioning change | the wheel works without a checkout (beside an existing install, not on a clean machine) |
| `tests/measure_geometry.py [--quick]` | Ollama upgrade, model change, unmeasured tier | the numbers profiles cite |
| `tests/measure_cache.py [--quick\|--tail N]` | Ollama upgrade, model change, unmeasured tier | KV prefix reuse across turns; `--tail N` is passive (last N real requests from the Ollama log), use it right after a session. [REAL] 16 GB, `qwen3.5:4b`: a warm turn's prefill fell from ~18.6K tokens to a few dozen (~99.8% reuse, ~115× faster) through `claude-local`. |
| `./tests/artifact-routing.sh [runs]` | the artifact tool description, `alwaysLoad`/`searchHint` or the routing contract changes | an aggregate rate at which a real model calls the tool; the wording itself is asserted in-gate by `test_routing_contract.py` |
| `tests/measure_agentic.py`, `measure_refusals.py`, `measure_sampling.py` | evaluating a model or prompt | decode and tool plumbing; benign-refusal baseline (16 prompts, inspired by XSTest, not the benchmark); five scored function tasks |

The measurement drivers default to a 900-second wall-clock budget (`--time-budget`), one repetition, and release their models on exit; a timeout is an incomplete result, never a quality score. `measurement-contracts.py` checks the drivers in-gate with fixtures.

## Evaluating a model

- **Path.** Measure through `/api/chat`, matching the provider protocol; older `/api/generate` timings are a different path. Promotion needs evidence on the target hardware and the complete client → LiteLLM → Ollama path, recording digests, quantization, the `/api/ps` runner, Ollama and proxy versions, thinking and sampling, and limits. Coding success per elapsed time matters more than tokens per second. A 64 GB host does not validate smaller-tier headroom.
- **Budget.** Start with a bounded pilot (one repetition, three decode samples, three tool rounds), monitor swap, cap each subprocess, and keep a session under 30 minutes. Expand only when a decision needs it.
- **Presets.** Do not copy one Qwen preset across sizes or modes; each candidate carries its own, and `--no-think` is a mode-only ablation. Profiles declare Ollama's `repeat_penalty`, not `repetition_penalty`; LiteLLM 1.106.0 rejects `presence_penalty` unless the generator emits it in `allowed_openai_params`, which it does when the profile declares it.
- **Prompts.** Client instructions are the baseline. A repair prompt is tested against the same held-out tasks with and without it, counting its token cost. Warming (`keep_alive`) and prefix-cache reuse change startup cost, not capability. No proxy-wide persona without evidence on the complete client path.
- **Guidance.** Each profile's `model_guidance` is keyed by exact backend name and consumed by Codex's catalog, Claude's `SessionStart`/`PostModelSwitch` hooks and the agent MCP's `model_guide` (which reads `ailocal profile guide --json` per call). The hook carries limits, prompting advice and limitations; quantization, preset, draft depth and evidence stay in `model_guide` (#60). If `SessionStart` omits the model, the hook prints one line per alias and never guesses. [REAL client, APPROX inference] Claude 2.1.308 omitted the model on a headless startup. Never write model facts into client templates or apply one checkpoint's results to another.
- **Load check.** Install verifies every selected backend with an empty-message `/api/chat` load (120 s timeout, 2K context, one-second residency); no tokens or full-window geometry are measured. Failed imports keep verified assets.

## Model format

The rule for every chat backend a profile selects; `tests/policy.py` enforces the MLX clause.

1. **Require evidence beyond format conversion.** Prefer originals (an official Ollama library tag or the author's own build) or documented optimized builds: calibrated NVFP4 with `global_scale`, vendor QAT checkpoints, or calibrated GGUF with working MTP. A prebuilt GGUF through Ollama's native Hugging Face registry is eligible when its identity, calibration/MTP evidence and native trial are recorded. When a built-in renderer/parser is required, `source` declares the native pull/create recipe and expected weight SHA-256; a separate MTP head uses `draft_model`/`draft_sha256` and is attached through `draft_files`, then the importer requires the `DRAFT` line and a successful load. No tensor conversion. Changing format alone is not an enhancement.
2. **GGUF is the default.** The GGUF runner honours `OLLAMA_KV_CACHE_TYPE=q8_0`. No shipped chat role declares the patched runtime.
3. **MLX only on evidence.** An MLX build (`-mlx`, NVFP4, MXFP) is selected only when a same-model GGUF comparison on the same machine shows it at least as fast and as faithful to a higher-precision reference, recorded in the tag's `model_guidance` evidence as a `[REAL]` sentence containing `same-model GGUF comparison`.

Edited operator profiles may still select historical commit-pinned MLX snapshots: the installer fetches only model assets, verifies Git/LFS hashes and runs native `ollama create`; remove this adapter when upstream pull supports those snapshots.

### Inline completion

Shipped completion selects the official base GGUF tags, `qwen2.5-coder:1.5b-base` (16/32 GB) and `qwen2.5-coder:3b-base` (64/128 GB):

- **The proxy needs the base template.** LiteLLM v1.106.2 drops `suffix` on `/v1/completions` ([BerriAI/litellm#6900](https://github.com/BerriAI/litellm/issues/6900)) and forwards no `raw` flag, so the FIM-formatted prompt arrives as a chat message; the base template passes it through, the instruct template wraps it and infill fails.
- **MLX buys nothing.** The pinned MLX runner has no Qwen2 loader and mlx-community conversions panic without `quantization.mode` ([ollama#19135](https://github.com/ollama/ollama/issues/19135)); a reviewed loader patch with an mxfp8 import decoded slower at twice the footprint.

[REAL 2026-10-26, MacBook Air M4 16 GB under memory pressure (11 GB swap); Ollama `0.40.8-ailocal.18631.3`, `.4` for the MLX row; temperature 0] 80 held-out single-line infills from `src/` plus 10 executable FIM cases:

| Model | Path | exact | similarity | executable | p50 | decode |
|---|---|---|---|---|---|---|
| `qwen2.5-coder:1.5b-base` | proxy | 10/80 | 0.549 | 10/10 | 1.8 s | 68 tok/s |
| `qwen2.5-coder:3b-base` | proxy | 9/80 | 0.571 | 10/10 | 3.3 s | 32 tok/s |
| `qwen2.5-coder:1.5b` (instruct) | proxy | 0/80 | 0.096 | 0/10 | 2.0 s | 52 tok/s |
| `qwen2.5-coder:1.5b-base` | native | 9/80 | 0.539 | 10/10 | 2.1 s | 74 tok/s |
| `qwen2.5-coder:1.5b` (instruct) | native | 7/80 | 0.567 | 10/10 | 1.5 s | 77 tok/s |
| `qwen2.5-coder:1.5b-base-q8_0` | native | 8/80 | 0.544 | 9/10 | 2.3 s | 52 tok/s |
| `qwen2.5-coder:3b-base` | native | 9/80 | 0.571 | 10/10 | 3.2 s | 31 tok/s |
| MLX mxfp8 import, 1.5B | native | 10/80 | 0.557 | 9/10 | 2.2 s | 36 tok/s |

Differences between working rows are within noise; a repository-level header did not help; a base model takes no instructions. Per-request `max_tokens` does not bind through the proxy, so the role's `num_predict` of 512 is the ceiling, and LiteLLM's `max_input_tokens` of 3,968 rejects an oversized prompt. Ollama 0.40.13 gave the role two 4,608-token slots on 128 GB (0.32.14 halved them). The 3B base is under the non-commercial Qwen Research License; the 1.5B is Apache-2.0. 64/128 GB latency was not measured on target hardware.

## Patched Ollama

`resources/deploy/ollama/patch.toml` (read only through `policy.py`) pins the exact 0.40.14 source and signed native tarball, the upstream Gemma SwitchGLU loader patch (#18631, unmerged), the mixed-precision router, MLX stop-string and Qwen history patches ([016](decisions.md#016-qwen-tool-loop-history-keeps-the-thinking-mode-signature)), and a `go.mod`/`go.sum` security patch. Builds:

- take native files from an intact owned receipt, else from the installed `ollama-binary` cask when its CLI hash equals `native_cli_sha256`, else from the downloaded tarball verified before extraction; every copied Mach-O must pass `codesign --verify --strict` for team `3MU9H2V9Y9`; the installed cask is never modified;
- need Go, git, Xcode tools and codesign, run under a 20-minute limit, and require the SwitchGLU, mixed-precision, stop and Qwen history regressions to execute without skips before atomic promotion into `~/.local/share/ailocal/runtimes` (`AILOCAL_RUNTIMES`);
- are selected by `AILOCAL_OLLAMA_PATCH` in `.env.local`, written only after activation succeeds; an unavailable or modified selection fails explicitly.

Retire the backport when a signed upstream build supports the exact checkpoints and passes load, inference, client/tool transport, geometry and cache measurements; remove patch, adapter, option and tests together with a migration.

## Releasing

Versioning follows Cadence's `versioning` reference (SemVer, Keep a Changelog, the 0.x rule). From v0.9.0 `main` is a public contract: the CLI commands and flags, configuration layout, install flow, generated client configuration and profile behaviour. A change to any of them carries its migration note and bump in the PR that makes it.

- The version lives in `pyproject.toml` and `src/ailocal/__init__.py`; the gate fails unless they agree and `CHANGELOG.md` has a section for it.
- There is one install path, `pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git@vX.Y.Z`; the README and release notes carry it for the release being cut, and the wheel is attached as the downloadable artifact, not a second path.
- **The clean-install check is manual and the step most often skipped.** A human installs the built artifact on a Mac that has never run ailocal and confirms `ailocal check` passes; `tests/installed-runtime.py` is not a substitute. It was skipped for v0.9.1, v0.10.0 and v0.15.0; a skipped step is recorded in the release notes, never reported as passed. An automated clean-state gate (fresh `HOME`, `XDG_*` and config roots) is not built.

```sh
sed -i '' 's/ailocal.git@v[0-9.]*/ailocal.git@vX.Y.Z/' README.md
python3 tests/gate.py --full
python3 tests/installed-runtime.py
ailocal check
python3 -m pip wheel . --no-deps -w /tmp/ailocal-dist   # outside the checkout: the gate rejects untracked root entries
git tag -a vX.Y.Z -m "ailocal vX.Y.Z"
git push origin main --follow-tags
gh release create vX.Y.Z /tmp/ailocal-dist/ailocal-X.Y.Z-py3-none-any.whl --notes-file <(sed -n '/## vX.Y.Z/,/^## /p' CHANGELOG.md)
```

## Model audit

Labels: [REAL] observed through the actual runtime or client path; [APPROX] a reduced fixture, synthetic workload or estimate; [UNVERIFIED] not measured. Each row carries its measurement date and host; exact source pins live in the profiles, not here.

Shipped selections (profiles `16gb`, `32gb`, `64gb`, `128gb`):

| Tier | Primary | Fast | Cyber | Completion |
|---|---|---|---|---|
| 16 GB | `qwen3.5:4b-mtp` | resolves to primary | `qwen3.5:4b-unrestricted-mtp` | `qwen2.5-coder:1.5b-base` |
| 32 GB | `qwen3.5:9b-calibrated-mtp` | resolves to primary | `qwen3.5:9b-unrestricted-mtp` | `qwen2.5-coder:1.5b-base` |
| 64 GB | `gemma4:26b-qat-mtp` | `gemma4:26b-qat-mtp`, thinking off | `gemma4:26b-unrestricted-qat-mtp` | `qwen2.5-coder:3b-base` |
| 128 GB | `gemma4:26b-qat-mtp` | `gemma4:26b-qat-mtp`, thinking off | `gemma4:26b-unrestricted-qat-mtp` | `qwen2.5-coder:3b-base` |

### Common

| Fact | Result |
|---|---|
| Runtime, 16/32 GB | Stock Ollama 0.40.14; no role declares the patched runtime ([REAL] 2026-11-05). |
| Runtime, 64/128 GB earlier evidence | Gemma speed, coding and client rows below ran on managed `0.40.8-ailocal.18631.3` (2026-10-27); #68/#69 ran on Ollama 0.40.14 (2026-11-05). |
| Sampling, 16/32 GB chat roles | Thinking off, greedy: temperature 0, top_p 1.0, top_k 20, min_p 0, no penalties; stops `<\|im_end\|>`/`<\|im_start\|>`. Greedy closed the 4B gap vs Nemotron ([REAL] 2026-11-05). |
| Sampling, 64/128 GB chat roles | Google preset: temperature 1, top_p 0.95, top_k 64, repeat penalty 1; primary and cyber thinking on, fast thinking off. Matches Google's card (source review 2026-10-28). |
| Sampling, completion | Temperature 0.1, top_p 0.9, top_k 20, native FIM formatting. |
| Native context window | Every chat backend reports `context_length` 262144 via `/api/show` ([REAL] 2026-10-28; 4B library build re-read 2026-11-06). |
| Geometry rule | Admission is bounded by memory up to the native window; cyber total equals primary total on every tier (regression-pinned 2026-10-28). |
| MTP output identity | Two short greedy prompts per backend, MTP off vs selected depth: 8/8 identical ([REAL] 2026-10-27, the four 32/64 GB backends then selected; not distributional proof). |
| Prompt cache reuse | Strict raw continuation: Qwen 3,821/3,829 tokens, Gemma 4,488/4,497 (~99.8%) ([REAL] 2026-10-27/28; synthetic, not client history). |
| Image input | All six chat backends identified two color fixtures, 12/12, native API, 8192 context, thinking off ([REAL] 2026-10-28). |
| Proxy/client projection | Every alias served and routed; FIM excluded from chat pickers; FIM completed: 16 GB 28/28, 32 GB 28/28, 64 GB 32/32 ([REAL] 2026-10-28, 64 GB host). |
| Qwen thinking empty-turn defect | Cause: Ollama `Qwen35Renderer` writes an empty think block for un-replayed turns; renderer patch took four Qwen backends to 4/4 final replies vs 2-4/4 empty on stock ([REAL] 2026-10-29, #38). Shipped Qwen roles run thinking off. |
| Delegation and client paths | 24 bounded coding workflows across 4B, 9B, 26B standard and unrestricted through hosted Claude, hosted Codex, `claude-local`, `codex-local` completed (not 24 first-attempt successes) ([REAL client paths; APPROX fixtures] 2026-10-29). |
| Memory, all tiers | [APPROX] 2026-11-05 upper bounds: see per-model rows; `OLLAMA_MAX_LOADED_MODELS=4` holds on 64/128 GB; on 16/32 GB three loaded models and one parallel slot are recommended, not modelled per tier. `/api/ps` underreports GGUF residency and is not memory proof. |
| Unverified, all tiers | Physical 16, 32 and 128 GB hardware; filled-window quality and latency; repository-scale accuracy; VS Code GUI chat or image turn; native parent-subagent guide inheritance. |

### qwen3.5:4b-mtp (16 GB primary)

Ollama library `qwen3.5:4b-mtp-q4_K_M` (weights, MTP draft and projector attached by the library build), imported natively under the structured name; draft depth 1.

| Measured | Result |
|---|---|
| HumanEval+ 164, greedy, thinking off, 0.40.14, 128 GB M-series host, direct Ollama ([REAL] 2026-11-05) | 78.7%; 77/93 on the 93 problems Nemotron scored 81/93 |
| Tool loop / recovery after injected failure (same run) | 10/10; 5/5 |
| Decode (same run) | 98 tok/s |
| Q8_0 comparison (same run, not selected: no room for 96K beside cyber on 16 GB) | 79.3%, 82/93, 113 tok/s |
| Memory at 96K ([APPROX] 2026-11-05) | ~6.8 GB; ~8 GB with helpers |
| Library pull, `/api/show` ([REAL] 2026-11-06, 128 GB host) | Three blobs as pinned; `qwen35.context_length` 262144 |
| MTP on vs off, 0.40.11 stock, 16K, three seeds ([REAL runtime; APPROX fixtures] 2026-10-27, 64 GiB M3 Max) | 101.5 vs 84.1 tok/s median; coding 12/15 vs 9/15 (five functions x three seeds); three tool rounds |
| Throughput at depth 1, managed runtime, 16K, 256 tokens ([APPROX] 2026-10-27) | 99.65 tok/s median (99.62–101.59) |
| Proxy protocols: OpenAI Chat tool/result, Anthropic Messages, Responses, streaming ([REAL] 2026-10-27, `0.40.8-ailocal.18631.3`) | Passed; process args `--spec-type draft-mtp --spec-draft-n-max 1` |
| Codex and Claude file-edit fixture ([REAL clients; APPROX fixture] 2026-10-27) | Passed both, three independent assertions |

Unverified: physical 16 GB fit (memory figures are mapped-weight estimates from the 128 GB host); end-to-end re-run after the 2026-11-06 rename; full-window latency; repository-scale accuracy.

### qwen3.5:4b-unrestricted-mtp (16 GB cyber)

`hf.co/localslm/Qwen3.5-4B-MTP-Uncensored:model_q4_k_m.gguf`, publisher revision `36c997d2686cdbef4f8a70d505074eda536d1267`, built-in `qwen3.5` renderer/parser, embedded MTP head; draft depth 1; keep_alive 5m.

| Measured | Result |
|---|---|
| MTP on vs off ([REAL runtime; APPROX fixtures] 2026-10-27, 64 GiB M3 Max) | 101.5 vs 79.9 tok/s median (+27%); 9/15 coding both; three tool rounds incl. error recovery; MTP execution confirmed in process flags and draft logs |
| Throughput at depth 1 vs standard 4B, managed runtime ([APPROX] 2026-10-27) | 98.95 tok/s median (97.91–101.07), ~0.7% below standard |
| Native renderer fix ([REAL] 2026-10-27) | Raw HF template failed Codex with `System message must be at the beginning`; native `/api/create` with `qwen3.5` renderer/parser passed a late-system-message request |
| Proxy protocols and Codex/Claude edit fixture ([REAL] 2026-10-27) | Passed |
| Publisher scalar sampling (no DRY) vs base-Qwen preset, five tasks x three seeds, 16K ([APPROX] 2026-10-28) | 9/15 in 34.29 s vs 9/15 in 41.64 s; preset kept |
| Memory at 96K ([APPROX] 2026-11-05) | ~5.4 GB alone; Ollama unloads the primary to load it (~12 GB together exceeds the ~10.7 GB the primary leaves) |

Unverified: re-measurement under greedy sampling (figures are 2026-10-27/28); projector not independently pinned by the recipe; comprehensive refusal behavior; physical 16 GB fit.

### qwen3.5:9b-calibrated-mtp (32 GB primary)

`hf.co/unsloth/Qwen3.5-9B-MTP-GGUF:Qwen3.5-9B-Q4_K_M.gguf`, revision `9716a636ee4bddc3fed678220b7a33dd2a4160ae`, 5,868,826,976 bytes; imatrix metadata `unsloth_calibration_Qwen3.5-9B.txt`, 248 entries, 77 chunks; embedded MTP head; draft depth 3.

| Measured | Result |
|---|---|
| HumanEval+ 164, greedy, thinking off, 0.40.14, 128 GB host ([REAL] 2026-11-05) | 84.8% (published base 87.2%); 85/93 on Nemotron's 93 |
| Tool loop / recovery (same run) | 10/10; 5/5 |
| Decode (same run) | 51 tok/s |
| Coding fixtures, ten tasks x three seeds, depth 2 then depth 3 ([APPROX] 2026-10-28, `0.40.8-ailocal.18631.3`, 64 GB M3 Max) | 22/30 and 3/3 tool rounds at both depths (previous official 9B: 21/30) |
| Draft-depth sweep 0/1/2/3 ([APPROX] 2026-10-28) | 52.31 / 61.86 / 76.51 / 85.49 tok/s; depth 3 ~63% over MTP off |
| Proxy protocols; Codex/Claude edit fixture ([REAL] 2026-10-28) | Passed; 35.0 s Codex, 44.5 s Claude |
| Image probe, native `/api/chat` ([REAL] 2026-10-28) | Identified a solid-red image |
| Cache probe at 139,264 window, depth 3 ([REAL] 2026-10-28) | 3,821/3,829 tokens reused; prefill 5.6 s to ~0.1 s |
| Thinking-enabled client matrix after renderer fix, 32gb tier ([REAL] 2026-10-29, `0.40.8-ailocal.18631.4`) | `codex-local` 6/6; `claude-local` auto memory off 6/6 |
| Memory at 163,840 ([APPROX] 2026-11-05) | ~9.4 GB; both 9B models resident ~20 GB with helpers; KV near 24 KB per token |

Unverified: physical 32 GB fit; full-window latency and quality; repository-scale accuracy.

### qwen3.5:9b-unrestricted-mtp (32 GB cyber)

`hf.co/localslm/Qwen3.5-9B-MTP-Uncensored:model_q4_k_m.gguf`, revision `2d76f180895809e97ea55c524bca2236de7a0e26`, 5,885,604,000 bytes; imatrix metadata 77 chunks, 248 entries, one nextn layer; draft depth 3.

| Measured | Result |
|---|---|
| MTP depth 2 vs off ([APPROX] 2026-10-27, `0.40.8-ailocal.18631.3`, 64 GB M3 Max) | 53.45 vs 37.13 tok/s |
| Coding fixtures, ten tasks x three seeds, depth 1 ([APPROX] 2026-10-27) | 20/30 (one output-limit failure); three tool rounds |
| Coding fixtures at depth 2 and depth 3 ([APPROX] 2026-10-28) | 22/30 and 3/3 tool rounds at both |
| Draft-depth sweep 0/1/2/3 ([APPROX] 2026-10-28) | 51.97 / 62.96 / 75.49 / 85.85 tok/s |
| Codex/Claude edit fixture ([REAL] 2026-10-28) | 53.4 s Codex; 41.4 s Claude (first Claude run hit the eight-turn limit on a harness permission denial; retry passed) |
| Publisher scalar sampling (no DRY) vs base-Qwen preset ([APPROX] 2026-10-28) | 11/15 in 97.98 s vs 13/15 in 74.15 s; preset kept |
| Memory at 163,840 ([APPROX] 2026-11-05) | ~9.4 GB; resident beside the primary on 32 GB |

Unverified: re-measurement under greedy sampling (figures are 2026-10-27/28); universal non-refusal; physical 32 GB fit; repository-scale accuracy.

### gemma4:26b-qat-mtp (64/128 GB primary and fast)

Official `gemma4:26b-a4b-it-qat` QAT Q4_0 target plus the 251,937,728-byte HauhauCS MTP head (revision `f9093662a2e7ae0503f637088bc96f77a1a70c83`) via Ollama `DRAFT`/`draft_files`; `gemma4-large` renderer, `gemma4` parser; draft depth 2. A target-only HF pull does not attach the head.

| Measured | Result |
|---|---|
| MTP depth 2 vs off, 16K, thinking off ([APPROX] 2026-10-27, `0.40.8-ailocal.18631.3`, 64 GB M3 Max) | 123.71 vs 81.69 tok/s; depth 2 beat 0/1/3 |
| Coding fixtures at depth 3 ([APPROX] 2026-10-27) | 9/10 coding, 3/3 tool rounds (depth-2 quality not measured separately) |
| Codex/Claude edit fixture ([REAL] 2026-10-27) | 29.35 s Codex; 34.35 s Claude |
| Empty-message load at 262,144 ([REAL] 2026-10-27) | Worker RSS ~17.95 GiB (RSS is not full unified-memory accounting) |
| Cache probe at 196,608 ([REAL] 2026-10-28) | 4,488/4,497 reused (99.8%); warm prefill ~56x faster |
| Reasoning loops in tool turns, `codex-local` 0.166 ([REAL] 2026-11-05, #68) | 2/13 sessions looped on re-injected reasoning; `reasoning_loop_guard` (60,000-character threshold; normal max 41,466, loops >95,000) recovered 8/8 sessions |
| Tool calls dropped by Ollama's `gemma4` parser, 0.40.14 ([REAL] 2026-11-05, #69) | 4/9 edit runs lost a call missing the opening `<\|"\|>`; guard re-send once: 8/8 edit-and-test sessions passed |
| Memory with cyber ([APPROX] 2026-11-05) | 196K: ~20.0 GB (+22.2 cyber, ~44.5 GB with helpers under ~48 GB default GPU limit on 64 GB); 262K: ~21.3 GB (+23.5 cyber) |

Unverified: thinking-enabled full-window task speed; filled-window quality; the 64 GB residency margin on a 64 GB Mac; 128 GB hardware (128 GB reuses 64 GB evidence); other Gemma 4 task shapes for #68/#69.

### gemma4:26b-unrestricted-qat-mtp (64/128 GB cyber)

HauhauCS QAT Q4_K_M target (`Gemma4-26B-A4B-QAT-Uncensored-HauhauCS-Balanced-MTP`, revision `f9093662a2e7ae0503f637088bc96f77a1a70c83`) plus the same MTP head; `gemma4-large`/`gemma4`; draft depth 3; keep_alive 20m.

| Measured | Result |
|---|---|
| MTP depth 3 vs off ([APPROX] 2026-10-27, `0.40.8-ailocal.18631.3`, 64 GB M3 Max) | 111.19 vs 75.09 tok/s; ~10% slower than standard |
| Coding fixtures at depth 3 ([APPROX] 2026-10-27) | 9/10 coding, 3/3 tool rounds |
| Author sampling (0.6/0.9/64, min-p 0.05, repeat 1.1) ([APPROX] 2026-10-27) | 10/10 at depths 2 and 3, but 95.59/96.95 tok/s; Google preset kept |
| Codex/Claude edit fixture ([REAL] 2026-10-27) | 32.54 s Codex; 38.57 s Claude |
| Empty-message load at 262,144 ([REAL] 2026-10-27) | Worker RSS ~20.06 GiB |
| 64 GB residency, five aliases sequential ([REAL] 2026-10-27) | Cyber evicted the idle fast worker; two backends ~33.51 GiB RSS, zero swap |
| Memory ([APPROX] 2026-11-05) | ~22.2 GB at 196K; ~23.5 GB at 262K |

Unverified: universal non-refusal; repository coding quality; full-window prefill; 128 GB hardware.

### qwen2.5-coder:1.5b-base (16/32 GB completion) and qwen2.5-coder:3b-base (64/128 GB completion)

Official Ollama base GGUF tags with native FIM tokens; the base template is required because LiteLLM drops `suffix` and sends the FIM prompt as a chat message.

| Measured | Result |
|---|---|
| FIM arithmetic completion via completion API ([REAL] 2026-10-29) | Both backends returned the expected completion |
| FIM request through installed proxy per tier ([REAL] 2026-10-28, 64 GB host) | Completed on 16, 32 and 64 GB activations |

Unverified: latency on 64/128 GB target hardware (earlier FIM comparisons ran under memory pressure on an M4 16 GB host); any MTP enhancement; FIM quality beyond the arithmetic fixture.

### Superseded and rejected

- `nemotron-3.5-bolt:e4b` and `:e4b-unrestricted` (16/32 GB), 2026-11-05 to 2026-11-06: replaced once greedy sampling closed Qwen's gap (4B 77/93, 9B 85/93 vs Nemotron 81/93 on the same problems); its native window was 65,536 vs Qwen's 262,144.
- `qwen3.5:4b-mtp-q4_K_M`, `qwen3.5:9b-calibrated-mtp-q4_K_M`, `qwen3.5:{4b,9b}-unrestricted-mtp-q4_K_M`, `gemma4:26b-unrestricted-qat-mtp-q4_K_M`, 2026-11-06: same weights, renamed to the structured names.
- `qwen3.5:9b-mtp-q4_K_M` (official, 32 GB primary / 64 GB fast), 2026-10-28: replaced by calibrated Unsloth 9B (21/30 vs 22/30; 69.24 vs 77.92 tok/s).
- Qwen 9B as 64/128 GB fast, superseded: fast is now the Gemma primary with thinking off.
- `qwen3.5:4b-mtp-q4_K_M` Q8_0 variant, 2026-11-05: scored higher but leaves no room for the 96K window beside cyber on 16 GB.
- Qwen3.8-distilled 9B GGUFs (Twobombs IQ3_M, mradermacher IQ3_M/Q4_K_M), 2026-10-28: 21/30 and 18/30 coding, no vision.
- Nicolas Ramos Qwen3.8 9B oQ4e/MTP MLX, 2026-10-28: native load failed with an MLX concatenate panic.
- `qwen3.5:9b-mtp-nvfp4` and calibrated MLX MTP (32 GB), 2026-10-27: no fidelity or speed win over GGUF (JSD 0.00516 vs 0.00340; 52.38 vs 52.82 tok/s).
- `gemma4:26b-nvfp4` (calibrated MLX), 2026-10-27: 121.65 tok/s but no higher-precision fidelity comparison; unpromoted.
- `gemma4:26b-mlx` and legacy MLX (64/128 GB), 2026-10-27: 74.91 tok/s vs 123.71 for QAT GGUF MTP.
- Huihui Gemma QAT plus the same MTP head (64 GB cyber), 2026-10-27: 93.32 vs 111.19 tok/s for HauhauCS.
- `huihui_ai/gemma-4-abliterated:26b-qat` and `ailocal-gemma4-26b-uncensored:mlx-46ab84d` (00cyre MLX), 2026-10-27: replaced by HauhauCS QAT MTP GGUF.
- Huihui Opus-distilled abliterated 4B/9B (MLX and `huihui_ai/qwen3.5-abliterated:9b-Claude-4.6-Opus-q4_K`), andrevp 9B Heretic MLX (16/32 GB cyber), 2026-10-27: replaced by tested localslm unrestricted MTP GGUFs.
- `qwen3.5:{4b,9b,2b}-mlx` official MLX (16/32 GB), 2026-10-24 to 2026-10-27: MLX-only selection superseded by the GGUF/MTP rule; native Codex edits failed at 90 s deadlines.
- Inspected, never run (2026-10-27/28): bambocher Huihui 9B oQ4e/MTP, Foresee Qwen3.8 MTPLX (unverifiable lineage), TheCluster MXFP4 (no MTP tensors), underlotus/zecanard/yachen4ever (no MTP head), Blackwell/vLLM-only NVFP4 bundles.
