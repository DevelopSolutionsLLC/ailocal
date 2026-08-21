# Changelog

Release policy and the meaning of each bump: [RELEASING.md](RELEASING.md).

## v0.11.0 — sampling, output ceilings and models that actually unload

Three defects that all failed the same way: silently. Nothing here needs user action, but if you have edited your own `~/.config/ailocal/profiles/`, see the migration note at the end.

### Gemma now runs at Google's published sampling

The gemma4 roles shipped `temperature 0.1 / top_k 20 / top_p 0.9`, chosen for run-to-run reproducibility. They now ship Google's documented `temperature 1.0 / top_k 64 / top_p 0.95`.

This was measured, not adopted on faith. A 50-generation A/B on coding tasks scored by *executing* the model's code against hidden assertions:

| | old (t0.1/k20/p0.9) | vendor (t1.0/k64/p0.95) |
|---|---|---|
| tasks passed | 18/25 (72%) | **24/25 (96%)** |
| decode | 82.9 tok/s | 81.0 tok/s |

The mechanism is not "better code". All 7 failures at the old setting were **empty responses**: constrained sampling sent the model into 13,000–23,000 characters of reasoning until it exhausted its output budget and returned nothing. The one failure at vendor sampling was genuinely wrong code. Sampling costs nothing in throughput — draft acceptance was 0.79 against 0.77.

`tests/measure_sampling.py` is that benchmark, kept out of the gate like the other measurement scripts.

### Models unload

`implementation` and `review` declared `keep_alive = -1`. All three gemma roles share **one** resident model, and keep_alive is last-writer-wins on it, so the highest-frequency role re-pinned 16 GiB permanently while `architecture`'s `6h` sat in the profile looking like the policy. Measured: a request carrying `-1` set `expires_at` to the year **2318**.

All three now declare `6h`. Roles sharing a model must declare the same keep_alive or the shortest one is fiction. `fast` (20m) and `completion` (2h) are unchanged.

`OLLAMA_KEEP_ALIVE` also drops from `-1` to `6h`. It is the default for anything loaded *without* an explicit value — the preload agent, a bare `ollama run`, and models other tools load on the same daemon. `-1` there meant none of them ever released memory. A caller that wants a model pinned can still pass `-1` explicitly.

### Output ceilings raised to what the window allows

64 GB: `architecture`, `implementation` and `review` go to **32768** (implementation was 8192). 128 GB: `implementation` to 16384 — its `architecture` and `review` were already at the model's native 262144 ceiling and could not grow without cutting input.

