#!/usr/bin/env bash
# One-time host setup for the PageCite server (Ubuntu 24.04, t3.small). Idempotent: safe to run again.
# Run it in Session Manager (docs/deploy/aws-setup.md, step 10):
#
#   curl -fsSL https://raw.githubusercontent.com/Hritik456/pagecite/main/deploy/bootstrap.sh | sudo bash
#
# It never deploys: the deploy workflow runs deploy/deploy.sh, fetched at the commit it deploys.
set -euo pipefail
[[ "$(id -u)" == 0 ]] || { echo "bootstrap: run as root (sudo)" >&2; exit 1; }
RAW_MAIN="https://raw.githubusercontent.com/Hritik456/pagecite/main"
log() { echo "bootstrap: $*"; }

log "Docker CE + compose plugin (Docker's apt repository)"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

log "Docker log rotation defaults (every container: json-file, 10 MB x 3)"
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
systemctl enable docker containerd
systemctl restart docker

log "AWS CLI (snap; uses the instance role, no keys)"
command -v aws >/dev/null || snap install aws-cli --classic

log "2 GiB swap + kernel settings"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  [[ -f /swapfile ]] || fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
cat > /etc/sysctl.d/99-pagecite.conf <<'EOF'
# Prefer RAM, use swap only to absorb peaks (2 GB host).
vm.swappiness=10
# Redis forks for its AOF rewrite; without overcommit the fork can fail under memory pressure.
vm.overcommit_memory=1
EOF
sysctl --system >/dev/null

log "/opt/pagecite (root only)"
install -d -m 700 -o root -g root /opt/pagecite
if [[ ! -f /opt/pagecite/.env ]]; then
  curl -fsSL "$RAW_MAIN/deploy/env.prod.example" -o /opt/pagecite/.env
  chmod 600 /opt/pagecite/.env
  log "wrote /opt/pagecite/.env from the template: fill it in before the first deploy"
fi

log "daily MySQL backup to S3 at 02:30 IST (21:00 UTC); retention is the bucket's 7-day lifecycle rule"
cat > /etc/cron.d/pagecite-backup <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
0 21 * * * root [ -x /opt/pagecite/backup.sh ] && /opt/pagecite/backup.sh >> /var/log/pagecite-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/pagecite-backup
cat > /etc/logrotate.d/pagecite <<'EOF'
/var/log/pagecite-*.log {
  weekly
  rotate 4
  compress
  missingok
  notifempty
}
EOF

log "done. Next: fill /opt/pagecite/.env (chmod 600), then deploy (push to main or run the Deploy workflow)."
