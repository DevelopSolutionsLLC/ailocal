#!/usr/bin/env bash
# test-tool-gateway.sh — run the gateway known-answer tests in both places that
# matter.
#
#   host      — fast, stdlib only, no PyYAML: the policy tests SKIP here.
#   container — the image that actually serves requests, with the real litellm
#               and PyYAML: the policy tests and the real token counter run.
#
# The container pass is the one that counts. The host pass exists so the suite
# is runnable without Docker, and it says out loud which checks it skipped
# rather than reporting a green run over a reduced set.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*"; }

status=0

info "Host run (PyYAML absent -> policy tests skip)"
python3 "$ROOT/scripts/test-tool-gateway.py" || status=1

echo
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  info "Container run in $CONTAINER (real litellm + PyYAML)"
  # config/litellm is bind-mounted at /app/config, so the container already sees
  # the module under test. Only the test file needs copying in.
  docker cp "$ROOT/scripts/test-tool-gateway.py" "$CONTAINER:/tmp/tg-test.py" >/dev/null
  docker exec \
    -e AILOCAL_GATEWAY_MODULE=/app/config/tool_gateway.py \
    "$CONTAINER" python /tmp/tg-test.py || status=1
else
  warn "$CONTAINER is not running — the policy tests and the real token"
  warn "counter were NOT exercised. This run is incomplete, not green."
  status=1
fi

echo
if [ "$status" -ne 0 ]; then
  echo "TOOL GATEWAY SUITE: FAILED OR INCOMPLETE"
else
  echo "TOOL GATEWAY SUITE: OK (host + container)"
fi
exit "$status"
