#!/usr/bin/env bash
# env.sh — configure your shell session to use ailocal instead of cloud APIs
#
# Usage (add to ~/.zprofile for permanent setup, or run per-session):
#   source "~/ailocal/config/clients/env.sh"
#
# What this does:
#   Sets AILOCAL_BASE_URL as the single source of truth for the LiteLLM proxy.
#   Then derives all SDK-specific variables (OPENAI_*, ANTHROPIC_*) from it.
#   Claude Code, Codex CLI, Cowork, and any OpenAI/Anthropic SDK will route
#   to local Ollama models automatically — no changes to the tools themselves.

# ── Source of truth: AILOCAL_BASE_URL ────────────────────────────────────────
# Change this ONE value if your LiteLLM runs on a different host/port (e.g., LAN).
# All SDK variables below are derived from it.

AILOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
ENV_FILE="$AILOCAL_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "⚠  ailocal .env not found at: $ENV_FILE"
  echo "   Run: $AILOCAL_DIR/scripts/install.sh"
  return 1 2>/dev/null || exit 1
fi

LITELLM_KEY=$(grep '^LITELLM_MASTER_KEY=' "$ENV_FILE" | cut -d= -f2-)

if [ -z "$LITELLM_KEY" ]; then
  echo "⚠  LITELLM_MASTER_KEY not set in .env — run ./scripts/install.sh to generate it"
  return 1 2>/dev/null || exit 1
fi

export AILOCAL_BASE_URL="http://localhost:4000"

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

echo "✓ ailocal: routing AI requests to ${AILOCAL_BASE_URL}"
echo "  OpenAI:    OPENAI_BASE_URL=${OPENAI_BASE_URL}  OPENAI_API_KEY=<set>"
echo "  Anthropic: ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}  ANTHROPIC_API_KEY=<set>"
echo ""
echo "  Verify LiteLLM is running: curl ${AILOCAL_BASE_URL}/health/liveliness"
