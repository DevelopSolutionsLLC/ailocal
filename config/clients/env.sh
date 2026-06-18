#!/usr/bin/env bash
# env.sh — configure your shell session to use ailocal instead of cloud APIs
#
# Usage (add to ~/.zprofile for permanent setup, or run per-session):
#   source "/path/to/ailocal/config/clients/env.sh"
#
# What this does:
#   Sets ANTHROPIC_BASE_URL and OPENAI_API_BASE to the local LiteLLM proxy.
#   Claude Code, Cowork, Codex CLI, and any OpenAI/Anthropic SDK will route
#   to local Ollama models automatically — no changes to the tools themselves.

AILOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
ENV_FILE="$AILOCAL_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "⚠  ailocal .env not found at: $ENV_FILE"
  echo "   Run: $AILOCAL_DIR/scripts/install.sh"
  return 1 2>/dev/null || exit 1
fi

export LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$ENV_FILE" | cut -d= -f2-)

if [ -z "$LITELLM_KEY" ]; then
  echo "⚠  LITELLM_MASTER_KEY not set in .env — run ./scripts/install.sh to generate it"
  return 1 2>/dev/null || exit 1
fi

# ── Anthropic SDK (Claude Code, Cowork, anthropic-sdk) ─────────────────────
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY="$LITELLM_KEY"

# ── OpenAI SDK (Codex CLI, Continue, Cline, openai-sdk) ────────────────────
export OPENAI_API_BASE=http://localhost:4000/v1
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY="$LITELLM_KEY"

echo "✓ ailocal: routing AI requests to http://localhost:4000"
echo "  Anthropic: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY set"
echo "  OpenAI:    OPENAI_BASE_URL + OPENAI_API_KEY set"
echo ""
echo "  Verify LiteLLM is running: curl http://localhost:4000/health/liveliness"
