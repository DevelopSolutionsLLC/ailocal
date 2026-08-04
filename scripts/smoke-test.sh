#!/usr/bin/env bash
# smoke-test.sh — bounded runtime verification of a running stack.
#
#   ./scripts/smoke-test.sh [alias]                (or: ailocal smoke [alias])
#
# Checks containers, proxy health, served aliases, advertised geometry, Ollama
# and its model inventory, one bounded model response, and search. Every call
# carries a timeout.
#
# Exit 0 clean, 1 if any required runtime check failed. Search is optional
# infrastructure: its absence degrades the report without failing it.
#
# Deterministic configuration checking is `ailocal validate`.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT_DIR/scripts/lib/checks/run.py" smoke "$@"
