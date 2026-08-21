# ailocal

**Run Claude Code, Codex CLI, and VS Code Copilot Chat with local models on your Mac.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE) [![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]() [![Memory](https://img.shields.io/badge/memory-16%20GB%20minimum-lightgrey.svg)]()

| Your tool | What you run |
|---|---|
| Claude Code | `claude-local` |
| Codex CLI | `codex-local` |
| VS Code | local models in Copilot Chat |

Local models. Local data. One configuration.

---

## What is ailocal?

ailocal runs the coding tools you already use against models on your own machine. Nothing is sent to Anthropic or OpenAI, and there is nothing to pay per token.

**What it configures for you:** the models, the local API they are served on, and every supported client you have installed. You do not edit a config file by hand.

**What it does not do:** install software. You install the prerequisites and whichever clients you want; ailocal detects what is present and configures it. Every client is optional.

**Requirements:** macOS on Apple Silicon, 16 GB unified memory minimum.

---

## 1. Install prerequisites

If you do not have Homebrew, install it first from [brew.sh](https://brew.sh).

**Docker Desktop and Ollama are required.** They run the models and the local API; ailocal cannot work without them.

```bash
brew install --cask docker-desktop ollama-app
```

**Now open Docker Desktop and Ollama once, from your Applications folder.** Both need one manual launch before anything can use them.

**pipx** is how you install ailocal, not something ailocal needs at runtime:

```bash
brew install pipx
```

`pipx` brings its own Python — you do not need to install Python or set up a virtual environment.

**Language servers** are optional and only matter for Claude Code. Install the ones for
languages you work in; ailocal enables the matching plugin for each server it finds. See
[Language servers](#language-servers).

```bash
npm i -g pyright                                    # Python
npm i -g typescript-language-server typescript      # TypeScript/JavaScript
brew install gopls                                  # Go
xcode-select --install                              # C/C++ (clangd)
```

## 2. Install clients (optional)

Install whichever you want to use, or none. You can add one later at any time.

```bash
brew install --cask claude-code           # Claude Code
brew install --cask codex                 # Codex CLI
brew install --cask visual-studio-code    # VS Code Copilot Chat
```

**If you install VS Code, open it once** — it creates its settings folder on first launch, and ailocal cannot configure it before that.

ailocal works with no client at all: it still serves a local OpenAI- and Anthropic-compatible API at `http://127.0.0.1:4000` for any app you point at it.

## 3. Install ailocal

```bash
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git
ailocal install
```

`ailocal install` measures your Mac, picks models that fit its memory, downloads them, starts the services, and configures the clients it finds. It will ask you before touching any client configuration.

Expect a download of roughly 6–40 GB of models, depending on your Mac. Run it once; it is safe to re-run.

## 4. Verify

```bash
ailocal check
```

**Success is `CHECK: OK` on the last line.** The report is grouped by area; the **Clients** group shows what ailocal did with each supported client:

```
Clients
  ✓ Claude Code    configured
  — Codex CLI      not installed (optional)
      → brew install --cask codex
  ✓ VS Code        configured
```

- `✓` — configured and working.
- `—` — not installed. Not an error: ailocal left it alone. Install it and run `ailocal clients` if you want it.
- `⚠` — advisory. The line below it is the exact command that fixes it.

## Use it

Open a **new** terminal, then:

```bash
claude-local        # Claude Code, against your local models
codex-local         # Codex CLI, against your local models
```

Installed a client after ailocal? Run `ailocal clients` and it picks it up.

### VS Code: paste the key once

VS Code keeps model API keys in its own encrypted storage, which no other program can write to, so this one step is yours. Everything else on the VS Code side is already configured.

```bash
grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env
```

Then in VS Code: Copilot Chat → model picker → **Manage Models…** → **LiteLLM** → paste the key. The `ailocal-*` models appear in the picker right after.

Until you do, `ailocal check` reports `VS Code   provider configured; API key not initialized` and repeats these two steps.

Copilot Chat ships inside VS Code — there is no extension to install and no Copilot subscription needed.

## Everyday commands

| Command | What it does |
|---|---|
| `ailocal install` | set everything up (run once) |
| `ailocal install --reset-config` | re-take the shipped policy defaults, backing up your edits first |
| `ailocal start` | bring the models and proxy up |
| `ailocal stop` | bring them down |
| `ailocal status` | what is loaded right now |
| `ailocal check` | is everything configured and working? |
| `ailocal clients` | configure the supported clients you have installed |

`ailocal check` answers the whole question end to end — configuration, running services, every model, and one real response — and prints the fixing command next to anything that is wrong.

To upgrade: `pipx upgrade ailocal && ailocal start`.

## Supported tools

| Tool | Status |
|---|---|
| Claude Code | Fully supported — tools, web search, and Python language support |
| VS Code Copilot | Supported for chat and code completion |
| Codex CLI | Configured and routed correctly, but interactive sessions do not finish — an upstream bug ([BerriAI/litellm#27442](https://github.com/BerriAI/litellm/issues/27442)) |

Any OpenAI- or Anthropic-compatible app also works directly: point it at `http://127.0.0.1:4000` with the key in `~/.local/state/ailocal/env`.

Local models are capable everyday assistants, not frontier models. Expect strong routine work, not hosted Opus or GPT on the hardest problems.

## Language servers

ailocal enables the official Claude Code LSP plugin for each language **whose server binary
you already have**. It installs no language ecosystem, and reports the fixing command for a
language whose server is absent.

| Language | Server binary | You install it with | Plugin ailocal enables |
|---|---|---|---|
| Python | `pyright-langserver` | `npm i -g pyright` | `pyright-lsp` |
| TypeScript | `typescript-language-server` | `npm i -g typescript-language-server typescript` | `typescript-lsp` |
| Go | `gopls` | `brew install gopls` | `gopls-lsp` |
| C/C++ | `clangd` | `xcode-select --install` | `clangd-lsp` |

Install a server, re-run `ailocal clients claude`, and the plugin follows — in **ailocal's
own root only**. Plugin state is per config root, so this root is self-sufficient and
depends on nothing your hosted client has.

Your hosted `claude` is **yours**; ailocal never adds plugins to it. To get the same
languages there, run `/plugin` in a hosted session once.

There is no bash plugin in the official marketplace, so shell is covered by ShellCheck
instead — static analysis, not LSP.

## Web search

`claude-local` searches the live web: the local proxy intercepts the request, runs it against ailocal's own SearXNG instance, and returns results the model answers with sources.

Claude Code may display **0 searches** on a clearly sourced answer. That counter tracks Anthropic-hosted search, which never runs here; retrieval still happened.

**Brave Search is optional.** Without a key, search uses the keyless engines. Adding a Brave API key enables an API-backed general-web engine, which is more reliable for broad questions:

```bash
$EDITOR ~/.config/ailocal/.env.local    # set BRAVE_API=your-key
ailocal start                     # re-renders the search settings
```

The key stays in that file on your machine. It is never committed, never printed, and never leaves the local search container.

## Your Mac's profile

ailocal measures your Mac's memory and chooses a profile automatically. You do not need to think about this to install or use it.

| Memory | Main model | Context window | Models on disk |
|---|---|---|---|
| 16 GB | `qwen3.5:4b` | 96K | ~6 GB |
| 32 GB | `qwen3.5:9b` | 128K | ~9 GB |
| 64 GB | `gemma4:26b-mlx` | 192K | ~40 GB |
| 128 GB | `gemma4:26b-mlx` | 256K | ~40 GB (sized from model limits, not yet measured on hardware) |

ailocal never picks a profile your machine cannot hold. To override it: `ailocal profile use 32gb`, then `ailocal start`.

A long conversation compacts automatically before it reaches that window. Your client does the compacting; ailocal supplies the threshold. The full window stays available for one-off large requests.

To change which model a profile uses, edit the file in `~/.config/ailocal/profiles/` and run `ailocal start`. Your edits are preserved across upgrades.

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
