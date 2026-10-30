# ailocal

**Run Claude Code, Codex CLI and VS Code Copilot Chat against local models on your Mac.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]() [![Memory](https://img.shields.io/badge/memory-16%20GB%20minimum-lightgrey.svg)]()

| Your tool | What you run |
|---|---|
| Claude Code | `claude-local` |
| Codex CLI | `codex-local` |
| VS Code | the `ailocal` models in Copilot Chat |

Local models, local data, one configuration.

## What ailocal does

ailocal serves models through Ollama, fronts them with an OpenAI- and Anthropic-compatible API on `127.0.0.1:4000`, and configures every supported client it finds. It measures your Mac, picks models that fit its memory, verifies every download against a pinned hash, and writes the client configuration for you. Nothing leaves your machine unless the agent calls web search or a vendor's public documentation service.

ailocal installs no software of its own. You install the prerequisites and whichever clients you want; every client is optional, and one you do not have is left alone.

**Requirements:** macOS on Apple Silicon, 16 GB unified memory minimum, Docker Desktop and Ollama.

## Install

### 1. Prerequisites

Install [Homebrew](https://brew.sh) if you do not have it, then:

```bash
brew install --cask docker-desktop ollama-app
brew install pipx
```

Open Docker Desktop and Ollama once from your Applications folder. Both need one manual launch. `pipx` brings its own Python; you do not install Python or manage a virtual environment.

Language servers are optional and only matter for Claude Code. Install the ones for languages you work in and ailocal enables the matching plugin:

```bash
npm i -g pyright                                    # Python
npm i -g typescript-language-server typescript      # TypeScript/JavaScript
brew install gopls                                  # Go
xcode-select --install                              # C/C++ (clangd); also needed to build the native runtime
```

### 2. Clients (optional)

```bash
brew install --cask claude-code           # Claude Code
brew install --cask codex                 # Codex CLI
brew install --cask visual-studio-code    # VS Code Copilot Chat
```

Open VS Code once after installing it; it creates its settings folder on first launch. ailocal also works with no client at all: any OpenAI- or Anthropic-compatible app can use the local API directly.

### 3. ailocal

```bash
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git@v0.15.1
ailocal install
```

That is the only install path. The tag is the current release; the same wheel is attached to [the release](https://github.com/DevelopSolutionsLLC/ailocal/releases/latest) for download and verification.

`ailocal install` detects your memory tier, pulls and verifies the models, builds the reviewed native runtime (Go and Xcode's command-line tools are required), starts the services, configures the clients it finds, and ends with `ailocal check`. It asks before touching any client configuration and exits nonzero with the fixing commands if validation fails. Allow 10 to 55 GB of model storage depending on your tier. It is safe to re-run.

### 4. Verify

```bash
ailocal check
```

Success is `CHECK: OK` on the last line. The **Clients** group shows what ailocal did with each supported client: `✓` configured, `—` not installed and left alone, `⚠` advisory with the fixing command on the next line.

## Use it

Open a new terminal, then:

```bash
claude-local        # Claude Code, against your local models
codex-local         # Codex CLI, against your local models
```

In either client, `/model` lists exactly the enabled `ailocal-*` capabilities with the same labels. Each selection carries its own context window and reasoning default. Installed a client later? Run `ailocal clients`.

### VS Code: paste the key once

VS Code keeps model API keys in its own encrypted storage, which no other program can write, so this one step is yours:

```bash
grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env
```

In Copilot Chat open the model picker, choose **Manage Language Models**, select **ailocal** and paste the key. The `ailocal-*` chat models appear immediately. Until then `ailocal check` reports `API key not initialized` and repeats these steps.

ailocal uses VS Code's built-in [Custom Endpoint provider](https://code.visualstudio.com/docs/agent-customization/language-models) and installs no extension. Copilot Chat needs no subscription for this. The bundled `research` and `agent` MCP servers are registered for VS Code and in [Copilot's portable scope](https://code.visualstudio.com/docs/agents/reference/mcp-configuration); foreign entries are preserved.

## Everyday commands

| Command | What it does |
|---|---|
| `ailocal install` | set everything up; safe to re-run |
| `ailocal install --upgrade-models` | adopt the shipped models in every tier, keeping your context and output budgets; originals are backed up |
| `ailocal install --reset-config` | take every shipped default, backing up your edits first |
| `ailocal start` / `ailocal stop` | bring the models and proxy up or down |
| `ailocal status` | what is loaded right now |
| `ailocal check` | is everything configured and working, end to end |
| `ailocal clients` | configure the supported clients you have installed |
| `ailocal update --json` | which pinned images have newer candidates |
| `ailocal audit` | are the pinned versions still free of advisories; nonzero when one is affected or yanked |

To upgrade ailocal itself, install the new release tag with `pipx install --force`, then `ailocal start`. That regenerates the runtime; it does not change Ollama, models or pinned service images on its own.

### Pinning Homebrew packages

ailocal holds its dependencies at validated versions, roughly two releases behind the newest. Homebrew has no minimum-age setting, so hold a version with a pin; `brew upgrade` skips pinned packages.

```bash
brew pin --cask codex claude-code@latest   # the clients ailocal is measured against
brew pin pipx
```

To move a pinned package: unpin, upgrade, run `ailocal check`, pin again. Docker Desktop, the Ollama app and VS Code ignore cask pins and update themselves; turn off automatic updates in each app. ailocal pins its service images by digest, so Docker Desktop updates never change LiteLLM or SearXNG.

## Supported clients

| Client | Status |
|---|---|
| Claude Code | Fully supported: tools, web search, language servers, local artifacts |
| Codex CLI | Supported: interactive and `exec` sessions, tools, file edits, web search, research MCP (measured on 0.163.0) |
| VS Code Copilot Chat | Supported for chat and inline completion |

Any other OpenAI- or Anthropic-compatible app works against `http://127.0.0.1:4000` with the key in `~/.local/state/ailocal/env`.

Local models are capable everyday assistants, not frontier models. Expect strong routine work; do not expect hosted Opus or GPT on the hardest problems.

## Your Mac's profile

ailocal measures your memory and chooses a profile. You do not need to think about this to install or use it.

| Memory | Main model | Context window | Models on disk |
|---|---|---|---|
| 16 GB | `qwen3.5:4b-mtp-q4_K_M` | 104K | ~10 GB |
| 32 GB | `qwen3.5:9b-calibrated-mtp-q4_K_M` | 136K | ~21 GB |
| 64 GB | `gemma4:26b-qat-mtp` | 192K | ~55 GB |
| 128 GB | `gemma4:26b-qat-mtp` | 256K | ~55 GB |

Windows are totals including each role's output reserve (K = 1,024 tokens). Roles that share the main model share its window; a long conversation compacts in the client before it reaches that window.

Every backend follows one [model-format rule](docs/maintenance.md#model-format): official or author-published builds, GGUF by default, MLX only with a same-model comparison on real hardware. Each model is a `name:tag` Ollama identity created from a hash-pinned source; nothing is converted. Every profile also exposes **Cyber** as `ailocal-cyber`, a separate unrestricted model with its own guide that releases its memory sooner when idle. What was measured, what failed and what remains unverified is in the [model audit](docs/model-audit.md).

To override the detected tier: `ailocal profile use 32gb`, then `ailocal start`. To change a model or its sampling, edit the file in `~/.config/ailocal/profiles/` and run `ailocal start`; your edits survive upgrades, and `ailocal install --upgrade-models` adopts new shipped models without discarding your budgets.

## What a local session gets

**Web search.** `claude-local` searches the live web through ailocal's own SearXNG instance and answers with sources. Claude Code may show "0 searches" on a sourced answer; that counter tracks Anthropic-hosted search, which never runs here. A Brave Search key is optional and used only by the research server within a budget: set `BRAVE_API` in `~/.config/ailocal/.env.local`, then `ailocal clients claude`. Details and limits are in [the architecture](docs/architecture.md#local-research).

**Research MCP.** Requests that need documentation, discussion, projects or papers route to the bundled `search` tool. Results carry provider attribution and are indexed snippets, not page reads. See [ADR 014](docs/adr/014-local-research.md).

**Artifacts.** The hosted `Artifact` tool is unavailable to a local session, so ailocal ships a local renderer. Ask for a diagram, page or report; the result opens from `127.0.0.1` in a sandboxed page that cannot reach the network, and its source is written to `.artifacts/` in your project. `html`, `markdown` and `mermaid` need nothing; `architecture` diagrams need `node` on PATH. See [ADR 011](docs/adr/011-bundled-artifacts.md).

**Language servers.** ailocal enables the official Claude Code LSP plugin for each language whose server you already have, in ailocal's own root only. Install a server, re-run `ailocal clients claude`, and the plugin follows.

| Language | Server binary | Install with |
|---|---|---|
| Python | `pyright-langserver` | `npm i -g pyright` |
| TypeScript | `typescript-language-server` | `npm i -g typescript-language-server typescript` |
| Go | `gopls` | `brew install gopls` |
| C/C++ | `clangd` | `xcode-select --install` |

**Model guidance and delegation.** Each profile carries a guide for every selected backend; Codex receives it with the selected model and Claude Code on session start and model switches. A hosted Claude or Codex session can delegate a bounded task to a local worker through the bundled `agent` MCP when Cadence declares it, in `text`, `research`, `edit` or `full` mode under a ceiling you set. The contract, including resume, image input and what a child can and cannot touch, is in [the architecture](docs/architecture.md#delegation).

A thinking-mode Qwen turn can end with reasoning and no final reply. ailocal reports that as a failure rather than switching models silently; see [troubleshooting](docs/troubleshooting.md#a-thinking-mode-turn-ends-with-no-reply).

## Defaults

| | Default | Change it with |
|---|---|---|
| Inference | Ollama, via LiteLLM on `127.0.0.1:4000` | — |
| Profile | chosen from your Mac's memory | `ailocal profile use <tier>`, or edit `~/.config/ailocal/profiles/` |
| LiteLLM and SearXNG | pinned by digest; versions reported by `ailocal check` | a deliberate upgrade, then the gate |
| Tool search (`claude-local`) | on (`ENABLE_TOOL_SEARCH=true`) | `AILOCAL_TOOL_SEARCH=…` |
| Native Workflow (`claude-local`) | off | `AILOCAL_NATIVE_WORKFLOWS=1` |
| Local artifacts | on, bundled | — |
| Web search | ailocal's own SearXNG, keyless | `BRAVE_API` adds budgeted Brave to research |
| Hosted Claude/Codex settings | public documentation MCPs only; kept after teardown | native client configuration |

Every `claude-local` variable is process-scoped: set it inline for one session. The full table is in [docs/claude-local.md](docs/claude-local.md#configuration).

`ailocal teardown` stops ailocal's services, restores stock Ollama's recorded startup preference, and keeps your models and profiles. See [maintenance](docs/maintenance.md#restoring-ollama-startup-on-teardown).

## Documentation

| Document | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | how the pieces fit, what an install writes, geometry, delegation |
| [docs/claude-local.md](docs/claude-local.md) | what claude-local is, tool search, Workflow, every variable |
| [docs/troubleshooting.md](docs/troubleshooting.md) | symptoms and fixes |
| [docs/security.md](docs/security.md) | trust boundaries, secrets, supply chain |
| [docs/maintenance.md](docs/maintenance.md) | keeping the stack current, the model-format rule, evidence |
| [docs/model-audit.md](docs/model-audit.md) | what was measured for each shipped model |
| [docs/adr/](docs/adr/) | the decisions and why |
| [CHANGELOG.md](CHANGELOG.md) · [RELEASING.md](RELEASING.md) | what changed in each release, and how one is cut |
| [AGENTS.md](AGENTS.md) | developing and validating ailocal |

## License

Apache-2.0. See [LICENSE](LICENSE).

Developed and maintained by Victor T. Chevalier for DevelopSolutions, LLC.
