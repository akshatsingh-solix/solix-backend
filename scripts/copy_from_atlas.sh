#!/usr/bin/env bash
# Copy the whole database from MongoDB Atlas into the MongoDB container on this VM.
# Run from the repo root on the VM:  ATLAS_URL='mongodb+srv://...' scripts/copy_from_atlas.sh
# Safe to run again: --drop replaces each collection with Atlas's current copy (use that for the final sync at cutover).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ATLAS_URL:?Set ATLAS_URL to the Atlas connection string (the MONGO_URL value from Render)}"
set -a; . ./.env; set +a
DB="${DB_NAME:-solix}"
LOCAL="mongodb://${MONGO_ROOT_USER}:${MONGO_ROOT_PASSWORD}@localhost:27017/?authSource=admin"
STAMP=$(date +%F-%H%M)

echo "1/3 Dumping '$DB' from Atlas ..."
docker compose exec -T mongo mongodump --uri "$ATLAS_URL" --db "$DB" --gzip --archive="/backups/atlas-$STAMP.gz"

echo "2/3 Restoring into the local MongoDB ..."
docker compose exec -T mongo mongorestore --uri "$LOCAL" --gzip --archive="/backups/atlas-$STAMP.gz" --drop

echo "3/3 Comparing document counts (Atlas vs local) ..."
CMP='db.getMongo().getDB("'"$DB"'").getCollectionNames().sort().forEach(c => print(c + " " + db.getMongo().getDB("'"$DB"'")[c].estimatedDocumentCount()))'
diff <(docker compose exec -T mongo mongosh "$ATLAS_URL" --quiet --eval "$CMP") \
     <(docker compose exec -T mongo mongosh "$LOCAL" --quiet --eval "$CMP") \
  && echo "OK: every collection has the same count. Dump kept at backups/atlas-$STAMP.gz" \
  || { echo "Counts differ (lines above). Normal only if the site took writes during the copy; run again after stopping Render."; exit 1; }
