#!/usr/bin/env bash
# sync-models.sh — propagate the active config/profiles/<tier>.yaml + config/clients.yaml
# to all derived files.
# Usage:
#   ./scripts/sync-models.sh          regenerate every derived file
#   ./scripts/sync-models.sh --check  regenerate, then fail if any TRACKED generated file changed
#                                     (drift check for pre-commit: sources and generated output are
#                                      out of sync). capabilities.generated.json is excluded — it
#                                      carries a timestamp and is gitignored.
#   ./scripts/sync-models.sh --resolve <capability>   print the active backend tag
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${1:-}" = "--check" ]; then
  GENERATED=(
    config/litellm/config.yaml
    config/clients/model_catalog.json
    config/clients/claude/settings.json
    config/clients/codex/config.toml
    config/clients/codex/plan.config.toml
    config/clients/codex/review.config.toml
    config/clients/continue/config.json
  )
  before="$(cd "$ROOT_DIR" && md5 -q "${GENERATED[@]}" 2>/dev/null)"
  python3 "$ROOT_DIR/scripts/sync-models.py" >/dev/null
  after="$(cd "$ROOT_DIR" && md5 -q "${GENERATED[@]}" 2>/dev/null)"
  if [ "$before" != "$after" ]; then
    echo "DRIFT — generated files were stale and have been regenerated. Review and commit:" >&2
    (cd "$ROOT_DIR" && git --no-pager diff --stat -- "${GENERATED[@]}") >&2 || true
    exit 1
  fi
  echo "[REAL] in sync — generated files match the active profile + config/clients.yaml"
  exit 0
fi

exec python3 "$ROOT_DIR/scripts/sync-models.py" "$@"
