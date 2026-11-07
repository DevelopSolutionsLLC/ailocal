#!/usr/bin/env bash
# Manual opt-in: point this whole shell's OpenAI/Anthropic SDK env at LiteLLM.
# Redirects plain `claude`/`codex` too; claude-local/codex-local are scoped.

STATE_ENV="${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}/env"
USER_ENV="${AILOCAL_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/ailocal}/.env.local"

if [ ! -f "$STATE_ENV" ]; then
  echo "⚠  No ailocal environment at $STATE_ENV"
  echo "   Run: ailocal install"
  return 1 2>/dev/null || exit 1
fi
LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$STATE_ENV" | cut -d= -f2-)

# The user file may override the URL; it never holds the key.
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

# Ollama MLX backend on Apple Silicon (32GB+).
export OLLAMA_USE_MLX=1

export OPENAI_API_KEY="$LITELLM_KEY"
export OPENAI_BASE_URL="${AILOCAL_BASE_URL}/v1"

export ANTHROPIC_BASE_URL="${AILOCAL_BASE_URL}"
export ANTHROPIC_API_KEY="$LITELLM_KEY"

echo "✓ ailocal: routing AI requests to ${AILOCAL_BASE_URL} (this shell session only)"
echo "  OpenAI:    OPENAI_BASE_URL=${OPENAI_BASE_URL}  OPENAI_API_KEY=<set>"
echo "  Anthropic: ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}  ANTHROPIC_API_KEY=<set>"
echo ""
echo "  Verify LiteLLM is running: curl ${AILOCAL_BASE_URL}/health/liveliness"
