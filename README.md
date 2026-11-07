# ailocal

Run Claude Code, Codex CLI and VS Code Copilot Chat against local models on your Mac. ailocal serves models through Ollama behind an OpenAI- and Anthropic-compatible proxy on `127.0.0.1:4000`, picks models that fit your memory, verifies every download against a pinned hash, and configures each supported client it finds. Nothing leaves your machine unless the agent searches the web or a public documentation service.

**Requirements:** macOS on Apple Silicon, 16 GB unified memory, [Homebrew](https://brew.sh). Allow 10–55 GB for models, depending on tier.

## Install

```bash
brew install --cask docker-desktop ollama-binary     # open Docker Desktop once afterwards
brew install pipx ripgrep cosign
brew install --cask claude-code codex visual-studio-code   # any you want; open VS Code once
pipx install git+https://github.com/DevelopSolutionsLLC/ailocal.git@v0.15.1
ailocal install
ailocal check                                        # success: CHECK: OK on the last line
```

`ailocal install` asks before touching client configuration, is safe to re-run, and refuses while `/Applications/Ollama.app` exists. Optional language servers for Claude Code are in the [guide](docs/guide.md#what-a-local-session-gets).

## Use

```bash
claude-local        # Claude Code against your local models (open a new terminal first)
codex-local         # Codex CLI against your local models
```

**VS Code** keeps API keys where no other program can write them, so paste the key once:

1. `grep LITELLM_MASTER_KEY ~/.local/state/ailocal/env`
2. In Copilot Chat: model picker → **Manage Language Models** → **ailocal**.
3. Paste the key; the `ailocal-*` models appear immediately.

| Command | Does |
|---|---|
| `ailocal install` | set up or repair everything; `--upgrade-models` adopts new shipped models, `--reset-config` takes all shipped defaults |
| `ailocal start` / `stop` | bring the models and proxy up or down |
| `ailocal status` | what is loaded now |
| `ailocal check` | verify the whole installation end to end |
| `ailocal clients` | configure clients installed later; `--remove` reverses it |
| `ailocal profile use <tier>` | override the detected tier, then `ailocal start` |
| `ailocal bind lan` / `local` | expose the proxy on the network, or not (default `local`) |
| `ailocal docker check` / `apply` / `restore` | compare or apply ailocal's Docker Desktop settings |
| `ailocal update --json` / `ailocal audit` | newer pinned-image candidates / advisories against the pins |
| `ailocal teardown` | remove ailocal, keeping models and profiles |

| Memory | Main model | Context window | Models on disk |
|---|---|---|---|
| 16 GB | `qwen3.5:4b-mtp` | 96K | ~10 GB |
| 32 GB | `qwen3.5:9b-calibrated-mtp` | 160K | ~15 GB |
| 64 GB | `gemma4:26b-qat-mtp` | 192K | ~37 GB |
| 128 GB | `gemma4:26b-qat-mtp` | 256K | ~37 GB |

## Documentation

- [docs/guide.md](docs/guide.md): clients, VS Code, profiles, variables, pinning, removal, troubleshooting
- [docs/architecture.md](docs/architecture.md): how it works, what an install writes, security
- [docs/maintenance.md](docs/maintenance.md): keeping the stack current, model rules, the model audit, releasing
- [docs/decisions.md](docs/decisions.md): the design decisions and the alternatives rejected
- [AGENTS.md](AGENTS.md): developing ailocal · [CHANGELOG.md](CHANGELOG.md): what changed

## License

Apache-2.0, see [LICENSE](LICENSE). Maintained by Victor T. Chevalier for DevelopSolutions, LLC.
