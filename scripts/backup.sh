#!/usr/bin/env bash
# backup.sh — back up configs, .env, and Postgres database
# Usage: ./scripts/backup.sh
#
# What is backed up:
#   config/          — all service configuration files
#   docker-compose.yml
#   .env             — secrets (archive is stored locally, not committed)
#   postgres dump    — full logical dump via pg_dump (if postgres is running)
#
# What is NOT backed up:
#   Ollama models    — large; re-pull with install-models.sh
#   Redis cache      — ephemeral; rebuilds automatically
#   Open WebUI data  — conversation history in Docker volume (not yet included)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/backups"
mkdir -p "$BACKUP_DIR"

# ── Helpers ────────────────────────────────────────────────────────────────

has()   { command -v "$1" >/dev/null 2>&1; }
info()  { echo "  ✓ $*"; }
warn()  { echo "  ⚠ $*"; }
error() { echo "  ✗ $*" >&2; }
step()  { echo; echo "▶ $*"; }

# ── Staging area ───────────────────────────────────────────────────────────

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGING="$BACKUP_DIR/.staging-$TIMESTAMP"
mkdir -p "$STAGING"
# Staging dir holds secrets (.env, postgres dump) — lock it down immediately.
chmod 700 "$STAGING"

# Clean up the staging directory on any exit (success or failure)
trap 'rm -rf "$STAGING"' EXIT

step "Creating backup ($TIMESTAMP)"

# ── Config and env files ───────────────────────────────────────────────────

cp -r "$ROOT_DIR/config"             "$STAGING/config"
cp    "$ROOT_DIR/docker-compose.yml" "$STAGING/docker-compose.yml"
if [ -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env" "$STAGING/.env"
  chmod 600 "$STAGING/.env"
fi

# ── Postgres logical dump ──────────────────────────────────────────────────

if docker ps --format "{{.Names}}" | grep -q "^ailocal_postgres$"; then
  echo "  Dumping Postgres..."
  # Read DB credentials directly from .env (avoid sourcing the whole file into the shell)
  PG_USER=$(grep '^POSTGRES_USER=' "$ROOT_DIR/.env" | cut -d= -f2 || echo "ailocal")
  PG_DB=$(grep '^POSTGRES_DB='   "$ROOT_DIR/.env" | cut -d= -f2 || echo "ailocal")
  docker exec ailocal_postgres \
    pg_dump -U "${PG_USER:-ailocal}" "${PG_DB:-ailocal}" \
    > "$STAGING/postgres.sql"
  chmod 600 "$STAGING/postgres.sql"
  info "Postgres dump written ($(wc -c < "$STAGING/postgres.sql" | tr -d ' ') bytes)"
else
  warn "ailocal_postgres is not running — skipping database dump."
  echo "  Start services and re-run backup to include database data."
fi

# ── Archive and clean up ───────────────────────────────────────────────────

ARCHIVE="$BACKUP_DIR/ailocal-backup-$TIMESTAMP.tar.gz"
tar -czf "$ARCHIVE" -C "$BACKUP_DIR" ".staging-$TIMESTAMP"
# Archive contains .env and postgres dump — owner-read/write only.
chmod 600 "$ARCHIVE"
# trap will remove the staging dir on exit

info "Archive: $ARCHIVE ($(du -sh "$ARCHIVE" | cut -f1))"

# Keep only the 10 most recent backups
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/ailocal-backup-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt 10 ]; then
  echo "  Pruning old backups (keeping 10 most recent)..."
  ls -1t "$BACKUP_DIR"/ailocal-backup-*.tar.gz | tail -n +11 | xargs rm -f
fi

step "Backup complete."
