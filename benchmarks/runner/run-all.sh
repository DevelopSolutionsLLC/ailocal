#!/usr/bin/env bash
# run-all.sh — every suite, every model, SERIALLY.
#
# Order matters: throughput first, because it writes the fixture calibration the
# retrieval suite needs. Each suite checkpoints independently, so an interrupt
# loses at most one run and `resume` continues.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="$ROOT/scripts/benchmark-models"
step(){ echo; echo "════ $* ════"; }
step "1/5 throughput (tok/s per context)";      "$B" run --suite throughput  "$@" || true
step "2/5 retrieval (accuracy per context)";    "$B" run --suite retrieval   "$@" || true
step "3/5 fast coding (executed tests)";        "$B" run --suite fastcode    "$@" || true
step "4/5 code understanding (CRUXEval-O)";     "$B" run --suite cruxeval    "$@" || true
step "5/5 architecture + review";               "$B" run --suite architecture "$@" || true
                                                "$B" run --suite review      "$@" || true
step "report"; "$B" report
