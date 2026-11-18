#!/usr/bin/env bash
# Nightly MongoDB backup on the VM. Keeps 14 days locally; optionally copies off the VM with rclone.
# Install:  crontab -e   ->   15 2 * * * /home/ubuntu/solix-backend/scripts/backup.sh >> /home/ubuntu/backup.log 2>&1
# Off-VM copy: set BACKUP_REMOTE in .env (an rclone remote, e.g. ocibucket:solix-backups). A backup that only lives
# on the same disk as the database does not protect you from losing the VM.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
FILE="solix-$(date +%F).gz"
docker compose exec -T mongo mongodump \
  --uri "mongodb://${MONGO_ROOT_USER}:${MONGO_ROOT_PASSWORD}@localhost:27017/?authSource=admin" \
  --db "${DB_NAME:-solix}" --gzip --archive="/backups/$FILE"
find backups -name 'solix-*.gz' -mtime +14 -delete
if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "backups/$FILE" "$BACKUP_REMOTE" && rclone delete --min-age 30d "$BACKUP_REMOTE"
fi
echo "$(date -Is) backup ok: backups/$FILE"
