#!/usr/bin/env bash
# test-tool-gateway.sh — negotiator tests inside the proxy image, where PyYAML,
# the real litellm and the mounted registry all exist. The host interpreter has
# no PyYAML, so a host run would skip the substance; this refuses to pretend.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "$CONTAINER is not running — start the stack first."; exit 1; }
docker cp "$ROOT/scripts/tests/tool-gateway-impl.py" "$CONTAINER:/tmp/tg-test.py" >/dev/null
exec docker exec \
  -e AILOCAL_GATEWAY_MODULE=/app/config/tool_gateway.py \
  -e AILOCAL_REGISTRY_MODULE=/app/config/capability_registry.py \
  -e AILOCAL_REGISTRY=/app/config/registry.yaml \
  -e AILOCAL_CONFIG_PATH=/app/config/config.yaml \
  -e AILOCAL_CAPABILITIES_JSON=/app/ailocal-config/capabilities.generated.json \
  "$CONTAINER" python /tmp/tg-test.py
