#!/usr/bin/env bash
# teardown.sh — completely remove ailocal Docker resources
# Usage: ./scripts/teardown.sh [--images] [--clients]
#
# This stops all containers, removes their volumes, removes the Docker network,
# and optionally removes pulled images. It does NOT delete the ailocal directory
# itself or your .env / config files.
#
#   ./scripts/teardown.sh              # containers + volumes + network
#   ./scripts/teardown.sh --images     # also remove all pulled Docker images
#   ./scripts/teardown.sh --clients    # also uninstall claude-local/codex-local
#                                       # shell integration (see below)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Single source of truth for how this stack is composed (deploy/litellm + deploy/searxng).
AILOCAL_ROOT="$ROOT_DIR"
. "$(dirname "$0")/lib/compose.sh"

# ── Helpers ────────────────────────────────────────────────────────────────

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/output.sh"

# ── Parse flags ────────────────────────────────────────────────────────────

REMOVE_IMAGES=false
REMOVE_CLIENTS=false
for arg in "$@"; do
  case "$arg" in
    --images)  REMOVE_IMAGES=true ;;
    --clients) REMOVE_CLIENTS=true ;;
  esac
done

# ── Confirmation ───────────────────────────────────────────────────────────

step "ailocal teardown"
echo ""
echo "  This will permanently remove:"
echo "    • All ailocal containers"
echo "    • The ailocal Docker network"
[ "$REMOVE_IMAGES" = true ] && echo "    • All pulled Docker images"
if [ "$REMOVE_CLIENTS" = true ]; then
  echo "    • The claude-local/codex-local shell integration (~/.zshrc lines + ~/.config/ailocal/)"
fi
echo ""
echo "  Your .env and repo config files will NOT be touched."
echo "  Re-run ./scripts/install.sh + ailocal start to rebuild."
echo ""
read -r -p "  Proceed? [y/N]: " confirm
[[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Stop containers and remove volumes ────────────────────────────────────

step "Stopping containers and removing volumes"
dc down --volumes --remove-orphans 2>/dev/null || true

# ── Remove Docker network (compose may have already done this) ─────────────

if docker network ls --format "{{.Name}}" | grep -q "^ailocal_net$"; then
  step "Removing Docker network"
  docker network rm ailocal_net 2>/dev/null || true
fi

# ── Optionally remove images ───────────────────────────────────────────────

if [ "$REMOVE_IMAGES" = true ]; then
  step "Removing Docker images"
  # Extract image names from the deploy/ compose files and remove them
  grep -h '^\s*image:' deploy/litellm/compose.yaml deploy/searxng/compose.yaml \
    | awk '{print $2}' \
    | while read -r img; do
        if docker image inspect "$img" >/dev/null 2>&1; then
          docker rmi "$img" && info "removed $img" || warn "could not remove $img (may be in use elsewhere)"
        fi
      done
fi

# ── Optionally uninstall claude-local/codex-local shell integration ───────

if [ "$REMOVE_CLIENTS" = true ]; then
  step "Removing claude-local/codex-local shell integration"

  RC="${ZDOTDIR:-$HOME}/.zshrc"
  if [ -f "$RC" ] && grep -qE '# ailocal-configure|# ailocal-finalize' "$RC" 2>/dev/null; then
    ts=$(date +%Y%m%d_%H%M%S)
    cp "$RC" "${RC}.bak.${ts}"
    info "Backed up: $(basename "$RC") → $(basename "$RC").bak.${ts}"
    python3 - "$RC" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
lines = s.splitlines(keepends=True)
lines = [l for l in lines if '# ailocal-configure' not in l and '# ailocal-finalize' not in l]
s = ''.join(lines)
s = re.sub(r'\n{3,}', '\n\n', s)
s = s.rstrip('\n') + '\n'
open(p, "w", encoding="utf-8").write(s)
PY
    info "Removed ailocal-configure/ailocal-finalize lines from ~/.zshrc"
  else
    warn "No ailocal-configure/ailocal-finalize lines found in ~/.zshrc — nothing to remove"
  fi

  AILOCAL_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/ailocal"
  if [ -d "$AILOCAL_CFG" ]; then
    if [ -f "$AILOCAL_CFG/env" ]; then
      ts=$(date +%Y%m%d_%H%M%S)
      cp "$AILOCAL_CFG/env" "$AILOCAL_CFG/env.bak.${ts}"
      cp "$AILOCAL_CFG/env.bak.${ts}" "$HOME/ailocal-env.bak.${ts}" 2>/dev/null || true
      info "Backed up $AILOCAL_CFG/env → ~/ailocal-env.bak.${ts}"
    fi
    rm -rf "$AILOCAL_CFG"
    info "Removed $AILOCAL_CFG"
  else
    warn "$AILOCAL_CFG does not exist — nothing to remove"
  fi

  echo "  ~/.claude and ~/.codex were never touched by ailocal — nothing to revert there."
fi

step "Teardown complete."
echo ""
echo "  To fully reset and start fresh:"
echo "    ./scripts/install.sh     # re-generate .env if needed"
echo "    ailocal start       # rebuild and start"
