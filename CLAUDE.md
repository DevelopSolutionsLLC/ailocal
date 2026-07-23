# CLAUDE.md — ailocal repo primer

Agent primer. Keep it under ~70 lines; merge or delete before adding a doc file.
User-facing setup/troubleshooting lives in README.md — don't duplicate it here.

## What this repo is

Tooling to run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat)
against **local** models on Apple Silicon — no cloud, no code changes to the tools.

**Ollama** runs models natively (Metal/MLX). **LiteLLM** (one Docker container,
`127.0.0.1:4000`) fronts it as an OpenAI+Anthropic-compatible proxy exposing **role
names** (`coder-main`, `coder-agent`, `coder-fast`, `deep-think`, `deep-think-more`,
`supervisor`, `embed`) instead of raw model tags. Claude/OpenAI names (`claude-*`,
`gpt-*`) are aliased onto roles via `router_settings.model_group_alias` — one backend
entry, many client-facing names.

## Golden rule

**Use role names only** in client configs and scripts — never backend tags
(`qwen3-coder:30b`). Roles decouple configs from the models behind them.

## The four non-obvious mechanisms

Most of this repo's complexity is in these; change them carefully.

1. **Generation.** `sync-models.py` regenerates the `model_list` block of
   `config/litellm/config.yaml` (between the GENERATED markers) and
   `config/clients/model_catalog.json` from `config/models.yaml`. Never hand-edit
   those regions. `models.yaml` itself is written by `install.sh` from the RAM tier
   in `config/profiles/{16,32,64,128}gb.yaml`.
2. **Persona injection.** `config/litellm/persona_injector.py` is a LiteLLM pre-call
   hook merging `config/personas/_core.md` + `<role>.md` into whatever system message
   the client sent — server-side, so every alias inherits it. Reasoners get **no**
   persona (DeepSeek's guidance), temp 0.6 / top-p 0.95. Caveat: LiteLLM issue #27518
   reports `async_pre_call_hook` being bypassed on the Anthropic `/v1/messages` route —
   the one Claude Code uses. Re-verify before relying on personas there.
3. **Reasoning vs. non-reasoning.** Only `deep-think*` think; their stream is
   **merged into the answer text** (`merge_reasoning_content_in_choices`) so VS Code
   renders visible `<think>` instead of a silent spinner. Every other role carries
   `additional_drop_params: ["thinking", "reasoning_effort"]` (so Claude Code sending
   `thinking` to a non-thinking backend doesn't 400) **plus** `think: false` (suppresses
   qwen3.6's default reasoning, which otherwise hangs VS Code Copilot). Both are
   required — dropping either one reintroduces a real, previously-hit bug.
4. **Client deployment is XDG-isolated.** Everything lands in `~/.config/ailocal/`;
   `~/.claude` and `~/.codex` are never touched, so cloud and local sessions coexist.
   `configure.zsh` defines the `claude-local` / `codex-local` / `ailocal-code` wrappers
   and is sourced from `.zshrc` between installer markers (`finalize.zsh` runs last).

## Where things live

- `config/models.yaml` — role → backend + num_ctx + sampling (the source of truth).
- `config/litellm/` — `config.yaml` (generated block + hand-kept aliases/fallbacks)
  and `persona_injector.py`.
- `config/personas/` — `_core.md` + per-role enhancers (`_`-prefixed = not a role).
- `config/clients/` — `configure.zsh`/`finalize.zsh`, `env.sh`, `model_catalog.json`,
  `scratchpad-hook.sh` (shared SessionStart hook → per-session
  `/tmp/scratchpad/<tool>-<session_id>/`), and per-client dirs `claude/` (settings,
  `agents/`, `commands/`), `codex/`, `copilot/`, `continue/`. Detail in its own
  `config/clients/CLAUDE.md`.
- `scripts/` — `install.sh`, `install-clients.sh`, `sync-models.sh` → `sync-models.py`,
  `start/stop/update/teardown`, `setup-ollama-env.sh`, `setup-startup.sh` (login
  LaunchAgents), `preload-model.sh`, `doctor.sh`, `smoke-test.sh`.

## Verify

`./scripts/doctor.sh` (0=healthy, 2=degraded), `./scripts/smoke-test.sh`,
`bash -n` any edited script, and `./scripts/sync-models.sh` must produce **zero diff**
on a second run. After editing a persona `.md`, restart the proxy — the hook reads
them at load.

## Conventions

Shell: `set -euo pipefail`, reuse the `info/warn/step/backup` helpers. Python: stdlib
only. Never commit `.env` or secrets; ports bind `127.0.0.1` only. No Claude commit
attribution — Victor's identity only. Never `git push` without explicit approval.
