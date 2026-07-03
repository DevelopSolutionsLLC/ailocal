#!/usr/bin/env bash
# sync-models.sh — propagate config/models.yaml to all derived files
# Usage: ./scripts/sync-models.sh
#
# Edit config/models.yaml to change a model, then run this.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT_DIR/scripts/sync-models.py" "$@"
