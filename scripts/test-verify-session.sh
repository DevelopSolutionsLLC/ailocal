#!/usr/bin/env bash
# test-verify-session.sh — end-to-end tests for the Phase E classification
# pipeline. Builds real git repos and real ledgers, then asserts the verdict AND
# the exit code, because the exit code is what any automation will act on.
#
# The case that matters most is UNVERIFIED->3: a verification layer whose
# "could not check" is scriptable as success is worse than no layer.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"; LED="$TMP/ledgers"; mkdir -p "$LED"
fails=0

ledger() { # $1=file $2=json
  printf '%s' "$2" > "$LED/$1.json"
}
mkrepo() { # $1=name $2=dirty?
  local d="$TMP/$1"; mkdir -p "$d"; ( cd "$d" && git init -q \
    && printf 'v=1\n' > f.py && git add -A \
    && git -c user.email=t@t -c user.name=t commit -qm init )
  [ "${2:-}" = dirty ] && printf 'v=2\n' > "$d/f.py"
  echo "$d"
}
expect() { # $1=label $2=expected-verdict $3=expected-exit $4..=args
  local label="$1" want="$2" wantrc="$3"; shift 3
  local out rc
  out="$(python3 scripts/verify-session.py "$@" 2>&1)"; rc=$?
  local got
  got="$(printf '%s' "$out" | sed -n 's/^CLASSIFICATION  //p')"
  if [ "$got" = "$want" ] && [ "$rc" = "$wantrc" ]; then
    echo "  PASS  $label -> $got (exit $rc)"
  else
    echo "  FAIL  $label -> got '$got' exit $rc, wanted '$want' exit $wantrc"
    fails=$((fails+1))
  fi
}

echo
echo "PHASE E CLASSIFICATION"

ledger edit_ok '{"session":"a","model":"m","requested_change":"fix it","tool_calls_total":2,"tool_calls_by_name":{"Read":1,"Edit":1},"tool_call_sequence":["Read","Edit"],"tool_results_total":2,"tool_results_errored":0,"tool_results_unknown_status":0}'
DIRTY="$(mkrepo dirty dirty)"
expect "edit + tree changed + test passes" VERIFIED 0 \
  --repo "$DIRTY" --ledger-dir "$LED" --test "true"
expect "edit + tree changed, no test" PARTIALLY_VERIFIED 0 \
  --repo "$DIRTY" --ledger-dir "$LED"
expect "edit + tree changed + test fails" PARTIALLY_VERIFIED 0 \
  --repo "$DIRTY" --ledger-dir "$LED" --test "false"

CLEAN="$(mkrepo clean)"
expect "edit ran but tree is clean" SUSPICIOUS 2 \
  --repo "$CLEAN" --ledger-dir "$LED"

# The distinction that keeps SUSPICIOUS meaningful.
ledger edit_ok '{"session":"a","model":"m","requested_change":"look","tool_calls_total":2,"tool_calls_by_name":{"Read":1,"Bash":1},"tool_call_sequence":["Read","Bash"],"tool_results_total":2,"tool_results_errored":0,"tool_results_unknown_status":0}'
expect "only AMBIGUOUS mutators + clean tree is not suspicious" UNVERIFIED 3 \
  --repo "$CLEAN" --ledger-dir "$LED"

ledger edit_ok '{"session":"a","model":"m","requested_change":"x","tool_calls_total":0,"tool_calls_by_name":{},"tool_call_sequence":[],"tool_results_total":0,"tool_results_errored":0,"tool_results_unknown_status":0}'
expect "no tool calls at all" UNVERIFIED 3 --repo "$DIRTY" --ledger-dir "$LED"

ledger edit_ok '{"session":"a","model":"m","requested_change":"x","tool_calls_total":2,"tool_calls_by_name":{"Read":1,"Edit":1},"tool_call_sequence":["Read","Edit"],"tool_results_total":2,"tool_results_errored":1,"tool_results_unknown_status":0}'
expect "errored tool result" PARTIALLY_VERIFIED 0 --repo "$DIRTY" --ledger-dir "$LED"

ledger edit_ok '{"session":"a","model":"m","requested_change":"x","tool_calls_total":2,"tool_calls_by_name":{"Read":1,"exec_command":1},"tool_call_sequence":["Read","exec_command"],"tool_results_total":2,"tool_results_errored":0,"tool_results_unknown_status":2}'
expect "unknown-status results + clean tree" UNVERIFIED 3 --repo "$CLEAN" --ledger-dir "$LED"

expect "not a git repository" UNVERIFIED 3 --repo "$TMP" --ledger-dir "$LED"

out="$(python3 scripts/verify-session.py --repo "$DIRTY" --ledger-dir "$TMP/none" 2>&1)"; rc=$?
if [ "$rc" = 1 ] && printf '%s' "$out" | grep -q "not.*evidence"; then
  echo "  PASS  a missing ledger exits 1 and says it is not evidence of nothing"
else
  echo "  FAIL  missing ledger: exit $rc"; fails=$((fails+1))
fi

rm -rf "$TMP"
echo
if [ "$fails" -ne 0 ]; then echo "VERIFY SESSION TESTS: $fails FAILED"; exit 1; fi
echo "VERIFY SESSION TESTS: OK"
