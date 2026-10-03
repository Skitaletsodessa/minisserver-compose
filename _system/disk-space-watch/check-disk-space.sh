#!/bin/bash
# Alerts via Telegram when /srv/library crosses 80% used. qBittorrent now downloads
# straight onto /srv/library (Task 13, 2026-09-22, Ivan's decision) instead of the
# old hynix /srv/staging (removed); qbt-space-guard is the independent free-space
# pause for qBittorrent itself, this is the general "is the disk filling up" check,
# now covering the whole library disk instead of a dedicated staging device.
#
# Separate from disk-error-watch on purpose: that one greps kernel log events
# (a "did something happen" check), this one polls a numeric threshold (a
# "is a condition currently true" check) - different shape, own state file,
# own alert-once/reset-on-recovery logic rather than content-hash dedup.
set -euo pipefail

MOUNT=/srv/library
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

# Alerts through watchnotify (Task 24): the first message, a REMINDER every 6 h while the disk stays full, a recovery with the duration.
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
KEY="disk-space-$(echo "$MOUNT" | tr -c 'a-zA-Z0-9' '_')"
rm -f "$STATE_FILE"                      # the old "already alerted" flag; the shared state replaced it
if [ "$USED" -ge "$THRESHOLD" ]; then
    $NOTIFY alert "$KEY" "minisserver disk-space-watch: ${MOUNT} is ${USED}% full (threshold ${THRESHOLD}%). Check qBittorrent's own pause-on-low-space setting actually fired."
else
    $NOTIFY clear "$KEY" "minisserver disk-space-watch: ${MOUNT} is back to ${USED}% (threshold ${THRESHOLD}%)."
fi
