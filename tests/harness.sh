# Shared mechanics for the shell test suites. Source it; do not execute it.
#
# Six suites carried their own reporting in two dialects -- check(cond,label)
# and an ok()/bad() pair -- with three summary formats between them. This owns
# reporting, exit status, repository root and temporary state. Suites keep their
# own fixtures and assertions.
#
#   . "$(dirname "$0")/harness.sh"
#   check 0 "a passing assertion"
#   ok "an assertion that already decided"
#   bad "a failing assertion"
#   report          # prints the summary; returns 0 clean, 1 otherwise

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_fails=0
_passes=0
_skips=0
_failed_labels=""

ok() {   # ok <label>
  _passes=$((_passes + 1))
  printf '  \033[32mPASS\033[0m  %s\n' "$1"
}

bad() {  # bad <label> [detail]
  _fails=$((_fails + 1))
  _failed_labels="${_failed_labels}
  - $1"
  printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  [ -n "${2:-}" ] && printf '        %s\n' "$2"
  return 0
}

check() {  # check <0|1> <label> [detail]
  if [ "$1" = 0 ]; then ok "$2"; else bad "$2" "${3:-}"; fi
}

skip() {   # skip <label> [reason]
  _skips=$((_skips + 1))
  printf '  \033[33mSKIP\033[0m  %s%s\n' "$1" "${2:+ — $2}"
}

section() { printf '\n%s\n' "$1"; }

# Status-line rendering. `error` does NOT exit: callers decide, and several
# continue deliberately. Colour follows NO_COLOR and is dropped when stdout is
# not a terminal.
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then _O_BLUE='' _O_RESET=''
else _O_BLUE=$'\033[1;34m' _O_RESET=$'\033[0m'; fi
banner() { printf '%s==>%s %s\n' "$_O_BLUE" "$_O_RESET" "$*"; }
step()   { echo; echo "▶ $*"; }
info()   { echo "  ✓ $*"; }
warn()   { echo "  ⚠ $*" >&2; }
error()  { echo "  ✗ $*" >&2; }
has()    { command -v "$1" >/dev/null 2>&1; }

report() {  # report [suite-name]
  printf '\n'
  if [ "$_fails" -gt 0 ]; then
    printf '%sFAILED (%s)%s\n' "${1:+$1: }" "$_fails" "$_failed_labels"
    return 1
  fi
  printf '%sall checks passed (%s%s)\n' "${1:+$1: }" "$_passes" \
    "$([ "$_skips" -gt 0 ] && printf ', %s skipped' "$_skips")"
  return 0
}

# Temporary directories removed on exit, including after a failure. Suites that
# need their own trap should call _harness_cleanup from it.
_harness_tmp=""
temp_dir() {  # temp_dir -> prints the path
  local d; d="$(mktemp -d)"
  _harness_tmp="$_harness_tmp $d"
  printf '%s' "$d"
}
_harness_cleanup() { [ -n "$_harness_tmp" ] && rm -rf $_harness_tmp; return 0; }
trap _harness_cleanup EXIT
