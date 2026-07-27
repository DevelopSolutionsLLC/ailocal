#!/usr/bin/env bash
# cleanup-installation.sh — act on scripts/audit-installation.sh findings.
#
#   --dry-run   show exactly what would happen. THE DEFAULT.
#   --apply     do it, after backing everything up
#
# Nothing is removed without a backup, and nothing at all happens without
# --apply. Running with no arguments is a dry run, not an apply: a cleanup tool
# whose default is destructive is a trap.
#
# ── what this will NOT touch, deliberately ──
#   ~/.claude, ~/.codex            the CLOUD roots. ailocal keeps them separate on
#                                  purpose; removing them breaks the user's
#                                  non-local setup, which is not this tool's business.
#   VS Code SecretStorage          Keychain-backed. Not ours to modify, and losing
#                                  the key means re-entering it by hand.
#   anything git tracks            git already has it; deleting tracked files here
#                                  would just produce a confusing dirty tree.
#   other projects' containers     e.g. sola-db-1, cadence-qdrant. Only
#                                  ailocal-prefixed resources are in scope.
#
# ── files the audit flags that a human should decide on ──
# Untracked working files (session notes, ad-hoc JSON) are flagged STALE because
# nothing reads them, but they may be someone's notes. They are listed under
# "needs your decision" and are NOT removed even with --apply unless
# --include-notes is also given.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE=dry
INCLUDE_NOTES=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE=dry ;;
    --apply)   MODE=apply ;;
    --include-notes) INCLUDE_NOTES=1 ;;
    *) echo "usage: $0 [--dry-run|--apply] [--include-notes]"; exit 1 ;;
  esac
done

FINDINGS="${AUDIT_FINDINGS:-/tmp/ailocal-audit-findings.txt}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$ROOT/backups/cleanup-$STAMP"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_0=$'\033[0m'
did=0; skipped=0

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
act()  { printf '  %s%s%s %s\n' "$C_OK" "$([ "$MODE" = apply ] && echo "DONE   " || echo "WOULD  ")" "$C_0" "$*"; }
hold() { printf '  %s HOLD  %s %s\n' "$C_WARN" "$C_0" "$*"; }

# The audit is the single source of findings. Regenerating them here would mean
# two implementations that can disagree about what is stale.
if [ ! -s "$FINDINGS" ]; then
  info "no findings file — running the audit first"
  ./scripts/audit-installation.sh >/dev/null 2>&1 || true
fi
if [ ! -s "$FINDINGS" ]; then
  echo "${C_OK}Nothing to clean.${C_0}"
  exit 0
fi

echo "══════════════════════════════════════════════════════════════════════"
echo " INSTALLATION CLEANUP — mode: $MODE"
[ "$MODE" = dry ] && echo " ${C_DIM}nothing will be modified${C_0}"
echo "══════════════════════════════════════════════════════════════════════"

backup_file() { # $1=path
  [ "$MODE" = apply ] || return 0
  local src="$1" rel
  rel="${src#$ROOT/}"
  mkdir -p "$BACKUP/$(dirname "$rel")"
  cp -a "$src" "$BACKUP/$rel" 2>/dev/null || return 1
}

is_note() { # untracked ad-hoc files a human may want to keep
  case "$(basename "$1")" in
    *.md|*.json|*.txt|*.log) return 0 ;;
    *) return 1 ;;
  esac
}

info "processing findings"
while IFS=$'\t' read -r class item location action; do
  [ -n "${class:-}" ] || continue
  case "$class" in
    STALE)
      if [ ! -e "$location" ]; then
        printf '  %s—%s already gone: %s\n' "$C_DIM" "$C_0" "$item"
        continue
      fi
      # Never remove something git is tracking.
      if git ls-files --error-unmatch "$location" >/dev/null 2>&1; then
        hold "git tracks this; not touching it: $item"
        skipped=$((skipped + 1)); continue
      fi
      if is_note "$location" && [ -z "$INCLUDE_NOTES" ]; then
        hold "looks like notes/working output — needs your decision: $item"
        printf '        %s\n' "$location"
        printf '        re-run with --include-notes to move it to backups/\n'
        skipped=$((skipped + 1)); continue
      fi
      if backup_file "$location"; then
        act "backed up + removed: $item"
        [ "$MODE" = apply ] && rm -rf "$location"
        did=$((did + 1))
      else
        hold "could not back up, so NOT removed: $item"
        skipped=$((skipped + 1))
      fi
      ;;
    DUPLICATE)
      # Duplicates are config-shaped; picking a winner automatically is exactly
      # the kind of guess that breaks a working setup.
      hold "duplicate needs a human choice: $item"
      printf '        %s\n        %s\n' "$location" "$action"
      skipped=$((skipped + 1))
      ;;
    MISSING)
      hold "missing — install, do not clean: $item"
      printf '        %s\n' "$action"
      skipped=$((skipped + 1))
      ;;
    *)
      hold "unclassified finding, left alone: $class $item"
      skipped=$((skipped + 1))
      ;;
  esac
done < "$FINDINGS"

# ── docker resources scoped to ailocal only ─────────────────────────────────
info "docker (ailocal-scoped only)"
DEAD=$(docker ps -a --filter "name=ailocal-" --filter "status=exited" \
        --format '{{.Names}}' 2>/dev/null || true)
if [ -n "$DEAD" ]; then
  for c in $DEAD; do
    act "remove exited container $c"
    [ "$MODE" = apply ] && docker rm "$c" >/dev/null 2>&1
    did=$((did + 1))
  done
else
  printf '  %s—%s no exited ailocal containers\n' "$C_DIM" "$C_0"
fi
ORPHAN=$(docker volume ls -qf dangling=true 2>/dev/null | grep -i ailocal || true)
if [ -n "$ORPHAN" ]; then
  for v in $ORPHAN; do
    hold "dangling volume $v — review before removing (may hold data)"
    skipped=$((skipped + 1))
  done
else
  printf '  %s—%s no dangling ailocal volumes\n' "$C_DIM" "$C_0"
fi

echo
echo "══════════════════════════════════════════════════════════════════════"
if [ "$MODE" = dry ]; then
  echo " DRY RUN. $did action(s) would be taken, $skipped held for your decision."
  echo " Re-run with --apply to act. Everything removed is copied to backups/ first."
else
  echo " $did action(s) taken, $skipped held."
  [ "$did" -gt 0 ] && echo " Backup: $BACKUP"
  echo " Re-run ./scripts/audit-installation.sh to confirm."
fi
