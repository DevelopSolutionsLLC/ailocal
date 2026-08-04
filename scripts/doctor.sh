#!/usr/bin/env bash
# doctor.sh — health diagnosis with remediation.
#
#   ./scripts/doctor.sh                            (or: ailocal doctor)
#
# Renders the same configuration and runtime checks as `ailocal validate` and
# `ailocal smoke`, plus host-machine guidance those two do not report, and
# attaches a fix to every finding.
#
# Exit codes:
#   0  healthy (advisory warnings may still be printed)
#   1  the active tier or profile could not be resolved, so diagnosis is
#      REFUSED rather than reported against an assumed configuration
#   2  degraded: one or more checks failed
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT_DIR/scripts/lib/checks/run.py" doctor "$@"
