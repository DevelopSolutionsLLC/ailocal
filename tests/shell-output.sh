#!/usr/bin/env bash
# Contract of the shared shell status helpers (lib/output.sh).
#
# Seventeen scripts render status through these, so a change here is a change
# everywhere. The stream each helper writes to is part of the contract: callers
# redirect stdout while expecting warnings and errors to remain visible.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/harness.sh"
OUT="$ROOT_DIR/lib/output.sh"

echo "SHELL OUTPUT HELPERS"
check $([ -f "$OUT" ] && echo 0 || echo 1) "lib/output.sh exists"

# Sourcing must be inert and repeatable under every strict mode in use.
bash -c "set -euo pipefail; . '$OUT'; . '$OUT'; info x" >/dev/null 2>&1
check $? "sourcing twice under set -euo pipefail is harmless"
out=$(bash -c "set -u; . '$OUT'" 2>&1)
check $([ -z "$out" ] && echo 0 || echo 1) "sourcing emits nothing" "$out"

# Stream discipline.
so=$(bash -c ". '$OUT'; info a; step b; banner c" 2>/dev/null)
se=$(bash -c ". '$OUT'; warn d; error e" 2>&1 1>/dev/null)
check $(grep -q '✓ a' <<<"$so" && echo 0 || echo 1) "info writes '  ✓ msg' to stdout"
check $(grep -q '▶ b' <<<"$so" && echo 0 || echo 1) "step writes '▶ msg' to stdout"
check $(grep -q '==> c' <<<"$so" && echo 0 || echo 1) "banner writes '==> msg' to stdout"
check $(grep -q '⚠ d' <<<"$se" && echo 0 || echo 1) "warn writes to stderr"
check $(grep -q '✗ e' <<<"$se" && echo 0 || echo 1) "error writes to stderr"
check $([ -z "$(bash -c ". '$OUT'; warn x; error y" 2>/dev/null)" ] && echo 0 || echo 1) \
  "warn and error put nothing on stdout"

# error prints; it must not exit, because callers continue deliberately.
bash -c ". '$OUT'; error boom; exit 0" >/dev/null 2>&1
check $? "error does not exit the caller"

# Colour: absent when piped or when NO_COLOR is set.
piped=$(bash -c ". '$OUT'; banner x" 2>/dev/null | cat)
check $(grep -qv $'\033' <<<"$piped" && echo 0 || echo 1) "no ANSI when stdout is not a TTY"
nc=$(NO_COLOR=1 bash -c ". '$OUT'; banner x" 2>/dev/null)
check $(grep -qv $'\033' <<<"$nc" && echo 0 || echo 1) "NO_COLOR suppresses colour"

# has() is a predicate, not a printer.
bash -c ". '$OUT'; has sh" >/dev/null 2>&1;              check $? "has succeeds for a real command"
bash -c ". '$OUT'; has definitely-not-real" >/dev/null 2>&1
check $([ $? -ne 0 ] && echo 0 || echo 1) "has fails for a missing command"

# One owner: only the documented exceptions may define these locally.
strays=$(git -C "$ROOT_DIR" grep -lE '^\s*(info|step|banner|error|has)\(\)' -- '*.sh' \
         | grep -v 'lib/output.sh' || true)
check $([ -z "$strays" ] && echo 0 || echo 1) \
  "no script redefines a shared helper" "${strays:-}"

report || exit 1
