# Runbook (production)

One EC2 instance (`pagecite`, t3.small, ap-south-1) runs everything with `docker-compose.prod.yml`. Everything on it lives in `/opt/pagecite` (root only):
- `.env`: yours, chmod 600
- `deployed.env` / `previous.env`: `IMAGE_TAG=<sha>`
- the compose file and the helper scripts of the deployed commit
- `pc`, the compose wrapper

Open a shell with EC2 → `pagecite` → Connect → **Session Manager**. Port 22 stays closed.

```sh
sudo /opt/pagecite/pc ps                  # services and health
sudo /opt/pagecite/pc logs --tail 100 api # or worker, relay, caddy, mysql, redis
cat /opt/pagecite/deployed.env            # what is running
```

## Deploy

**Automatic.** Every push to `main` that passes CI runs [.github/workflows/deploy.yml](../.github/workflows/deploy.yml):
1. It builds `pagecite-{api,worker,caddy,migrate}:<sha>` and pushes them to GHCR (public).
2. SSM runs `deploy/deploy.sh <sha>`, fetched **at that commit**. It:
   - pulls the images
   - starts mysql + redis
   - runs **Flyway migrate**
   - only then restarts api, relay, worker and caddy
   - checks that `/health` reports the sha
3. It smoke-tests over HTTPS: `/api/health` has the new sha, the home page loads, and the demo key gets 403 on upload.

**Manual redeploy** of the current or any commit: Actions → **Deploy** → Run workflow → `sha` = a full 40-character SHA on `main`.

## Roll back

Actions → **Deploy** → Run workflow → `sha` = the last good commit (`cat /opt/pagecite/previous.env` on the server shows the one before the current deploy). Its images are already in GHCR, so nothing is rebuilt.

**Flyway migrations only go forward.** A rollback runs the *old* code against the *newer* schema, and Flyway won't undo a migration. That is safe only because every migration is additive (new tables and columns; for example V5 only adds `api_keys`). The rule:
- never drop, rename or change the type of a column in the same release as the code that stops using it
- ship the code change first; remove the column in a later release

If a migration itself was the problem, write a new forward migration that fixes it. Restoring a backup is the last resort.

## Rotate the OpenAI key

1. OpenAI dashboard → project `pagecite-prod` → create a new key.
2. `sudo nano /opt/pagecite/.env`: replace `OPENAI_API_KEY`.
3. `sudo /opt/pagecite/pc up -d api worker`. Compose recreates the containers whose environment changed.
4. Ask one demo question, then delete the old key in the dashboard.

## Rotate the demo (ask-only) key

```sh
sudo /opt/pagecite/pc exec api flask --app apps.api revoke-ask-keys --tenant 1
sudo /opt/pagecite/pc exec api flask --app apps.api create-ask-key --tenant 1   # -> ask_key=pca_...
sudo nano /opt/pagecite/.env                                                     # DEMO_ASK_KEY=pca_...
sudo /opt/pagecite/pc up -d caddy
```

Open browsers pick up the new key on their next page load.

## Demo limits

These are set in `.env`:
- `ASK_RATE_PER_MINUTE=10` and `ASK_RATE_PER_DAY=20` per client IP (IPv6 per /64)
- `DAILY_SPEND_CAP_USD=0.50`, global
- `ASK_MAX_CHARS=500`

After changing them, run `sudo /opt/pagecite/pc up -d api`. They live in Redis DB 2:

```sh
sudo /opt/pagecite/pc exec redis redis-cli -n 2 GET "pagecite:spend:$(date -u +%Y%m%d)"   # micro-USD spent today
sudo /opt/pagecite/pc exec redis redis-cli -n 2 --scan --pattern 'pagecite:rl:*' | head
```

To reset the limits, delete their keys. **Never run `FLUSHDB`/`FLUSHALL` on any Redis database:** in Redis 8, a flush of *any* DB drops every search index, including the chunk index in DB 0.

```sh
sudo /opt/pagecite/pc exec redis sh -c "redis-cli -n 2 --scan --pattern 'pagecite:*' | xargs -r redis-cli -n 2 unlink"
```

## Stop and start the instance to save credits

- **Stop:** EC2 → `pagecite` → Instance state → **Stop**. While stopped you pay only for the 20 GB disk (about $1.8/month) and the Elastic IP (about $3.6/month); the instance itself costs nothing.
- **Start:** Instance state → **Start**. The Elastic IP and the DuckDNS record don't change.
  - Docker starts at boot, every container is `restart: unless-stopped`, and swap is in fstab, so the site comes back in about 2 minutes with nothing to do.
  - Caddy's certificate is in the `caddy-data` volume, so it isn't re-issued.

## Backups and restore

`/opt/pagecite/backup.sh` runs daily at 02:30 IST (cron). It writes `s3://<BUCKET>/backups/pagecite-<UTC>.sql.gz` (SSE-S3). The lifecycle rule deletes copies after 7 days. Its log is `/var/log/pagecite-backup.log`.

```sh
sudo /opt/pagecite/backup.sh                          # take one now
aws s3 ls s3://<BUCKET>/backups/                      # (instance role; ListBucket)
```

**Restore** (this replaces the database):

```sh
cd /opt/pagecite
sudo aws s3 cp s3://<BUCKET>/backups/pagecite-<TS>.sql.gz /var/tmp/restore.sql.gz
sudo ./pc stop api relay worker
gunzip -c /var/tmp/restore.sql.gz | sudo ./pc exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot "$MYSQL_DATABASE"'
sudo ./pc start api relay worker
sudo ./pc exec api flask --app apps.api reindex --all   # the Redis index is derived from MySQL: rebuild it
sudo rm /var/tmp/restore.sql.gz
```

The S3 objects (`tenant/...`) aren't in the dump. They stay in the bucket, and the restored rows point at them.

## Prod ingest (the demo corpus)

See [deploy/aws-setup.md](deploy/aws-setup.md), steps 12–13.
- `scripts/prod_ingest.py` uploads the 60 PDFs through the public API with the demo tenant's full key, from `PAGECITE_ADMIN_KEY` in your shell.
- `sudo /opt/pagecite/sample-memory.sh` logs `free -m` and `docker stats` every 5 s to `/var/log/pagecite-mem.log`.
- `sudo /opt/pagecite/oom-check.sh` reports OOM kills and restarts per container, plus kernel OOM lines.

## Disk and memory

```sh
df -h /                                   # 20 GB; images of the last week are kept for rollback
sudo docker system df
free -m && swapon --show                  # 2 GiB swap
sudo /opt/pagecite/oom-check.sh
```

Every container's logs are capped at json-file 10 MB × 3. If the disk fills anyway: `sudo docker image prune -af --filter until=72h`.

## The site is down

1. `curl -sS https://pagecite.duckdns.org/api/health` from your laptop.
2. In Session Manager:
   - `sudo /opt/pagecite/pc ps`: is anything unhealthy or restarting?
   - `sudo /opt/pagecite/pc logs --tail 200 caddy api`
3. **Certificate problems:** check the Caddy logs for `acme`. Port 80 must be open to the world for HTTP-01.
4. **S3 errors (`STORAGE_UNAVAILABLE`):** check that the instance role is attached and that the metadata hop limit is **2**.
5. **Index errors (`INDEX_UNAVAILABLE`):** `sudo /opt/pagecite/pc exec api flask --app apps.api reindex --all`.
