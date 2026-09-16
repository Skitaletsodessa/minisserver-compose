#!/bin/bash
# Alerts via Telegram when /srv/staging crosses 80% used - qBittorrent's own
# threshold should pause it before this, but this is the independent check
# that proves it actually did, or catches it if the qBittorrent-side setting
# is ever misconfigured or bypassed.
#
# Separate from disk-error-watch on purpose: that one greps kernel log events
# (a "did something happen" check), this one polls a numeric threshold (a
# "is a condition currently true" check) - different shape, own state file,
# own alert-once/reset-on-recovery logic rather than content-hash dedup.
set -euo pipefail

MOUNT=/srv/staging
THRESHOLD=80
STATE_DIR=/var/lib/disk-space-watch
STATE_FILE="$STATE_DIR/staging-alerted"
ENV_FILE=/srv/compose/scrutiny/.env

mkdir -p "$STATE_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "disk-space-watch: $ENV_FILE not found" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

USED=$(df --output=pcent "$MOUNT" | tail -1 | tr -dc '0-9')

if [ "$USED" -ge "$THRESHOLD" ]; then
    if [ -f "$STATE_FILE" ]; then
        exit 0
    fi
    touch "$STATE_FILE"
    MSG="minisserver disk-space-watch: ${MOUNT} is ${USED}% full (threshold ${THRESHOLD}%). Check qBittorrent's own pause-on-low-space setting actually fired."
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${MSG}" >/dev/null
else
    rm -f "$STATE_FILE"
fi
