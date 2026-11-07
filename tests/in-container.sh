#!/usr/bin/env bash
# Run a test implementation inside the LiteLLM container, beside harness.py.
# Usage: in-container.sh <impl.py> [NAME=VALUE ...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"
TIMEOUT="${AILOCAL_CONTAINER_TEST_TIMEOUT:-180}"
IMPL="${1:?usage: in-container.sh <impl.py> [NAME=VALUE ...]}"
shift
[ -f "$ROOT/$IMPL" ] || { echo "no such implementation: $IMPL" >&2; exit 1; }
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  echo "$CONTAINER is not running; start the stack first." >&2; exit 1; }

# Per-run dir keeps suites apart; streaming (not docker cp) keeps ownership.
STAGE="/tmp/ailocal-test-$$-$(basename "$IMPL" .py)"
trap 'docker exec "$CONTAINER" rm -rf "$STAGE" >/dev/null 2>&1 || true' EXIT
docker exec "$CONTAINER" mkdir -p "$STAGE"
for pair in "harness.py:tests/harness.py" "impl.py:$IMPL"; do
  docker exec -i "$CONTAINER" sh -c 'umask 077; cat > "$1"' sh "$STAGE/${pair%%:*}" < "$ROOT/${pair#*:}"
done

env_args=()
for kv in "$@"; do env_args+=(-e "$kv"); done
# timeout(1) is absent on stock macOS; run unbounded there.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
set +e
${TIMEOUT_BIN:+"$TIMEOUT_BIN" "$TIMEOUT"} docker exec ${env_args[@]+"${env_args[@]}"} \
  "$CONTAINER" python "$STAGE/impl.py"
rc=$?
set -e
[ "$rc" -eq 124 ] && echo "timed out after ${TIMEOUT}s inside $CONTAINER" >&2
exit "$rc"
