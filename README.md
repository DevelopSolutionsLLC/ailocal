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

**What it does not do:** install software. You install the prerequisites and whichever clients you want; ailocal detects what is present and configures it.

**Supported clients** — all optional:

- **Claude Code** — run `claude-local`
- **Codex CLI** — run `codex-local`
- **VS Code Copilot Chat** — pick an `ailocal-*` model in the chat model picker

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

You do not need to install Python or set up a virtual environment. Homebrew's `pipx` brings its own Python and keeps ailocal isolated for you.

**Language servers** are optional, and only matter if you use Claude Code. ailocal
wires up the official plugin for each server it finds, so the isolated
`claude-local` profile can answer "where is this defined" instead of re-reading
whole files. Install the ones for languages you actually work in:

```bash
npm i -g pyright                                    # Python
npm i -g typescript-language-server typescript      # TypeScript/JavaScript
brew install gopls                                  # Go
xcode-select --install                              # C/C++ (clangd)
```

Without any of them, `ailocal clients claude` reports what is missing and
everything else still works. ailocal installs no language server itself — see
[Language servers](#language-servers).

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

ailocal configures everything on the VS Code side except the key itself. VS Code keeps model API keys in its own encrypted storage, and offers no supported way for another program to write to it — so this one step is yours. It is a limitation of the VS Code boundary, not something ailocal skipped.

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

Claude Code has a built-in LSP tool — nothing to switch on. What it needs is a
language server behind it, and that takes **two halves**: the server binary, and
the official plugin that tells Claude Code to use it. A plugin on its own
configures the integration; it does not install the binary.

ailocal installs the official plugin for each language **whose server you already
have**, into the roots it creates, so `claude-local` is not blind where hosted
Claude can see. It never installs a language ecosystem: a language whose binary is
absent is skipped, with the command that would fix it.

| Language | Server binary | You install it with | Plugin ailocal enables |
|---|---|---|---|
| Python | `pyright-langserver` | `npm i -g pyright` | `pyright-lsp` |
| TypeScript | `typescript-language-server` | `npm i -g typescript-language-server typescript` | `typescript-lsp` |
| Go | `gopls` | `brew install gopls` | `gopls-lsp` |
| C/C++ | `clangd` | `xcode-select --install` | `clangd-lsp` |

Install a server, re-run `ailocal clients claude`, and the plugin follows — in
**ailocal's own root only**. Plugin state is per config root: a fresh root starts
with no marketplaces and no plugins whatever `~/.claude` holds, so the isolated
root is self-sufficient and nothing here depends on your hosted client.

Your hosted `claude` is **yours** — ailocal does not add plugins to it. If you
want the same languages there, run `/plugin` in a hosted session once. `cadence
capabilities` reports both roots separately so you can see which is missing what.

**There is no Bash plugin.** The official marketplace publishes 13 LSP plugins and
none of them is bash, so `bash-language-server` on PATH is unreachable from Claude
Code's LSP tool. Shell is covered by ShellCheck, which is static analysis, not LSP.

To check a server is genuinely working, ask for something only it can answer — a
definition or a reference — rather than trusting that the plugin is listed.
Installed, configured, and answering are three different states.

## Web search

Web search works. `claude-local` can search the live web: the request is intercepted by the local proxy, executed against ailocal's own SearXNG instance, and the results go back to the model, which answers with sources.

Claude Code may still display **0 searches** even when the answer is clearly sourced. That counter tracks Anthropic-hosted search, which never runs here; retrieval still happened.

**Brave Search is optional.** Without a key, search uses the keyless engines and keeps working — a fresh install needs nothing. Adding a Brave API key turns on an API-backed general-web engine, which is more reliable than the keyless ones for broad questions:

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
| 64 GB | `gemma4:26b-mlx` | 144K | ~40 GB |
| 128 GB | `gemma4:26b-mlx` | 256K | ~40 GB (sized from model limits, not yet measured on hardware) |

ailocal never picks a profile your machine cannot hold. To override it: `ailocal profile use 32gb`, then `ailocal start`.

A long conversation compacts automatically before it reaches that window — your client does the compacting, and ailocal supplies the point at which it starts, sized to what your hardware can process without a long pause. The full window stays available for one-off large requests.

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
