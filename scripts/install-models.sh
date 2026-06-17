#!/usr/bin/env bash
# install-models.sh — pull or update Ollama models for ailocal
# Usage: ./scripts/install-models.sh
#
# Models are pulled with their default quantization (q4_K_M for most).
# To use a specific quantization, append a tag: e.g. qwen3:8b-q8_0
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
# All 5 models combined ≈ 65 GB. Warn if less than 80 GB free.

FREE_GB=$(df -g "$HOME" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
if [ "$FREE_GB" -lt 80 ]; then
  warn "Only ${FREE_GB} GB free on disk. Full model set needs ~65 GB."
  echo "  Proceeding — skip large models if you run low."
fi

# ── Model list ─────────────────────────────────────────────────────────────
# Format: "name|size_hint|description"
#   qwen3:8b         ~5 GB   — fast inference, lightweight tasks (LiteLLM default)
#   qwen2.5:7b       ~5 GB   — fallback when qwen3:8b is unavailable
#   qwen3-coder:30b  ~19 GB  — coding tasks (LiteLLM coding route)
#   deepseek-r1:32b  ~20 GB  — deep reasoning (LiteLLM reasoning route)
#   nomic-embed-text ~300 MB — embeddings

declare -a MODELS=(
  "qwen3:8b"
  "qwen2.5:7b"
  "qwen3-coder:30b"
  "deepseek-r1:32b"
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
