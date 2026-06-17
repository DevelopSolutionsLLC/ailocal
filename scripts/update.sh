#!/usr/bin/env bash
# update.sh — backup, pull latest images, update Ollama models, rolling restart
# Usage: ./scripts/update.sh [--skip-models]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Parse flags ────────────────────────────────────────────────────────────

SKIP_MODELS=false
[[ "${1:-}" == "--skip-models" ]] && SKIP_MODELS=true

# ── Backup first ───────────────────────────────────────────────────────────

step "Creating backup before update"
if ! "$ROOT_DIR/scripts/backup.sh"; then
  error "Backup failed — aborting update to protect your data."
  echo "  Fix the backup issue then retry." >&2
  exit 1
fi

# ── Pull updated images ────────────────────────────────────────────────────

step "Pulling latest Docker images"
docker compose pull

# ── Update Ollama models ───────────────────────────────────────────────────

if [ "$SKIP_MODELS" = false ]; then
  step "Updating Ollama models"
  "$ROOT_DIR/scripts/install-models.sh" || warn "Model update had warnings — services will still restart."
fi

# ── Rolling restart (dependency order) ────────────────────────────────────
# Restart infrastructure first, then dependents.

step "Restarting services"
docker compose up -d --remove-orphans

# ── Post-update health check ───────────────────────────────────────────────

step "Validating health post-update"
sleep 10  # Allow containers time to settle after restart
if "$ROOT_DIR/scripts/healthcheck.sh"; then
  step "Update complete — all services healthy."
else
  warn "Health check reported issues after update."
  echo "  Check logs: docker compose logs --tail=50 <service>"
  echo "  To roll back configs: ./scripts/restore.sh"
  exit 1
fi
