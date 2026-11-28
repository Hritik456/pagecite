#!/usr/bin/env bash
# After the prod ingest (P5B "Done when"): was any container killed for memory?
#   sudo /opt/pagecite/oom-check.sh
# Exit 0 only if no container was OOM-killed or restarted, and the kernel logged no OOM kill.
set -uo pipefail
cd /opt/pagecite || exit 2
status=0
echo "== containers"
for id in $(./pc ps -aq); do
  line="$(docker inspect -f '{{.Name}} oom_killed={{.State.OOMKilled}} restarts={{.RestartCount}} status={{.State.Status}} started={{.State.StartedAt}}' "$id")"
  echo "  ${line#/}"
  [[ "$line" == *"oom_killed=false restarts=0 "* ]] || status=1
done
echo "== kernel log (OOM)"
if dmesg -T 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process'; then
  status=1
else
  echo "  none"
fi
echo "== memory now"
free -m
swapon --show
exit "$status"
