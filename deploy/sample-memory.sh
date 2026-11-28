#!/usr/bin/env bash
# Memory samples during the prod ingest (P5B "Done when"): free -m and per-container usage every 5 s.
#   sudo /opt/pagecite/sample-memory.sh            # until Ctrl-C
#   sudo /opt/pagecite/sample-memory.sh 900        # for 900 s
# Output: /var/log/pagecite-mem.log (and a short line per sample on the console).
set -uo pipefail
DURATION="${1:-0}"
LOG=/var/log/pagecite-mem.log
end=$(( $(date +%s) + DURATION ))
echo "== sampling start $(date -u +%FT%TZ)" >> "$LOG"
while (( DURATION == 0 || $(date +%s) < end )); do
  ts="$(date -u +%FT%TZ)"
  read -r used avail < <(free -m | awk '/^Mem:/ {print $3, $7}')
  read -r sused < <(free -m | awk '/^Swap:/ {print $3}')
  echo "$ts mem_used=${used}M avail=${avail}M swap_used=${sused}M" | tee -a "$LOG"
  docker stats --no-stream --format '  {{.Name}} {{.MemUsage}} cpu={{.CPUPerc}}' >> "$LOG" 2>&1
  sleep 5
done
echo "== sampling end $(date -u +%FT%TZ)" >> "$LOG"
