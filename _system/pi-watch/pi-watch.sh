#!/bin/bash
# Task 22: is piserver (the Pi) alive? Every minute: ping AND a TCP connect to port 22.
# Three consecutive bad checks -> one Telegram alert; recovery -> one message. Runs on the server,
# so the Pi holds no bot token.
set -uo pipefail
PI=192.168.31.5
STATE_DIR=/var/lib/pi-watch
COUNT_FILE="$STATE_DIR/consecutive-failures"
ALERTED_FILE="$STATE_DIR/alerted"
ENV_FILE=/srv/compose/scrutiny/.env
mkdir -p "$STATE_DIR"
[ -f "$ENV_FILE" ] || { echo "pi-watch: $ENV_FILE not found" >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a
send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null
}
ping_ok=false; tcp_ok=false
ping -c 2 -W 2 "$PI" >/dev/null 2>&1 && ping_ok=true
timeout 4 bash -c "exec 3<>/dev/tcp/$PI/22" 2>/dev/null && tcp_ok=true
if $ping_ok && $tcp_ok; then
    if [ -f "$ALERTED_FILE" ]; then
        send_telegram "minisserver pi-watch: piserver ($PI) is back (ping and SSH port both answer)."
        rm -f "$ALERTED_FILE"
    fi
    echo 0 > "$COUNT_FILE"; exit 0
fi
COUNT=$(cat "$COUNT_FILE" 2>/dev/null || echo 0); COUNT=$((COUNT + 1)); echo "$COUNT" > "$COUNT_FILE"
echo "pi-watch: check failed (ping=$ping_ok tcp22=$tcp_ok), consecutive=$COUNT" >&2
if [ "$COUNT" -ge 3 ] && [ ! -f "$ALERTED_FILE" ]; then
    touch "$ALERTED_FILE"
    send_telegram "minisserver pi-watch: piserver ($PI) failed $COUNT consecutive checks (ping=$ping_ok, ssh port=$tcp_ok)."
fi
exit 0
