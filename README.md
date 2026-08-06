# ailocal

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]()
[![Status](https://img.shields.io/badge/status-release%20candidate-orange.svg)]()

Run Claude Code, Codex CLI and VS Code Copilot Chat against local models on
Apple Silicon. No cloud costs, no data leaving the machine, no changes to the
tools themselves.

Ollama runs the models natively on the GPU. LiteLLM fronts them on
`localhost:4000` as an OpenAI- and Anthropic-compatible proxy. Point a tool at
that address instead of Anthropic or OpenAI; everything else stays the same.

## What you get

- **Capability names, not model tags.** Clients ask for `ailocal-architecture`
  or `ailocal-implementation`; the profile decides which model answers. Change
  models without touching a client.
- **Hardware-sized profiles.** One profile per memory class picks models,
  context windows and output limits the machine can actually hold.
- **Isolated client profiles.** `claude-local` and `codex-local` launch with
  their own config roots, so local and cloud sessions coexist untouched.
- **Local web search** through SearXNG behind the proxy, with an optional Brave
  API key.
- **A Python language server for `claude-local`**, working out of the box.
- **A tool gateway** that trims oversized tool schemas before a local model sees
  them — often the difference between a usable and an unusable session.

## Requirements

macOS on Apple Silicon · 16 GB unified memory minimum · Docker Desktop ·
Ollama · `jq`

Disk depends on the profile: roughly 25 GB at 16 GB RAM, 40 GB at 64 GB.

## Install

```bash
brew install git jq
brew install --cask docker ollama    # open Docker Desktop once to finish setup
ollama serve                         # or open Ollama.app

git clone https://github.com/DevelopSolutionsLLC/ailocal.git
cd ailocal
./install.sh
```

The installer detects memory, selects a profile, pulls the models, starts the
services and deploys the client configurations. It refuses to select a profile
the machine cannot hold.

It can also install login agents that start Ollama and preload the resident
model — answer `y` when prompted, or run `ailocal autostart` later.

## Verify

```bash
ailocal doctor      # health, with a fix for anything wrong
ailocal smoke       # bounded runtime check: one real model response
ailocal validate    # configuration consistency; works with the stack stopped
```

Then start a session:

```bash
claude-local        # Claude Code against local models
codex-local         # Codex CLI against local models
```

Any OpenAI- or Anthropic-compatible client works directly: base URL
`http://localhost:4000`, key from `LITELLM_MASTER_KEY` in `.env`, model
`ailocal-<capability>`.

## Profiles

| Profile | Architecture model | Context / output |
|---|---|---|
| `16gb` | `qwen3.5:4b` | 64K total (57 344 in / 8 192 out) |
| `32gb` | `qwen3.5:9b` | 64K total (57 344 in / 8 192 out) |
| `64gb` | `gemma4:26b-mlx` | 96K total (81 920 in / 16 384 out) |
| `128gb` | `gemma4:26b-mlx` | pending hardware validation |

Capabilities are `architecture`, `implementation`, `review`, `fast`,
`completion` and `embeddings`. The small tiers use `qwen2.5-coder:1.5b` for
inline completion; the 64 GB tier uses `qwen2.5-coder:3b`. Profiles live in `profiles/` — edit the
profile, never a generated file, then run `ailocal sync`.

## Commands

```
ailocal status | doctor | validate | smoke      inspect
ailocal start | stop | update | sync            lifecycle
ailocal clients | vscode | models-install       deploy
ailocal trace | metrics | e2e <client>          diagnostics
ailocal benchmark <models|planner|gateway>      developer benchmarks
ailocal teardown                                remove everything
```

`ailocal sync` regenerates every derived file from the profile and client
policy. Generated output lives outside the repository, under
`${AILOCAL_STATE:-~/.local/state/ailocal}`. Deleting that directory and
re-running `ailocal sync` is a supported repair.

## Supported clients

| Client | Status |
|---|---|
| Claude Code | fully supported — tools, search, and a Python LSP |
| VS Code Copilot Chat | supported for chat and completion |
| Codex CLI | configuration, routing, geometry and tool transport work; interactive streaming is blocked upstream |

## Known limitations

- **Codex interactive sessions do not complete.** A streamed turn never emits a
  terminal event — [BerriAI/litellm#27442](https://github.com/BerriAI/litellm/issues/27442),
  not an ailocal defect. Configuration and routing are validated.
- **Claude Code reports "0 searches"** even when retrieval worked. The count is
  a display artefact: the search block is dropped during response serialisation
  upstream.
- **The 128 GB profile is unvalidated.** It mirrors the 64 GB policy and is not
  a completed sizing recommendation.
- **Local models are not frontier models.** Expect a capable everyday assistant,
  not a replacement for hosted Opus or GPT on hard problems.

## Documentation

| Document | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how the pieces fit together |
| [docs/operations.md](docs/operations.md) | lifecycle, recovery and validation |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms and fixes |
| [docs/security.md](docs/security.md) | secrets, permissions, exposure |
| [benchmarks/README.md](benchmarks/README.md) | reproducing model comparisons |
| [AGENTS.md](AGENTS.md) | contributing and agent operating rules |

## License

Apache-2.0 — see [LICENSE](LICENSE).

Developed and maintained by Victor T. Chevalier for DevelopSolutions, LLC.
