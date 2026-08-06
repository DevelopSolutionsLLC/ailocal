#!/usr/bin/env bash
# stop.sh — stop all ailocal Docker services
# Usage: ailocal stop [--volumes]
#
# --volumes  Also remove Docker volumes (DESTROYS all data — use with caution)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$(dirname "$0")/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/output.sh"

# ── Parse flags ────────────────────────────────────────────────────────────

REMOVE_VOLUMES=false
if [[ "${1:-}" == "--volumes" ]]; then
  REMOVE_VOLUMES=true
  warn "--volumes flag set: all Docker volumes will be removed."
  read -r -p "  This destroys all database and cache data. Are you sure? [y/N]: " confirm
  [[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ── Stop services ──────────────────────────────────────────────────────────

step "Stopping ailocal services"

if [ "$REMOVE_VOLUMES" = true ]; then
  dc down --volumes --remove-orphans
  info "Services stopped and volumes removed."
else
  dc down --remove-orphans
  info "Services stopped. Data volumes preserved."
  echo "  To also remove volumes: ailocal stop --volumes"
fi
