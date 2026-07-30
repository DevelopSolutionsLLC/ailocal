#!/usr/bin/env bash
# env.sh — MANUAL opt-in only. Never auto-sourced by the installer.
#
# WARNING: sourcing this redirects the Anthropic AND OpenAI SDK env vars
# shell-wide for the rest of this shell session — including plain `claude`
# and `codex`, which will silently stop talking to the cloud and start
# talking to the local LiteLLM proxy instead. If you want an isolated,
# per-invocation local session instead, use the `claude-local` / `codex-local`
# wrapper functions (config/clients/configure.zsh, sourced by ~/.zshrc) —
# they scope the env to a single process and never touch the calling shell.
#
# Usage (manual, per-session):
#   source "~/ailocal/config/clients/env.sh"
#
# What this does:
#   Sets AILOCAL_BASE_URL as the single source of truth for the LiteLLM proxy.
#   Then derives all SDK-specific variables (OPENAI_*, ANTHROPIC_*) from it.

# ── Source of truth: AILOCAL_BASE_URL / AILOCAL_API_KEY ──────────────────────
# Prefer the installed config dir (written by install-clients.sh); fall back
# to the repo .env for a dev checkout that hasn't run the installer yet.

CFG_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal/env"
AILOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
REPO_ENV_FILE="$AILOCAL_DIR/.env"

if [ -f "$CFG_ENV" ]; then
  AILOCAL_BASE_URL_VAL=$(grep '^AILOCAL_BASE_URL=' "$CFG_ENV" | cut -d= -f2-)
  LITELLM_KEY=$(grep '^AILOCAL_API_KEY=' "$CFG_ENV" | cut -d= -f2-)
elif [ -f "$REPO_ENV_FILE" ]; then
  AILOCAL_BASE_URL_VAL="http://localhost:4000"
  LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$REPO_ENV_FILE" | cut -d= -f2-)
else
  echo "⚠  No ailocal env found at $CFG_ENV or $REPO_ENV_FILE"
  echo "   Run: ./scripts/install-clients.sh   (or ./scripts/install.sh first)"
  return 1 2>/dev/null || exit 1
fi

if [ -z "$LITELLM_KEY" ]; then
  echo "⚠  No API key found — run ./scripts/install-clients.sh to generate one"
  return 1 2>/dev/null || exit 1
fi

export AILOCAL_BASE_URL="$AILOCAL_BASE_URL_VAL"

# Enables Ollama's MLX backend on Apple Silicon (32GB+). Also set in ~/.zshrc for ollama serve.
export OLLAMA_USE_MLX=1

# ── Derived variables — do not edit these manually ────────────────────────────
# They all come from AILOCAL_BASE_URL and LITELLM_KEY above.

# OpenAI SDK (Codex CLI, Continue, Cline, openai-sdk)
export OPENAI_API_KEY="$LITELLM_KEY"
export OPENAI_BASE_URL="${AILOCAL_BASE_URL}/v1"

# Anthropic SDK (Claude Code, Cowork, anthropic-sdk)
export ANTHROPIC_BASE_URL="${AILOCAL_BASE_URL}"
export ANTHROPIC_API_KEY="$LITELLM_KEY"

echo "✓ ailocal: routing AI requests to ${AILOCAL_BASE_URL} (this shell session only)"
echo "  OpenAI:    OPENAI_BASE_URL=${OPENAI_BASE_URL}  OPENAI_API_KEY=<set>"
echo "  Anthropic: ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}  ANTHROPIC_API_KEY=<set>"
echo ""
echo "  Verify LiteLLM is running: curl ${AILOCAL_BASE_URL}/health/liveliness"
