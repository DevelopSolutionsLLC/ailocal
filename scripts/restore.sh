#!/usr/bin/env bash
# restore.sh — restore config and database from a backup archive
# Usage: ./scripts/restore.sh [path/to/backup.tar.gz]
#        (defaults to the most recent backup in ./backups/)
#
# WARNING: This overwrites current config files and drops/restores the database.
# Services will be stopped during restore and restarted afterward.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/backups"

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Locate backup archive ──────────────────────────────────────────────────

if [ -n "${1:-}" ]; then
  ARCHIVE="$1"
else
  ARCHIVE=$(ls -1t "$BACKUP_DIR"/ailocal-backup-*.tar.gz 2>/dev/null | head -n1 || true)
fi

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  error "No backup archive found."
  echo "  Usage: $0 [path/to/backup.tar.gz]" >&2
  echo "  Create a backup first: ./scripts/backup.sh" >&2
  exit 1
fi

step "Restore from: $ARCHIVE"
echo ""
echo "  This will:"
echo "    1. Stop all running services"
echo "    2. Overwrite config/ and docker-compose.yml"
echo "    3. Restore the Postgres database"
echo "    4. Restart services"
echo ""
read -r -p "  Proceed? [y/N]: " confirm
[[ "${confirm:-}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

# ── Stop services ──────────────────────────────────────────────────────────

step "Stopping services"
docker compose -f "$ROOT_DIR/docker-compose.yml" down --remove-orphans 2>/dev/null || true

# ── Extract archive ────────────────────────────────────────────────────────

step "Extracting archive"
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
tar -xzf "$ARCHIVE" -C "$STAGING"

# The archive contains a single staging directory
STAGING_INNER=$(ls -1 "$STAGING" | head -n1)
RESTORE_SRC="$STAGING/$STAGING_INNER"

# ── Restore config files ───────────────────────────────────────────────────

step "Restoring config files"
[ -d "$RESTORE_SRC/config" ]             && cp -r "$RESTORE_SRC/config"             "$ROOT_DIR/"
[ -f "$RESTORE_SRC/docker-compose.yml" ] && cp    "$RESTORE_SRC/docker-compose.yml" "$ROOT_DIR/"
if [ -f "$RESTORE_SRC/.env" ]; then
  cp "$RESTORE_SRC/.env" "$ROOT_DIR/"
  chmod 600 "$ROOT_DIR/.env"
  info ".env restored"
fi

# ── Restore Postgres ───────────────────────────────────────────────────────

if [ -f "$RESTORE_SRC/postgres.sql" ]; then
  step "Restoring Postgres database"
  # Start just postgres to restore into it
  docker compose -f "$ROOT_DIR/docker-compose.yml" up -d postgres

  # shellcheck source=../.env
  source "$ROOT_DIR/.env" 2>/dev/null || true

  echo "  Waiting for Postgres to be ready..."
  attempts=0
  max_attempts=30
  until docker exec ailocal_postgres pg_isready -U "${POSTGRES_USER:-ailocal}" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ $attempts -ge $max_attempts ]; then
      error "Postgres did not become ready after ${max_attempts}s — aborting."
      exit 1
    fi
    sleep 1
  done
  info "Postgres ready"

  docker exec -i ailocal_postgres \
    psql -U "${POSTGRES_USER:-ailocal}" "${POSTGRES_DB:-ailocal}" \
    < "$RESTORE_SRC/postgres.sql"
  info "Postgres restored"
else
  warn "No postgres.sql in archive — skipping database restore."
fi

# trap will clean up $STAGING on exit

# ── Restart services ───────────────────────────────────────────────────────

step "Starting services"
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d --remove-orphans

step "Restore complete."
echo "  Run ./scripts/healthcheck.sh to verify."
