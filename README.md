# ailocal

**Run Claude Code, Codex CLI, and VS Code Copilot Chat with local models on your Mac.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]()
[![Memory](https://img.shields.io/badge/memory-16%20GB%20minimum-lightgrey.svg)]()

| Your tool | What you run |
|---|---|
| Claude Code | `claude-local` |
| Codex CLI | `codex-local` |
| VS Code | local models in Copilot Chat |

Local models. Local data. One configuration.

---

## What is ailocal?

ailocal runs the coding tools you already use against models on your own
machine. Nothing is sent to Anthropic or OpenAI, and there is nothing to pay
per token.

You do not configure any of it by hand. `ailocal install` detects your Mac,
picks models that fit its memory, downloads them, starts the services, and
writes the configuration for every supported client.

**Requirements:** macOS on Apple Silicon, 16 GB unified memory minimum.

## Install

```bash
# 1 — prerequisites
brew install jq pipx
brew install --cask docker-desktop ollama-app

# 2 — install ailocal
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git

# 3 — configure everything
ailocal install

# 4 — verify
ailocal check
```

**Open Docker Desktop and Ollama once** after installing them — both need a
first manual launch before anything can use them. `ailocal install` stops with a
clear message naming anything that is missing.

You do not need to install Python or create a virtual environment. Homebrew's
`pipx` brings its own Python, and pipx keeps ailocal in an isolated environment
for you.

Expect a download of roughly 6–40 GB of models, depending on your Mac.

## Use it

Open a new terminal, then:

```bash
claude-local        # Claude Code, against your local models
codex-local         # Codex CLI, against your local models
```

**VS Code Copilot Chat** — ailocal configures the local provider during
installation. Open VS Code and pick an `ailocal-*` model in the chat model
picker.

## Everyday commands

| Command | What it does |
|---|---|
| `ailocal install` | set everything up (run once) |
| `ailocal start` | bring the models and proxy up |
| `ailocal stop` | bring them down |
| `ailocal status` | what is loaded right now |
| `ailocal check` | is everything configured and working? |

`ailocal check` answers the whole question end to end — configuration, running
services, every model, and one real response — and prints the fixing command
next to anything that is wrong.

To upgrade: `pipx upgrade ailocal && ailocal start`.

## Supported tools

| Tool | Status |
|---|---|
| Claude Code | Fully supported — tools, web search, and Python language support |
| VS Code Copilot Chat | Supported for chat and code completion |
| Codex CLI | Configured and routed correctly, but interactive sessions do not finish — an upstream bug ([BerriAI/litellm#27442](https://github.com/BerriAI/litellm/issues/27442)) |

Any OpenAI- or Anthropic-compatible app also works directly: point it at
`http://127.0.0.1:4000` with the key from `~/.config/ailocal/.env`.

Local models are capable everyday assistants, not frontier models. Expect
strong routine work, not hosted Opus or GPT on the hardest problems.

## Your Mac's profile

ailocal measures your Mac's memory and chooses a profile automatically. You do
not need to think about this to install or use it.

| Memory | Main model | Context window | Models on disk |
|---|---|---|---|
| 16 GB | `qwen3.5:4b` | 64K | ~6 GB |
| 32 GB | `qwen3.5:9b` | 64K | ~9 GB |
| 64 GB | `gemma4:26b-mlx` | 96K | ~40 GB |
| 128 GB | `gemma4:26b-mlx` | 96K | ~40 GB (not yet validated on hardware) |

ailocal never picks a profile your machine cannot hold. To override it:
`ailocal profile use 32gb`, then `ailocal start`.

To change which model a profile uses, edit the file in
`~/.config/ailocal/profiles/` and run `ailocal start`. Your edits are preserved
across upgrades.

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
