# Status-line rendering shared by the shell entry points. Source it.
#
#   . "$(dirname "$0")/lib/output.sh"
#
#   banner "Installing models"   bold-blue section header      stdout
#   step   "Checking aliases"    progress section, blank line  stdout
#   info   "config written"      success/status line           stdout
#   warn   "no models resident"  advisory                      stderr
#   error  "cannot reach proxy"  failure; prints only          stderr
#   has    docker                command-existence predicate   (no output)
#
# error does NOT exit: callers decide, and several continue deliberately.
# Colour follows NO_COLOR and is dropped when stdout is not a terminal.

[ -n "${_AILOCAL_OUTPUT_SOURCED:-}" ] && return 0
_AILOCAL_OUTPUT_SOURCED=1

if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
  _O_BLUE='' _O_RESET=''
else
  _O_BLUE=$'\033[1;34m' _O_RESET=$'\033[0m'
fi

banner() { printf '%s==>%s %s\n' "$_O_BLUE" "$_O_RESET" "$*"; }
step()   { echo; echo "▶ $*"; }
info()   { echo "  ✓ $*"; }
warn()   { echo "  ⚠ $*" >&2; }
error()  { echo "  ✗ $*" >&2; }
has()    { command -v "$1" >/dev/null 2>&1; }
