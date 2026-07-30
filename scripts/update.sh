#!/usr/bin/env bash
# update.sh — backup, pull latest images, update Ollama models, rolling restart
# Usage: ./scripts/update.sh [--skip-models]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$(dirname "$0")/lib/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Parse flags ────────────────────────────────────────────────────────────

SKIP_MODELS=false
[[ "${1:-}" == "--skip-models" ]] && SKIP_MODELS=true

# ── Snapshot .env first ─────────────────────────────────────────────────────
# The only non-git, non-regenerable state is .env (the master key). Config is
# in git and Ollama models re-pull, so a one-line snapshot is the whole backup.

step "Snapshotting .env before update"
if [ -f "$ROOT_DIR/.env" ]; then
  mkdir -p "$AILOCAL_STATE/backups"
  SNAP="$AILOCAL_STATE/backups/.env.$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$ROOT_DIR/.env" "$SNAP" && chmod 600 "$SNAP"
  info "Saved $SNAP"
else
  warn "No .env found — nothing to snapshot."
fi

# ── Pull updated images ────────────────────────────────────────────────────

step "Pulling latest Docker images"
dc pull

# ── Update Ollama models ───────────────────────────────────────────────────

if [ "$SKIP_MODELS" = false ]; then
  step "Updating Ollama models"
  bash "$ROOT_DIR/scripts/install-models.sh" || warn "Model update had warnings — services will still restart."
fi

# ── Regenerate model config (single source of truth) ──────────────────────
# Regenerate config.yaml / model_catalog.json / docs from models.yaml.
# Client configs are NOT auto-redeployed here — that would rewrite the user's
# ~/.codex and ~/.claude files on every update. Redeploy explicitly when you
# want to:  ailocal clients [claude|codex|vscode]

step "Regenerating model config (sync-models)"
bash "$ROOT_DIR/scripts/sync-models.sh"

# ── Rolling restart (dependency order) ────────────────────────────────────
# Restart infrastructure first, then dependents.

step "Restarting services"
dc up -d --remove-orphans
# Ensure LiteLLM reloads the regenerated model_info (config-only changes are
# not picked up by `up -d` when the image is unchanged).
dc restart litellm searxng

# ── Post-update health check ───────────────────────────────────────────────

step "Validating health post-update"
# Wait for LiteLLM to accept requests, then run doctor (the single health script).
attempts=0
until curl -sSf --max-time 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -ge 20 ] && break
  sleep 3
done
if bash "$ROOT_DIR/scripts/doctor.sh"; then
  step "Update complete — LiteLLM healthy."
else
  warn "Health check reported issues after update."
  echo "  Check logs: docker logs ailocal-litellm --tail=50"
  echo "  To roll back: git checkout the previous config, then ailocal start"
  exit 1
fi
