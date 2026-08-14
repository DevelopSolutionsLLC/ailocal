#!/usr/bin/env bash
# env.sh — MANUAL opt-in only. Never auto-sourced by the installer.
#
# WARNING: sourcing this redirects the Anthropic AND OpenAI SDK env vars
# shell-wide for the rest of this shell session — including plain `claude`
# and `codex`, which will silently stop talking to the cloud and start
# talking to the local LiteLLM proxy instead. If you want an isolated,
# per-invocation local session instead, use the `claude-local` / `codex-local`
# wrapper functions (clients/configure.zsh, sourced by ~/.zshrc) —
# they scope the env to a single process and never touch the calling shell.
#
# Usage (manual, per-session):
#   source "~/ailocal/clients/env.sh"
#
# What this does:
#   Sets AILOCAL_BASE_URL as the single source of truth for the LiteLLM proxy.
#   Then derives all SDK-specific variables (OPENAI_*, ANTHROPIC_*) from it.

# ── Source of truth: the canonical generated master key ──────────────────────
# ONE OWNER. This used to read `~/.config/ailocal/env`, a projection holding a
# SECOND copy of the master key under a different name — which a key rotation
# would leave stale while it still looked authoritative. The generated state
# file is the key's only home; the base URL is derived, not stored.

STATE_ENV="${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/env"
USER_ENV="${AILOCAL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/ailocal}/.env.local"

if [ ! -f "$STATE_ENV" ]; then
  echo "⚠  No ailocal environment at $STATE_ENV"
  echo "   Run: ailocal install"
  return 1 2>/dev/null || exit 1
fi
LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$STATE_ENV" | cut -d= -f2-)

# The user file may override the port the proxy is reached on; it never holds
# the key.
AILOCAL_BASE_URL_VAL="${AILOCAL_PROXY:-http://127.0.0.1:${AILOCAL_LITELLM_PORT:-4000}}"
if [ -f "$USER_ENV" ]; then
  _override=$(grep '^AILOCAL_BASE_URL=' "$USER_ENV" | cut -d= -f2-)
  [ -n "$_override" ] && AILOCAL_BASE_URL_VAL="$_override"
  unset _override
fi

if [ -z "$LITELLM_KEY" ]; then
  echo "⚠  No API key found — run ailocal clients to generate one"
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
