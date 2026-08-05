#!/usr/bin/env bash
# Run a test implementation inside the running LiteLLM container.
#
# The gateway and registry suites assert against the modules as the proxy
# actually loads them, so they must execute in the image rather than against a
# checkout copy. This owns the plumbing — readiness, staging, environment,
# timeout, cleanup — while the suites own the assertions.
#
# Usage: in-container.sh <impl.py> [NAME=VALUE ...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
TIMEOUT="${AILOCAL_CONTAINER_TEST_TIMEOUT:-120}"

IMPL="${1:?usage: in-container.sh <impl.py> [NAME=VALUE ...]}"
shift
[ -f "$ROOT/$IMPL" ] || { echo "no such implementation: $IMPL" >&2; exit 1; }

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "$CONTAINER is not running — start the stack first." >&2; exit 1; }

# Unique staging directory so concurrent suites cannot overwrite each other.
STAGE="/tmp/ailocal-test-$$-$(basename "$IMPL" .py)"
cleanup() { docker exec "$CONTAINER" rm -rf "$STAGE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker exec "$CONTAINER" mkdir -p "$STAGE" >/dev/null
# harness.py travels with the implementation: the image has no checkout, so the
# suite would otherwise need its own copy of the shared mechanics.
docker cp "$ROOT/scripts/tests/harness.py" "$CONTAINER:$STAGE/harness.py" >/dev/null
docker cp "$ROOT/$IMPL" "$CONTAINER:$STAGE/impl.py" >/dev/null

# Paths the suites resolve inside the image, not in the checkout.
env_args=(
  -e "AILOCAL_REGISTRY_MODULE=/app/config/capability_registry.py"
  -e "AILOCAL_REGISTRY=/app/config/registry.yaml"
  -e "AILOCAL_CONFIG_PATH=/app/generated/config.yaml"
  -e "AILOCAL_CAPABILITIES_JSON=/app/generated/capabilities.json"
  -e "AILOCAL_TEST_REPO=$STAGE"
)
for kv in "$@"; do env_args+=(-e "$kv"); done

# timeout(1) is GNU coreutils and is not present on a stock macOS. Bound the run
# when it is available; run unbounded rather than failing when it is not.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

set +e
if [ -n "$TIMEOUT_BIN" ]; then
  "$TIMEOUT_BIN" "$TIMEOUT" docker exec "${env_args[@]}" "$CONTAINER" python "$STAGE/impl.py"
else
  docker exec "${env_args[@]}" "$CONTAINER" python "$STAGE/impl.py"
fi
rc=$?
set -e
[ "$rc" -eq 124 ] && echo "timed out after ${TIMEOUT}s inside $CONTAINER" >&2
exit "$rc"
