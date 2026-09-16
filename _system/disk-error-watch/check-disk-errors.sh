#!/bin/bash
# Watches the last 20 minutes of kernel log for disk/USB-bus failure signatures
# and alerts via the same Telegram bot Scrutiny uses. Catches the failure mode
# that killed the Toshiba (repeated USB bus resets, no SMART threshold involved) -
# something Scrutiny's scheduled SMART polling would not have seen.
set -euo pipefail

STATE_DIR=/var/lib/disk-error-watch
STATE_FILE="$STATE_DIR/last-alert.hash"
ENV_FILE=/srv/compose/scrutiny/.env

mkdir -p "$STATE_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "disk-error-watch: $ENV_FILE not found, cannot get Telegram credentials" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

PATTERN='uas_eh_device_reset_handler|reset SuperSpeed|I/O error|Medium Error|failed command|EXT4-fs error|Buffer I/O error'

MATCHES=$(journalctl --since "-20 min" -k --no-pager 2>/dev/null | grep -iE "$PATTERN" || true)

if [ -z "$MATCHES" ]; then
    exit 0
fi

HASH=$(printf '%s' "$MATCHES" | sha256sum | cut -d' ' -f1)
LAST_HASH=""
if [ -f "$STATE_FILE" ]; then
    LAST_HASH=$(cat "$STATE_FILE")
fi

if [ "$HASH" = "$LAST_HASH" ]; then
    exit 0
fi

printf '%s' "$HASH" > "$STATE_FILE"

MSG="minisserver disk-error-watch: kernel log matched a failure signature in the last 20 min

$(printf '%s' "$MATCHES" | tail -20)"

curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${MSG}" >/dev/null
