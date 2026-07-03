#!/usr/bin/env bash
# install-models.sh — pull or update Ollama models for ailocal
# Usage: ./scripts/install-models.sh
#
# Model list is derived automatically from config/litellm/config.yaml — no
# separate list to maintain here. To add or change a model, update the config.
#
# Run this after 'ollama serve' is confirmed running.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

OLLAMA="${OLLAMA_CLI:-ollama}"
MODELS_YAML="$ROOT_DIR/config/models.yaml"

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Pre-flight ─────────────────────────────────────────────────────────────

step "Pre-flight checks"

if ! has "$OLLAMA"; then
  error "Ollama CLI not found. Install from: https://ollama.ai/download"
  exit 1
fi
info "Ollama CLI present"

if ! "$OLLAMA" list >/dev/null 2>&1; then
  error "Ollama daemon is not responding."
  echo "  Start it with: ollama serve   (or open /Applications/Ollama.app)" >&2
  exit 1
fi
info "Ollama daemon responding"

if [ ! -f "$MODELS_YAML" ]; then
  error "Model manifest not found: $MODELS_YAML"
  exit 1
fi
info "Model manifest found"

# ── Disk space check ───────────────────────────────────────────────────────
# Read expected disk usage from models.yaml (set per hardware profile).

DISK_NEEDED=$(grep '^disk_gb:' "$MODELS_YAML" | awk '{print $2}' || echo 0)
DISK_WARN=$((DISK_NEEDED + DISK_NEEDED / 4))   # warn at 125% of model size
FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
if [ "$DISK_NEEDED" -gt 0 ] && [ "$FREE_GB" -lt "$DISK_WARN" ]; then
  warn "Only ${FREE_GB} GB free. This profile needs ~${DISK_NEEDED} GB."
  echo "  Proceeding — skip large models if you run low."
fi

# ── Model list — derived from config/models.yaml ──────────────────────────

MODELS=()
while IFS= read -r _m; do MODELS+=("$_m"); done < <(
  grep '^\s*backend:' "$MODELS_YAML" | sed 's/.*backend:[[:space:]]*//'
)

# ── Pull models ────────────────────────────────────────────────────────────

step "Installing/updating Ollama models"

# Get currently installed model names (first column, skip header row)
INSTALLED=$("$OLLAMA" list 2>/dev/null | awk 'NR>1 {print $1}')

for model in "${MODELS[@]}"; do
  if echo "$INSTALLED" | grep -qF "$model"; then
    info "$model  (already installed)"
  else
    echo "  ↓ Pulling $model ..."
    if "$OLLAMA" pull "$model"; then
      info "$model  pulled"
    else
      warn "$model  failed to pull — skipping"
    fi
  fi
done

# ── Summary ────────────────────────────────────────────────────────────────

step "Installed models"
"$OLLAMA" list

step "Done."
