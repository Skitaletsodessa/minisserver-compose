#!/bin/bash
# Task 20: proves AdGuard Home is actually answering real queries, not just that
# port 53 is open. Runs every minute. Three consecutive bad checks -> one Telegram
# alert (not one per minute); recovery sends exactly one follow-up message.
#
# A check is "bad" if either:
#   - a normal domain doesn't come back with a real A record (DNS itself is down), or
#   - the known-blocked domain does NOT come back sinkholed (filtering engine isn't
#     actually processing queries, even if something is listening on :53)
set -uo pipefail

SERVER=${WATCH_TARGET:-192.168.31.2}      # Task 21: the same script watches the replica (WATCH_TARGET=192.168.31.5)
LABEL=${WATCH_LABEL:-origin}
NORMAL_DOMAIN=example.com
BLOCKED_DOMAIN=doubleclick.net

STATE_DIR=${WATCH_STATE_DIR:-/var/lib/adguard-watch}
COUNT_FILE="$STATE_DIR/consecutive-failures"
ALERTED_FILE="$STATE_DIR/alerted"
ENV_FILE=/srv/compose/scrutiny/.env

mkdir -p "$STATE_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "adguard-watch: $ENV_FILE not found, cannot get Telegram credentials" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" >/dev/null
}

normal_answer=$(dig @"$SERVER" "$NORMAL_DOMAIN" +short +time=3 +tries=1 A 2>/dev/null)
blocked_answer=$(dig @"$SERVER" "$BLOCKED_DOMAIN" +short +time=3 +tries=1 A 2>/dev/null)

normal_ok=false
if echo "$normal_answer" | grep -qE '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$'; then
    normal_ok=true
fi

blocked_ok=false
if [ "$blocked_answer" = "0.0.0.0" ]; then
    blocked_ok=true
fi

if $normal_ok && $blocked_ok; then
    if [ -f "$ALERTED_FILE" ]; then
        send_telegram "minisserver adguard-watch: AdGuard Home DNS ($LABEL, $SERVER) recovered (normal and blocked-domain checks both passing again)."
        rm -f "$ALERTED_FILE"
    fi
    echo 0 > "$COUNT_FILE"
    exit 0
fi

COUNT=0
if [ -f "$COUNT_FILE" ]; then
    COUNT=$(cat "$COUNT_FILE")
fi
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNT_FILE"

echo "adguard-watch: check failed (normal_ok=$normal_ok blocked_ok=$blocked_ok), consecutive=$COUNT" >&2

if [ "$COUNT" -ge 3 ] && [ ! -f "$ALERTED_FILE" ]; then
    touch "$ALERTED_FILE"
    send_telegram "minisserver adguard-watch: AdGuard Home DNS ($LABEL, $SERVER) has failed $COUNT consecutive checks.
normal domain ($NORMAL_DOMAIN) answer: ${normal_answer:-<none>}
blocked domain ($BLOCKED_DOMAIN) answer: ${blocked_answer:-<none>} (expected 0.0.0.0)"
fi

exit 0
