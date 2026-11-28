#!/usr/bin/env bash
# Deploy one commit on the server. Run as root by the deploy workflow through SSM Run Command:
#
#   curl -fsSL https://raw.githubusercontent.com/Hritik456/pagecite/<sha>/deploy/deploy.sh -o /tmp/... \
#     && bash /tmp/... <sha>
#
# This script is fetched at the SHA it deploys, and it fetches everything else at that SHA too
# (compose file, helper scripts), so a change to any of them ships with the deploy. Images come from
# GHCR (public), tagged with the SHA. Rollback = run it again with an older SHA (docs/runbook.md).
#
# Order: pull every image first (nothing changes yet) -> start mysql/redis -> Flyway migrate (forward
# only) -> only then restart the API and workers -> check /health reports this SHA.
set -euo pipefail

SHA="${1:?usage: deploy.sh <full 40-character git sha>}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "deploy: '$SHA' is not a full git sha" >&2; exit 2; }
REPO="Hritik456/pagecite"
RAW="https://raw.githubusercontent.com/$REPO/$SHA"
DIR=/opt/pagecite

cd "$DIR"
[[ -f .env ]] || { echo "deploy: $DIR/.env is missing (write it from deploy/env.prod.example)" >&2; exit 2; }
exec 9>"$DIR/.deploy.lock"
flock -n 9 || { echo "deploy: another deploy is running" >&2; exit 3; }
log() { echo "deploy $(date -u +%H:%M:%S) $*"; }

log "sha=$SHA: fetching the compose file and helper scripts at this commit"
fetch() { curl -fsSL --retry 3 "$RAW/$1" -o "$2.new" && mv "$2.new" "$2"; }
fetch docker-compose.prod.yml docker-compose.prod.yml
for script in backup.sh sample-memory.sh oom-check.sh; do
  fetch "deploy/$script" "$script"
  chmod 700 "$script"
done

# The compose wrapper used by every script and by hand (docs/runbook.md): pc ps, pc logs api, ...
cat > pc <<'EOF'
#!/bin/sh
exec docker compose --project-directory /opt/pagecite -f /opt/pagecite/docker-compose.prod.yml \
  --env-file /opt/pagecite/.env --env-file /opt/pagecite/deployed.env "$@"
EOF
chmod 700 pc

[[ -f deployed.env ]] && cp deployed.env previous.env
echo "IMAGE_TAG=$SHA" > next.env
PC=(docker compose --project-directory "$DIR" -f "$DIR/docker-compose.prod.yml" --env-file "$DIR/.env" --env-file "$DIR/next.env")

log "pulling images (the running containers are untouched until migrations pass)"
"${PC[@]}" --profile migrate pull --quiet

log "starting mysql + redis (no-op when already up)"
"${PC[@]}" up -d --wait --wait-timeout 300 mysql redis

log "flyway migrate (forward only)"
"${PC[@]}" --profile migrate run --rm flyway

mv next.env deployed.env
log "starting the new version"
./pc up -d --remove-orphans --wait --wait-timeout 300

got="$(./pc exec -T api python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5))['sha'])")"
[[ "$got" == "$SHA" ]] || { echo "deploy: /health reports sha=$got, expected $SHA" >&2; exit 4; }
log "api healthy at $SHA (previous: $(cut -d= -f2 previous.env 2>/dev/null || echo none))"

# Keep the last week of images (fast rollback), drop older unused ones: the disk is 20 GB.
docker image prune -af --filter "until=168h" >/dev/null || true
log "done"
