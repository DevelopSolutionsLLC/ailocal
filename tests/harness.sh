# shellcheck shell=bash
# Reporting for shell suites; source it. report returns 0 clean, 1 otherwise.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
_fails=0
_passes=0
_failed_labels=""

ok() { _passes=$((_passes + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

bad() {  # bad <label> [detail]
  _fails=$((_fails + 1))
  _failed_labels="${_failed_labels}
  - $1"
  printf '  \033[31mFAIL\033[0m  %s\n' "$1"
  [ -n "${2:-}" ] && printf '        %s\n' "$2"
  return 0
}

check() { if [ "$1" = 0 ]; then ok "$2"; else bad "$2" "${3:-}"; fi; }  # check <0|1> <label> [detail]

section() { printf '\n%s\n' "$1"; }

report() {  # report [suite-name]
  printf '\n'
  if [ "$_fails" -gt 0 ]; then
    printf '%sFAILED (%s)%s\n' "${1:+$1: }" "$_fails" "$_failed_labels"
    return 1
  fi
  printf '%sall checks passed (%s)\n' "${1:+$1: }" "$_passes"
}
