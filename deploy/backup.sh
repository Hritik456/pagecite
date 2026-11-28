#!/usr/bin/env bash
# Daily MySQL backup to S3 (cron, /etc/cron.d/pagecite-backup). Installed by deploy.sh at each deploy.
#   s3://<S3_BUCKET>/backups/pagecite-<UTC timestamp>.sql.gz, SSE-S3. Kept 7 days by the bucket's
#   lifecycle rule, so the instance role needs only PutObject on backups/* (no List, no Delete).
# The root password never leaves the mysql container (MYSQL_PWD from its own environment).
# MySQL is the truth; the Redis index is rebuilt from it after a restore (flask reindex --all).
set -euo pipefail
DIR=/opt/pagecite
cd "$DIR"
BUCKET="$(grep -E '^S3_BUCKET=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
REGION="$(grep -E '^AWS_REGION=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
[[ -n "$BUCKET" ]] || { echo "backup: S3_BUCKET is not set in .env" >&2; exit 2; }
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp --suffix=.sql.gz /var/tmp/pagecite-backup.XXXXXX)"
trap 'rm -f "$TMP"' EXIT

./pc exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -uroot --single-transaction --quick \
  --routines --events --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' | gzip -9 > "$TMP"

bytes="$(stat -c %s "$TMP")"
# A real dump has the schema of every table (> 10 KB even with no rows); less means it failed silently.
(( bytes > 10240 )) || { echo "backup: dump is only $bytes bytes, refusing to upload it" >&2; exit 3; }
gzip -t "$TMP"

aws s3 cp "$TMP" "s3://$BUCKET/backups/pagecite-$TS.sql.gz" --sse AES256 --region "${REGION:-ap-south-1}" --only-show-errors
echo "backup $(date -u +%FT%TZ) ok key=backups/pagecite-$TS.sql.gz bytes=$bytes"
