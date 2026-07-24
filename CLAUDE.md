# CLAUDE.md — ailocal repo primer

Agent primer. Keep it under ~70 lines; merge or delete before adding a doc file.
User-facing setup/troubleshooting lives in README.md — don't duplicate it here.

## What this repo is

Tooling to run AI coding clients (Claude Code, Codex CLI, VS Code Copilot Chat)
against **local** models on Apple Silicon — no cloud, no code changes to the tools.

**Ollama** runs models natively (Metal/MLX). **LiteLLM** (one Docker container,
`127.0.0.1:4000`) fronts it as an OpenAI+Anthropic-compatible proxy exposing **capability
names** (`architecture`, `implementation`, `review`, `completion`, `embeddings`) instead of raw
model tags. The `local/*` namespace (internal agent API) and the `claude-*`/`gpt-*` compatibility
IDs (external client adapters that Claude Code / OpenAI SDK hard-code) are aliased onto capabilities
via a `model_group_alias` block **generated from `config/clients.yaml`** — one backend entry, many
client-facing names. The old ailocal role names (`coder-main`/`deep-think*`/`supervisor`/…) are
gone. Full map + lifecycle in `docs/MODEL_ARCHITECTURE.md`, `MODEL_ROUTING.md`, `MODEL_LIFECYCLE.md`.

## Golden rule

**Use capability names only** in client configs and scripts — never backend tags
(`qwen3-coder:30b`). Capabilities decouple configs from the models behind them, and the router owns
context, sampling, and per-role lifecycle (`keep_alive`). Agents request a capability; they never
name a model.

## The four non-obvious mechanisms

Most of this repo's complexity is in these; change them carefully.

1. **Generation.** Two source files drive everything: `config/models.yaml` (WHAT each
   capability is — backend `active`, context, sampling, keep_alive, metadata) and
   `config/clients.yaml` (WHICH capability each client surface uses + the `claude-*`/`gpt-*`
   compat map). `sync-models.py` regenerates, between GENERATED markers or as whole managed
   files: the `model_list` **and** `model_group_alias` in `config/litellm/config.yaml`,
   `config/capabilities.generated.json`, `config/clients/model_catalog.json`, and the
   `claude/settings.json` · `codex/config.toml` (+ profiles) · `continue/config.json` · copilot
   tables. Never hand-edit a generated region — edit the two sources and run
   `./scripts/sync-models.sh`, then `./scripts/install-clients.sh` to deploy. `models.yaml` is
   written by `install.sh` from the RAM tier in `config/profiles/{16,32,64,128}gb.yaml`.
   `sync-models.py --resolve <capability>` prints the active backend (used by the shell scripts).
2. **Persona injection.** `config/litellm/persona_injector.py` is a LiteLLM pre-call
   hook merging `config/personas/_core.md` + `<role>.md` into whatever system message
   the client sent — server-side, so every alias inherits it. Reasoners get **no**
   persona (DeepSeek's guidance), temp 0.6 / top-p 0.95. Caveat: LiteLLM issue #27518
   reports `async_pre_call_hook` being bypassed on the Anthropic `/v1/messages` route —
   the one Claude Code uses. Re-verify before relying on personas there.
3. **Reasoning vs. non-reasoning.** No installed model currently thinks — the `deep-think*`
   reasoners (deepseek-r1) were removed, and `qwen3-coder` is Qwen's non-thinking variant. Every
   capability carries `additional_drop_params: ["thinking", "reasoning_effort"]` (so Claude Code
   sending `thinking` to a non-thinking backend doesn't 400) **plus** `think: false` (suppresses a
   backend's default reasoning, which otherwise hangs VS Code Copilot). Both are required — dropping
   either reintroduces a real, previously-hit bug. The reasoning path (`reasoning`/`merge` flags →
   `merge_reasoning_content_in_choices`, no drop, no `think:false`) is still in `sync-models.py`; a
   commented `reasoner` slot in `config/models.yaml` restores the tier in one repoint (see
   `docs/MODEL_LIFECYCLE.md`).
4. **Client deployment is XDG-isolated.** Everything lands in `~/.config/ailocal/`;
   `~/.claude` and `~/.codex` are never touched, so cloud and local sessions coexist.
   `configure.zsh` defines the `claude-local` / `codex-local` / `ailocal-code` wrappers
   and is sourced from `.zshrc` between installer markers (`finalize.zsh` runs last).
   `CLAUDE_CONFIG_DIR` relocates `.claude.json` itself, so MCP registrations, history, and
   credentials are genuinely per-root — nothing leaks between local and cloud.

## Two shared boundaries with Cadence

ailocal and Cadence are independent *installations*, not independent *content*. Two seams matter:

1. **ailocal owns the Ollama daemon machine-wide** (`com.ailocal.ollama-env.plist`:
   `OLLAMA_MODELS=/Users/Shared/ollama/models`, keep-alive, parallelism), and Cadence's semantic
   index depends on it for `nomic-embed-text`. Tearing down ailocal breaks Cadence's indexing
   silently. Cadence's own `setup-ollama-env.sh` sets values only when unset, so it defers to this
   plist rather than fighting it — but the dependency direction is real and one-way.
2. **Cadence appends into this repo's deployed agents.** `cadence install claude --root
   ~/.config/ailocal/claude` writes a marker block (`<!-- cadence:start -->` …
   `<!-- cadence:end -->`) into `implementer/planner/reviewer/search/tester` in the *deployed* root,
   and links `repository-health` alongside. Cadence's `Explore/Plan/verify` are deliberately **not**
   linked — `search/planner/tester` already cover them, and adding both makes a nine-agent picker.
   **`install-clients.sh` rewrites those files and strips the block**, so the install order is
   cadence → ailocal → cadence-local. Re-run the Cadence installer afterwards;
   `cadence doctor --root ~/.config/ailocal/claude` reports `NO-OVERLAY` when it is missing.

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
