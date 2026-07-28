#!/usr/bin/env bash
# check-litellm-version.sh — the RUNNING LiteLLM must be the one we validated.
#
# `main-stable` is a floating tag. It moved under this project: the runtime was
# 1.93.0 while CLAUDE.md claimed 1.92.0, so behaviour recorded as "verified on
# 1.92.0" had been verified against a version no longer running. Documentation,
# validation environment and runtime must name the same release, and drift must
# be LOUD rather than discovered months later while debugging something else.
#
# Exit 0 = match. Exit 2 = drift. Exit 1 = could not determine (also a failure:
# an unverifiable version is not a passing one).
set -euo pipefail

CONTAINER="${AILOCAL_LITELLM_CONTAINER:-ailocal-litellm}"

docker inspect "$CONTAINER" >/dev/null 2>&1 || {
  echo "  ✗ container '$CONTAINER' not found — cannot verify the LiteLLM version" >&2
  exit 1; }

expected="$(docker exec "$CONTAINER" printenv AILOCAL_LITELLM_VERSION 2>/dev/null || true)"
[ -n "$expected" ] || {
  echo "  ✗ AILOCAL_LITELLM_VERSION is not set in the container." >&2
  echo "    The compose file must declare the validated version." >&2
  exit 1; }

# Read the INSTALLED distribution metadata, not a self-reported attribute:
# litellm exposes no __version__, and trusting a tag is what caused this drift.
actual="$(docker exec "$CONTAINER" sh -c \
  "cat /app/.venv/lib/python*/site-packages/litellm-*.dist-info/METADATA 2>/dev/null \
   | grep -m1 '^Version:' | cut -d' ' -f2" || true)"
[ -n "$actual" ] || {
  echo "  ✗ could not read the installed LiteLLM version from '$CONTAINER'" >&2
  exit 1; }

if [ "$expected" = "$actual" ]; then
  echo "  ✓ LiteLLM $actual (matches the validated version)"
  exit 0
fi

echo "  ✗ LiteLLM VERSION DRIFT" >&2
echo "      validated: $expected" >&2
echo "      running:   $actual" >&2
echo "    The image moved, or the pin was changed without re-validating." >&2
echo "    Re-run scripts/test-all.sh and scripts/streaming-ab.py against the new" >&2
echo "    version, then update the digest, AILOCAL_LITELLM_VERSION and CLAUDE.md" >&2
echo "    together." >&2
exit 2
