#!/usr/bin/env bash
# replay-tool-captures.sh — run the captured real client payloads through the
# real policy, inside the proxy container (where PyYAML and the mounted policy
# both live). Read the drop list before enabling AILOCAL_TOOL_GATEWAY=filter.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "$CONTAINER is not running — start the stack first (./scripts/start.sh)."
  exit 1
fi

docker cp "$ROOT/scripts/replay-tool-captures.py" \
  "$CONTAINER:/tmp/replay-tool-captures.py" >/dev/null
exec docker exec \
  -e AILOCAL_GATEWAY_MODULE=/app/config/tool_gateway.py \
  -e AILOCAL_CAPTURES=/app/captures \
  -e AILOCAL_REGISTRY=/app/config/registry.yaml \
  -e AILOCAL_CONFIG_PATH=/app/config/config.yaml \
  -e AILOCAL_CAPABILITIES_JSON=/app/ailocal-config/capabilities.generated.json \
  "$CONTAINER" python /tmp/replay-tool-captures.py