Free on MLX, where KV is allocated lazily: `num_ctx` 196608 was verified to load at 16.15 GiB resident, identical to a small window. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` now derives to 32768, above Claude Code's own 32000 default request, so the truncate-and-retry behaviour v0.10.0 addressed is gone rather than narrowed.

To be clear about what this does *not* fix: with no ceiling at all, gemma4 finishes these tasks in 2,337–5,175 tokens, so 8192 was never truncating normal work. The empty answers came from looping, and the sampling change is what fixes those. This is headroom.

### `OLLAMA_MAX_LOADED_MODELS` 5 → 4

The tier needs three distinct models, not five: three roles share one gemma. The fourth slot is `nomic-embed-text`, which is in no profile but is served by the same daemon. Three would fit only while `completion` stays standby — and `keep_alive -1` stops the idle timer, not a capacity eviction, so admitting a 1.9 GB FIM model could evict the 16 GiB gemma.

### Fixes

- `ailocal check` reported **"Ollama not responding"** against a healthy daemon whenever `OLLAMA_HOST` was set — which `ailocal install` guarantees via `launchctl setenv`. Ollama's variable is documented as bare `host:port`; read as a URL, `127.0.0.1` parses as the scheme and every request raises. New `policy.ollama_url()` normalises it for both readers.
- `ailocal check` now detects a **deleted log destination**. If `~/Library/Logs/ailocal/` is removed while the agent runs, launchd neither recreates it nor reopens the descriptors: the process writes to an unlinked inode, `launchctl print` still says running, and every diagnostic is lost with no symptom. Remediation is a mkdir *and* a kickstart; either alone leaves the process writing to nowhere.
- `OLLAMA_FLASH_ATTENTION` and `OLLAMA_KV_CACHE_TYPE` are documented as reaching the **llama_cpp runner only**. The MLX runner takes neither, so every MLX-served role pays unquantized fp16 KV regardless. Measured 46.2 KB/token at 64K depth, which matches the 45.4 the profiles claim.
- `registry.yaml` engine behaviour re-measured on Ollama 0.32.14 and re-stamped, not version-bumped. MLX: loaded at 24576, processed 57,787 tokens without truncation. llama_cpp: loaded at 20480, processed 10,243 — *half*, because `OLLAMA_NUM_PARALLEL` splits the window into slots.

### New measurement scripts

`tests/measure_agentic.py` compares candidate models for the agentic default — decode, speculation acceptance, cold and warm prefill, resident memory, tool-loop correctness. `tests/measure_sampling.py` A/Bs sampling on executed code. Both are out of the gate, keep no history and assert nothing, like `measure_geometry.py`.

For the record, on this hardware `gemma4:26b-mlx` remains the fastest by a wide margin: 2.9x the decode and 3.8–6x the prefill of `qwen3.8:27b-mlx`. Ollama's MLX build of gemma4 has **no vision**; the GGUF `gemma4:26b` does, and beats qwen3.8 on every axis while keeping it.

### Migration

`ailocal install` preserves a profile you have edited — it compares against the shipped manifest digest and keeps anything that differs. So an untouched profile picks these values up on upgrade; **an edited one does not**. If you have customised `~/.config/ailocal/profiles/`, copy the new `temperature`, `top_k`, `top_p`, `max_output` and `keep_alive` values across by hand, or delete your copy to take the shipped one.

## v0.10.0 — language servers, client alignment, and a runtime that cannot drift silently

`claude-local` now bootstraps a complete, self-sufficient config root, Claude Code's context and output settings match what the backend actually serves, and the generated runtime can no longer disagree with the repository without saying so.

### Language servers in `claude-local`

`ailocal clients claude` enables the official Claude Code LSP plugin for each language whose server binary is already on your machine — Python (`pyright-lsp`), TypeScript/JavaScript (`typescript-lsp`), Go (`gopls-lsp`) and C/C++ (`clangd-lsp`).

ailocal installs no language server and no language ecosystem; a missing server is reported with the command that installs it. Plugins are enabled **only in ailocal's own config root** — hosted `~/.claude` is never modified. Plugin state is per config root, so a fresh root starts empty regardless of what your hosted client has.

### Claude Code now asks for what the backend serves

Claude Code defaults to a 32,000-token output request, while the backend serves the profile's `max_output`. Long answers were truncated and retried rather than ending cleanly. `settings.json` now carries `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` derived from the active profile — the same numbers LiteLLM receives.

Those variables are process-wide while the model slots differ, so the generated file also lists the background slots whose smaller limits they cannot express.

### 64 GB tier: more usable context

`architecture` input rises 131,072 → 163,840 (180,224 total), compacting at 139,264. Re-measured cold prefill on the MLX runner justified the increase; the full 262,144 native window was measured and deliberately left unexposed, because a cache miss at that size costs over nine minutes.

### More reliable tool calls on Gemma

Repaired the one `Agent` argument `gemma4` omits, and kept `Skill` and `SendMessage` available — delegation and skill invocation now work instead of failing quietly.

### Configuration ownership

`~/.config/ailocal/.env` held generated secrets and your own provider keys in one file, which is why upgrading it meant offering to destroy your keys. It is now split: generated state is ailocal's, `.env.local` is yours and wins. Existing files migrate automatically, byte-for-byte, without rotating a key.

The persona mechanism is removed. `ENABLE_LSP_TOOL` is no longer written and is retired from existing installs — the LSP tool is built into current Claude Code.

### A runtime that reports its own drift

- `ailocal check` detects a container whose config mount a reinstall detached; previously the proxy answered 200 while serving nothing.
- The gate refuses to run on generated-config drift instead of regenerating over it mid-run.
- The gate refuses to run when the installed package is older than the checkout. `ailocal start` runs the *installed* generator, so a tested change could otherwise be missing from the live runtime with a green suite either side of it. Remediation: `pipx install --force . && ailocal start`.
- Codex keeps its own trust records across regeneration.

### Upgrading

```sh
pipx upgrade ailocal && ailocal start
```

No commands or configuration layout change. Run `ailocal clients claude` to pick up LSP plugins for servers you already have.

## v0.9.1 — context windows sized from measured runner behaviour

Every profile carried context limits inherited from a model backend ailocal no longer runs. This release re-measures the real constraints and re-sizes all four tiers against them. No commands, configuration layout or client files change shape — only the numbers inside the profiles.

### What changed

Context windows and output ceilings rise on every tier:

| Tier | `architecture` input | Auto-compaction (was → now) |
|---|---|---|
| 16 GB | 57,344 → 90,112 | 45,875 → 67,891 |
| 32 GB | 57,344 → 122,880 | 45,875 → 92,262 |
| 64 GB | 81,920 → 131,072 | 49,152 → 111,411 |
| 128 GB | 81,920 → 245,760 | 49,152 → 139,264 |

`implementation` and `review` now get the same input window as `architecture` rather than roughly half of it. Context capacity and role identity are separate concerns; a review that cannot see a whole change is not a cheaper review.

Every tier now compacts at a uniform **85%** of its window, so no tier runs a different safety margin. The window stays per-tier, because it encodes the one thing that genuinely differs: how many tokens that tier's runner can cold-prefill inside its latency budget.

Output ceilings are now set by role rather than per tier, identically everywhere:

| Role | Output | Why |
|---|---|---|
| `architecture` | 16,384 | long design documents |
| `implementation` | 8,192 | large code generation without spending input budget |
| `review` | 16,384 | long review reports |
| `fast` | 4,096 → 8,192 | more than a word, still not a generation tier |
| `completion` (FIM) | 128 → 512 | multi-line suggestions; 128 truncated mid-block |
| `embeddings` | n/a | no generation route |

### Why the old numbers were wrong

They described a llama.cpp/GGUF backend the 64 and 128 GB tiers stopped using when they moved to the MLX runner. The profiles still justified a 49,152 compaction point with "cold prefill ~5 min at ~58K, 13 min at ~88K". Measured cold on 64 GB hardware, `gemma4:26b-mlx` evaluates 137,233 tokens in 229.5s — 88K costs 147s, not 13 minutes.

No model was ever the limit. Every chat model in every tier reports 262,144 native context.

### The 32 GB tier

16 GB and 32 GB previously shared identical geometry and an identical compaction point despite different memory and different models. That was a real defect, but not the obvious one: the four roles on those tiers share one llama.cpp runner, where `num_ctx` is fixed at first load and an over-long prompt is truncated **from the front with HTTP 200 and no error**. They must declare one identical total, so `context_input` and `max_output` offset each other. Raising a single role would have silently clipped the other three. The tier now moves as a unit, and a test enforces the equality.

### Auto-compaction

Ceilings are sized by memory; compaction triggers are sized by latency. These were previously conflated, which is how a tier ended up compacting at less than half the context it could carry.

The trigger is also no longer set by cold-prefill cost alone. An interactive session grows incrementally and hits the prefix cache every turn, so the full cold cost is paid on session *resume*, not per turn — sizing for resume penalised every ordinary turn to protect the rare one.

The full model window remains available for deliberate one-shot work. Compaction is a client-side threshold, not a model limit; ailocal still never summarises conversations itself.

### Deliberately unchanged

- **Embeddings** stay at 2,048. That is `nomic-embed-text`'s real maximum — it clips silently above it and still returns a valid vector. The model's own `num_ctx 8192` default parameter is misleading and should not be followed.
- **Inline completion (FIM)** keeps its 3,968-token input. The model supports 32,768, but there are no recorded requests and no installed consumer, so a larger input window would buy nothing. Its output ceiling does rise to 512, because 128 truncated multi-line completions mid-block.

### Upgrading

Run `ailocal start` after upgrading to regenerate client configuration and restart the proxy.

**If you have edited a file in `~/.config/ailocal/profiles/`, ailocal will not overwrite it** — that is the standing promise about authored policy, and it means a customised profile keeps its old context values. To adopt the new sizing, compare your file against the shipped one and merge, or delete yours and re-run `ailocal install`.

### Notes

The 64 GB tier is measured. The 16, 32 and 128 GB tiers are sized from model limits and from per-token costs measured on 64 GB hardware; no machine of those sizes has run them, and each profile records which of its numbers are measured and which are not. The 128 GB profile is no longer a copy of the 64 GB one.

## v0.9.0 — first public release

ailocal provides a local AI development environment for Apple Silicon Macs, integrating local models with Claude Code, Codex CLI, and VS Code Copilot through a single local gateway.

Everything runs on your own hardware using Ollama and Docker. Supported clients are configured automatically when present, while remaining optional.

### Highlights

**Simple installation**

```sh
brew install --cask docker-desktop ollama-app
brew install pipx
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git
ailocal install
ailocal check
```

**Supported clients**

- Claude Code (`claude-local`)
- Codex CLI (`codex-local`)
- VS Code Copilot (`ailocal-*` models)

Clients are optional. ailocal configures only the clients installed on your machine.

**Hardware-aware configuration**

Automatically selects appropriate model profiles for Apple Silicon systems with:

- 16 GB
- 32 GB
- 64 GB
- 128 GB (experimental)

No manual profile editing is required for typical installations.

**Local API**

Provides both an OpenAI-compatible API and an Anthropic-compatible API, allowing local models to work with existing tooling.

**Validation**

`ailocal check` performs an end-to-end validation of prerequisites, runtime, local gateway, models, client configuration, and live inference — with actionable remediation when something is missing.

### What's new

This release includes extensive stabilization work across the project:

- simplified repository structure
- cleaner packaging
- improved installation experience
- automatic client detection
- improved VS Code integration
- improved Claude Code integration
- improved Codex CLI integration
- cleaner configuration ownership
- removal of obsolete generated artifacts
- stronger validation gates
- improved documentation
- comprehensive installation verification

### Known limitations

- Docker Desktop and Ollama must be installed by the user.
- VS Code requires one manual API key paste into its encrypted SecretStorage. This is a limitation of the VS Code extension model rather than ailocal.
- 128 GB hardware profiles remain lightly validated compared to the other configurations.

### Stability

This release establishes the initial public interface for ailocal. Future releases will aim to preserve compatibility for CLI commands, configuration layout, generated client configuration, and profile behaviour. Breaking changes will be documented in release notes.

### Thank you

Thanks to everyone who tested early versions, reported issues, and helped improve the project through repeated installation, packaging, and validation testing.
