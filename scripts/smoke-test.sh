#!/usr/bin/env bash
# smoke-test.sh — verify that LiteLLM can actually answer a real request
# Usage: ./scripts/smoke-test.sh [model]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

MODEL="${1:-router}"

if [ ! -f ".env" ]; then
  error ".env not found. Run ./scripts/install.sh first."
  exit 1
fi

API_KEY=$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
if [ -z "$API_KEY" ]; then
  error "LITELLM_MASTER_KEY is not set in .env"
  exit 1
fi

if ! has curl; then
  error "curl is required for smoke tests"
  exit 1
fi

step "Running smoke test for model: $MODEL"
payload=$(cat <<EOF
{"model":"$MODEL","messages":[{"role":"user","content":"Reply with exactly: smoke-ok"}],"temperature":0}
EOF
)

response=$(curl -sS -X POST http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  --data "$payload")

python3 - "$response" <<'PY'
import json
import sys

raw = sys.argv[1]
try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON response: {exc}") from exc

choices = data.get("choices") or []
if not choices:
    raise SystemExit(f"No choices returned: {raw}")

content = choices[0].get("message", {}).get("content", "")
if not isinstance(content, str) or not content.strip():
    raise SystemExit(f"Empty model response: {raw}")

print(content)
print("")
print("Smoke test passed")
PY
