#!/usr/bin/env bash
# teardown.sh — completely remove ailocal Docker resources
# Usage: ./scripts/teardown.sh [--images]
#
# This stops all containers, removes their volumes, removes the Docker network,
# and optionally removes pulled images. It does NOT delete the ailocal directory
# itself or your .env / config files.
#
#   ./scripts/teardown.sh            # containers + volumes + network
#   ./scripts/teardown.sh --images   # also remove all pulled Docker images
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

REMOVE_IMAGES=false
[[ "${1:-}" == "--images" ]] && REMOVE_IMAGES=true

# ── Confirmation ───────────────────────────────────────────────────────────

step "ailocal teardown"
echo ""
echo "  This will permanently remove:"
echo "    • All ailocal containers"
echo "    • All Docker volumes (postgres data, redis cache, webui history)"
echo "    • The ailocal Docker network"
[ "$REMOVE_IMAGES" = true ] && echo "    • All pulled Docker images"
echo ""
echo "  Your .env and config files will NOT be touched."
echo "  Re-run ./scripts/install.sh + ./scripts/start.sh to rebuild."
echo ""
read -r -p "  Proceed? [y/N]: " confirm
[[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Stop containers and remove volumes ────────────────────────────────────

step "Stopping containers and removing volumes"
docker compose down --volumes --remove-orphans 2>/dev/null || true

# ── Remove Docker network (compose may have already done this) ─────────────

if docker network ls --format "{{.Name}}" | grep -q "^ailocal_net$"; then
  step "Removing Docker network"
  docker network rm ailocal_net 2>/dev/null || true
fi

# ── Optionally remove images ───────────────────────────────────────────────

if [ "$REMOVE_IMAGES" = true ]; then
  step "Removing Docker images"
  # Extract image names from docker-compose.yml and remove them
  grep '^\s*image:' docker-compose.yml \
    | awk '{print $2}' \
    | while read -r img; do
        if docker image inspect "$img" >/dev/null 2>&1; then
          docker rmi "$img" && info "removed $img" || warn "could not remove $img (may be in use elsewhere)"
        fi
      done
fi

# ── Clean up empty log files ───────────────────────────────────────────────

if [ -f "$ROOT_DIR/logs/caddy/access.log" ]; then
  truncate -s 0 "$ROOT_DIR/logs/caddy/access.log" 2>/dev/null || true
fi

step "Teardown complete."
echo ""
echo "  To fully reset and start fresh:"
echo "    ./scripts/install.sh     # re-generate .env if needed"
echo "    ./scripts/start.sh       # rebuild and start"
