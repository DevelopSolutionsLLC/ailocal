#!/usr/bin/env bash
# stop.sh — stop all ailocal Docker services
# Usage: ./scripts/stop.sh [--volumes]
#
# --volumes  Also remove Docker volumes (DESTROYS all data — use with caution)
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
  docker compose down --volumes --remove-orphans
  info "Services stopped and volumes removed."
else
  docker compose down --remove-orphans
  info "Services stopped. Data volumes preserved."
  echo "  To also remove volumes: ./scripts/stop.sh --volumes"
fi
