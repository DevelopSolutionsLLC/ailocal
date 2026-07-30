#!/usr/bin/env bash
# test-capability-registry.sh — run the Phase A registry tests inside the proxy
# image, where PyYAML and the mounted registry both exist. The host interpreter
# has no PyYAML, so a host run would skip the substance; this refuses to.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "$CONTAINER is not running — start the stack first."; exit 1; }
docker cp "$ROOT/scripts/test-capability-registry.py" "$CONTAINER:/tmp/tcr.py" >/dev/null
exec docker exec \
  -e AILOCAL_REGISTRY_MODULE=/app/config/capability_registry.py \
  -e AILOCAL_REGISTRY=/app/config/registry.yaml \
  -e AILOCAL_CONFIG_PATH=/app/config/config.yaml \
  -e AILOCAL_CAPABILITIES_JSON=/app/ailocal-config/capabilities.generated.json \
  -e AILOCAL_GATEWAY_SOURCE=/app/config/tool_gateway.py \
  "$CONTAINER" python /tmp/tcr.py
