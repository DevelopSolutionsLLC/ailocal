# Changelog

Release policy and the meaning of each bump: [RELEASING.md](RELEASING.md).

## v0.9.1 — context windows sized from measured runner behaviour

Every profile carried context limits inherited from a model backend ailocal no longer runs. This release re-measures the real constraints and re-sizes all four tiers against them. No commands, configuration layout or client files change shape — only the numbers inside the profiles.

### What changed

Context windows and output ceilings rise on every tier:

| Tier | Input (was → now) | Output | Auto-compaction (was → now) |
|---|---|---|---|
| 16 GB | 57,344 → 90,112 | 8,192 → 16,384 | 45,875 → 67,584 |
| 32 GB | 57,344 → 122,880 | 8,192 → 16,384 | 45,875 → 91,750 |
| 64 GB | 81,920 → 131,072 | 16,384 | 49,152 → 111,411 |
| 128 GB | 81,920 → 245,760 | 16,384 | 49,152 → 139,264 |

`implementation` and `review` now get the same input window as `architecture` rather than roughly half of it. Context capacity and role identity are separate concerns; a review that cannot see a whole change is not a cheaper review.

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
- **Inline completion (FIM)** stays at 3,968 input / 128 output. The model supports 32,768, but there are no recorded requests and no installed consumer, so a larger window would buy nothing.
- **The `fast` role** keeps a 4,096 output ceiling on every tier. It classifies and answers briefly; it is also the one role with recorded runaway-generation behaviour.

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
