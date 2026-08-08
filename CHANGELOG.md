# Changelog

Release policy and the meaning of each bump: [RELEASING.md](RELEASING.md).

## v0.9.0 — first public release

ailocal provides a local AI development environment for Apple Silicon Macs,
integrating local models with Claude Code, Codex CLI, and VS Code Copilot
through a single local gateway.

Everything runs on your own hardware using Ollama and Docker. Supported clients
are configured automatically when present, while remaining optional.

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

Clients are optional. ailocal configures only the clients installed on your
machine.

**Hardware-aware configuration**

Automatically selects appropriate model profiles for Apple Silicon systems with:

- 16 GB
- 32 GB
- 64 GB
- 128 GB (experimental)

No manual profile editing is required for typical installations.

**Local API**

Provides both an OpenAI-compatible API and an Anthropic-compatible API, allowing
local models to work with existing tooling.

**Validation**

`ailocal check` performs an end-to-end validation of prerequisites, runtime,
local gateway, models, client configuration, and live inference — with
actionable remediation when something is missing.

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
- VS Code requires one manual API key paste into its encrypted SecretStorage.
  This is a limitation of the VS Code extension model rather than ailocal.
- 128 GB hardware profiles remain lightly validated compared to the other
  configurations.

### Stability

This release establishes the initial public interface for ailocal. Future
releases will aim to preserve compatibility for CLI commands, configuration
layout, generated client configuration, and profile behaviour. Breaking changes
will be documented in release notes.

### Thank you

Thanks to everyone who tested early versions, reported issues, and helped
improve the project through repeated installation, packaging, and validation
testing.
