#!/bin/bash
# Task 22 / 24: is piserver (the Pi) alive? Every minute: ping AND a TCP connect to port 22. Three consecutive bad checks ->
# alert through watchnotify (reminder every 6 h while it lasts, recovery with the outage duration). Runs on the server, so
# the Pi holds no bot token for this.
set -uo pipefail
PI=${WATCH_PI:-192.168.31.5}
COUNT_DIR=${WATCH_COUNT_DIR:-/var/lib/pi-watch}
COUNT_FILE="$COUNT_DIR/consecutive-failures"
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
KEY=pi-watch
mkdir -p "$COUNT_DIR"
ping_ok=false; tcp_ok=false
ping -c 2 -W 2 "$PI" >/dev/null 2>&1 && ping_ok=true
timeout 4 bash -c "exec 3<>/dev/tcp/$PI/22" 2>/dev/null && tcp_ok=true
if $ping_ok && $tcp_ok; then
    echo 0 > "$COUNT_FILE"
    $NOTIFY clear "$KEY" "minisserver pi-watch: piserver ($PI) is back (ping and SSH port both answer)."
    exit 0
fi
COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 )); echo "$COUNT" > "$COUNT_FILE"
echo "pi-watch: check failed (ping=$ping_ok tcp22=$tcp_ok), consecutive=$COUNT" >&2
if [ "$COUNT" -ge 3 ]; then
    $NOTIFY alert "$KEY" "minisserver pi-watch: piserver ($PI) failed $COUNT consecutive checks (ping=$ping_ok, ssh port=$tcp_ok)."
fi
exit 0
