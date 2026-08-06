# Bounded client execution for the end-to-end validators. Source it.
#
# Owns process lifecycle only -- budget, process group, capture, termination
# escalation, stray-process sweep. Protocol assertions stay with each client:
# what a completed Claude turn looks like is not the same question as what a
# Codex stream failing to terminate means.
#
#   . "$(dirname "$0")/e2e.sh"
#   e2e_run 180 out.log claude-local -p "hello"
#   case $? in 0) ... ;; 124) ... ;; esac
#
# Returns the command's status, or 124 on timeout.

# e2e_run <budget-seconds> <logfile> <command...>
e2e_run() {
  local budget="$1" log="$2"; shift 2
  local rc=0

  if command -v timeout >/dev/null 2>&1; then
    timeout -k 5 "$budget" "$@" > "$log" 2>&1
    rc=$?
  else
    # No GNU timeout: run in its own process group so the whole tree can be
    # signalled, not just the leader.
    set -m
    "$@" > "$log" 2>&1 &
    local pid=$! waited=0
    set +m
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$budget" ]; do
      sleep 2; waited=$((waited + 2))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      sleep 3
      kill -0 "$pid" 2>/dev/null && { kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null; }
      rc=124
    else
      wait "$pid" 2>/dev/null; rc=$?
    fi
  fi
  return "$rc"
}

# e2e_sweep <pattern>...  — nothing of ours may outlive its budget.
e2e_sweep() {
  local pat
  for pat in "$@"; do
    pkill -f "$pat" 2>/dev/null || true
  done
  return 0
}

# e2e_strays <pattern> — count survivors; a validator that leaks is not bounded.
e2e_strays() {
  pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '
}

# e2e_workdir — a temporary directory registered for removal on exit.
e2e_workdir() {
  local d; d="$(mktemp -d)"
  _E2E_DIRS="${_E2E_DIRS:-} $d"
  printf '%s' "$d"
}
