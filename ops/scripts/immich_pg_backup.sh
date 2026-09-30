#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/pi/nas_share/backup/immich-postgres}"
PG_CONTAINER="${PG_CONTAINER:-immich-postgres}"
PGUSER="${PGUSER:-casaos}"
PGDATABASE="${PGDATABASE:-immich}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/immich_${stamp}.sql.gz"
tmp="$out.tmp"

/usr/bin/docker exec "$PG_CONTAINER" pg_dump -U "$PGUSER" "$PGDATABASE" | gzip -1 > "$tmp"
mv -f "$tmp" "$out"

find "$BACKUP_DIR" -type f -name 'immich_*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "backup created: $out"
