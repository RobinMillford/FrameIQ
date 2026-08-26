#!/usr/bin/env bash
# Nightly PostgreSQL backup — run via cron on the VPS.
# Crontab entry:
#   0 3 * * * /opt/frameiq/scripts/backup.sh >> /var/log/frameiq-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
CONTAINER="${CONTAINER:-frameiq-db-1}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-frameiq}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/frameiq_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting backup of ${PG_DB}..."
docker exec "$CONTAINER" pg_dump -U "$PG_USER" "$PG_DB" | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Backup written: $BACKUP_FILE ($SIZE)"

# Retention: delete backups older than N days
echo "[$(date)] Pruning backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "frameiq_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete

echo "[$(date)] Done."
