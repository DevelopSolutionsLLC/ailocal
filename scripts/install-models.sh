#!/usr/bin/env bash
# install-models.sh — pull or update Ollama models for ailocal
# Usage: ./scripts/install-models.sh
#
# These are the backend models mapped to role-based aliases in LiteLLM:
#   router     → qwen3:8b           ~5 GB   — fast classification and routing
#   coder      → qwen3.6:27b        ~17 GB  — implementation and generation
#   reasoner   → deepseek-r1:32b    ~20 GB  — planning and deep reasoning
#   supervisor → gemma4:31b      ~20 GB  — review and approval gate (Google DeepMind)
#   embed      → nomic-embed-text   ~300 MB — semantic retrieval only
#
# Run this after 'ollama serve' is confirmed running.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

OLLAMA="${OLLAMA_CLI:-ollama}"

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

# ── Disk space check ───────────────────────────────────────────────────────
# All 5 models combined ≈ 63 GB. Warn if less than 80 GB free.

FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
if [ "$FREE_GB" -lt 80 ]; then
  warn "Only ${FREE_GB} GB free on disk. Full model set needs ~45+ GB."
  echo "  Proceeding — skip large models if you run low."
fi

# ── Model list — backend names for Ollama (role mapping in config/litellm/config.yaml) ──

declare -a MODELS=(
  "qwen3:8b"
  "qwen3.6:27b"
  "deepseek-r1:32b"
  "gemma4:31b"
  "nomic-embed-text"
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
