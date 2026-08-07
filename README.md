# ailocal

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]()

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

ailocal does not install other people's software. Get the prerequisites
yourself, then install ailocal:

```bash
brew install jq
brew install --cask docker-desktop ollama-app   # open Docker Desktop once
ollama serve                                    # or open Ollama.app

pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git
ailocal install
```

`ailocal install` refuses to run if a prerequisite is missing, and names it.

The installer detects memory, selects a profile, pulls the models, starts the
services and deploys the client configurations. It refuses to select a profile
the machine cannot hold.

That is the whole lifecycle:

```bash
pipx install ...           # once
ailocal profile use 64gb   # only to override what memory selected
ailocal start              # regenerates configuration, then brings the stack up
ailocal check              # is it working?
```

Configuration is derived, never a state to remember to refresh: `start`
regenerates it from the active profile every time.

It can also install login agents that start Ollama and preload the resident
model — answer `y` when prompted. Re-running `ailocal install` is
idempotent and reconfigures them.

## Verify

```bash
ailocal check       # is ailocal configured and working?
```

One command answers it, end to end: configuration consistency, the running
containers, every served capability, one real model response, image pinning,
the installed client configs and the host machine. Anything that is not right
prints with the command that fixes it.

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
inline completion; the 64 GB tier uses `qwen2.5-coder:3b`. Profiles are installed to
`~/.config/ailocal/profiles/` — edit the profile there, never a generated file,
then `ailocal start`.

## Commands

`ailocal` is the only public entry point. Run `ailocal help` for the current
list — it is the source of truth, so this page does not restate it.

The ones you need first:

```
ailocal install     bootstrap the stack
ailocal start       bring the proxy and models up
ailocal status      live model status by capability
ailocal check       is everything ready and working?
ailocal stop        bring it down
```

`.env` lives at `~/.config/ailocal/.env`, never in a checkout. `ailocal install`
writes it with a random master key and will not overwrite it unattended; it is
the one piece of state that does not regenerate.

Configuration is generated from the profile straight into the place each
consumer reads it: the two files LiteLLM mounts under
`${AILOCAL_STATE:-~/.local/state/ailocal}`, and the client config under
`~/.config/ailocal`. Every generated file says so in its own header. Deleting
them and re-running `ailocal start` is a supported repair.

To upgrade ailocal itself, upgrade the package (`pipx upgrade ailocal`) and run
`ailocal start`. Docker images are digest-pinned, so there is nothing else to
refresh.

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
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms and fixes |
| [docs/security.md](docs/security.md) | secrets, permissions, exposure |
| [AGENTS.md](AGENTS.md) | developing and validating ailocal |

## License

Apache-2.0 — see [LICENSE](LICENSE).

Developed and maintained by Victor T. Chevalier for DevelopSolutions, LLC.
