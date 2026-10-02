#!/bin/bash
# Task 21: is adguard-sync itself healthy? Every minute reads its own status endpoint: origin and every replica
# must report status "success". 3 consecutive bad readings (container down, replica unreachable, auth failure,
# sync error) -> one Telegram message; recovery -> one. The parity checker proves the RESULT is identical;
# this one says early why it stops being so.
set -uo pipefail
STATE_DIR=/var/lib/adguard-sync-watch
ENV_FILE=/srv/compose/scrutiny/.env
SYNC_ENV=/srv/compose/adguard-sync/.env
mkdir -p "$STATE_DIR"
set -a; . "$ENV_FILE"; . "$SYNC_ENV"; set +a
send() { curl -s -m 20 -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
         --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null; }
out=$(curl -s -m 10 -u "$SYNC_API_USER:$SYNC_API_PASSWORD" http://127.0.0.1:18086/api/v1/status 2>/dev/null)
verdict=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("sync API not answering"); sys.exit()
bad = []
o = d.get("origin") or {}
if o.get("status") != "success": bad.append("origin %s: %s" % (o.get("host"), o.get("error") or o.get("status")))
for r in d.get("replicas") or []:
    if r.get("status") != "success": bad.append("replica %s: %s" % (r.get("host"), r.get("error") or r.get("status")))
print("; ".join(bad)[:300])
')
if [ -z "$verdict" ]; then
  [ -f "$STATE_DIR/alerted" ] && { send "minisserver adguard-sync-watch: adguard-sync is healthy again (origin and replica both report success)."; rm -f "$STATE_DIR/alerted"; }
  echo 0 > "$STATE_DIR/count"; exit 0
fi
c=$(cat "$STATE_DIR/count" 2>/dev/null || echo 0); c=$((c+1)); echo "$c" > "$STATE_DIR/count"
echo "sync-watch: bad ($verdict) consecutive=$c" >&2
if [ "$c" -ge 3 ] && [ ! -f "$STATE_DIR/alerted" ]; then
  touch "$STATE_DIR/alerted"
  send "minisserver adguard-sync-watch: adguard-sync has been unhealthy for $c checks: $verdict. The replica may be out of date."
fi
exit 0
