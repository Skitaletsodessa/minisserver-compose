#!/bin/bash
# Task 21 / 24: is adguard-sync itself healthy? Every minute reads its own status endpoint: origin and every replica must
# report status "success". 3 consecutive bad readings (container down, replica unreachable, auth failure, sync error) -> alert
# through watchnotify (reminder every 6 h while it lasts, recovery with the outage duration). The parity checker proves the
# RESULT is identical; this one says early why it stops being so.
set -uo pipefail
COUNT_DIR=${WATCH_COUNT_DIR:-/var/lib/adguard-sync-watch}
SYNC_ENV=${WATCH_SYNC_ENV:-/srv/compose/adguard-sync/.env}
SYNC_URL=${WATCH_SYNC_URL:-http://127.0.0.1:18086}
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
KEY=adguard-sync
mkdir -p "$COUNT_DIR"
set -a; . "$SYNC_ENV"; set +a
out=$(curl -s -m 10 -u "$SYNC_API_USER:$SYNC_API_PASSWORD" "$SYNC_URL/api/v1/status" 2>/dev/null)
verdict=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("sync API not answering"); sys.exit()
bad = []
if d.get("syncRunning"):                      # a pass is in progress right now: replica status reads "info", not an error
    print(""); sys.exit()
o = d.get("origin") or {}
if o.get("status") != "success": bad.append("origin %s: %s" % (o.get("host"), o.get("error") or o.get("status")))
for r in d.get("replicas") or []:
    if r.get("status") != "success": bad.append("replica %s: %s" % (r.get("host"), r.get("error") or r.get("status")))
print("; ".join(bad)[:300])
')
if [ -z "$verdict" ]; then
  echo 0 > "$COUNT_DIR/count"
  $NOTIFY clear "$KEY" "minisserver adguard-sync-watch: adguard-sync is healthy again (origin and replica both report success)."
  exit 0
fi
c=$(( $(cat "$COUNT_DIR/count" 2>/dev/null || echo 0) + 1 )); echo "$c" > "$COUNT_DIR/count"
echo "sync-watch: bad ($verdict) consecutive=$c" >&2
if [ "$c" -ge 3 ]; then
  $NOTIFY alert "$KEY" "minisserver adguard-sync-watch: adguard-sync has been unhealthy for $c checks: $verdict. The replica may be out of date."
fi
exit 0
