# CLAUDE.md — ailocal repo primer

Agent primer for this repository. Keep it lean: every edit here should stay
under ~50 lines. Do not add a new doc file without merging or deleting an old one.

## What this repo is

Tooling to run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat)
against **local** models on Apple Silicon — no cloud, no code changes to the tools.

Architecture: **Ollama** runs models natively (Metal/MLX GPU). **LiteLLM** (one
Docker container, `127.0.0.1:4000`) sits in front as an OpenAI+Anthropic-compatible
proxy and exposes **role names** (`router`, `reasoner`, `coder`, `supervisor`,
`embed`) instead of raw model tags. Clients point at `localhost:4000`.

## Golden rule

**Use role names only** in client configs and scripts — never backend model tags
(`qwen3.6:35b-mlx`, etc.). Roles decouple configs from the models behind them.

## Where things live

- `config/models.yaml` — single source of truth for role → backend (gitignored;
  generated per machine by `install.sh` from `config/profiles/{16,32,64,128}gb.yaml`).
- `config/litellm/config.yaml` — proxy config. **Generated** — do not hand-edit.
- `config/clients/` — per-client templates (`codex-config.toml`, `claude-code.json`,
  `model_catalog.json`, `CLAUDE.md`, `env.sh`, `copilot/`).
- `scripts/` — `install.sh` (bootstrap), `install-clients.sh` (deploy client configs),
  `sync-models.sh` → `sync-models.py`, `start/stop/update/teardown`, `doctor.sh`,
  `smoke-test.sh`, `install-models.sh`.

## Generated files — never hand-edit

`sync-models.py` regenerates these from `models.yaml`: `config/litellm/config.yaml`
(role capability blocks), `config/clients/model_catalog.json`, and the backend
names in `README.md` and `config/clients/CLAUDE.md`. After changing a model:
edit `config/models.yaml` → `./scripts/sync-models.sh` → `./scripts/start.sh`.

## Verify

`./scripts/doctor.sh` (0=healthy, 2=degraded) and `./scripts/smoke-test.sh`.
`bash -n` any edited script. `./scripts/sync-models.sh` must produce zero diff.

## Conventions

Shell: `set -euo pipefail`, reuse the `info/warn/step/backup` helpers. Python: stdlib
only. Never commit `.env` or secrets; ports bind `127.0.0.1` only. No Claude commit
attribution — Victor's identity only. Never `git push` without explicit approval.
