#!/bin/bash
# qBittorrent has no native "pause when free space below X" preference - checked
# its full WebAPI preferences dump directly, no such key exists (confirmed against
# v5.2.3). This script is the substitute task-03 asked for: pause all torrents
# when /srv/library's free space drops below PAUSE_BELOW_GB, resume once it
# recovers above RESUME_ABOVE_GB (hysteresis band so it doesn't flap right at
# the boundary).
#
# Repointed 2026-09-22 (Task 13): the hynix (/srv/staging) is physically removed;
# qBittorrent now downloads straight onto the Seagate (/srv/library), Ivan's
# decision (low download volume, auto-remove on completion). This guard now
# protects the whole library disk, not a dedicated disposable staging device -
# still useful, arguably more so.
set -euo pipefail

MOUNT=/srv/library
PAUSE_BELOW_GB=15
RESUME_ABOVE_GB=20
STATE_DIR=/var/lib/qbt-space-guard
STATE_FILE="$STATE_DIR/paused"
QBT_ENV=/srv/compose/qbittorrent/.env
TG_ENV=/srv/compose/scrutiny/.env
QBT_URL=http://localhost:8080
COOKIE_JAR="$STATE_DIR/cookie"

mkdir -p "$STATE_DIR"

for f in "$QBT_ENV" "$TG_ENV"; do
    if [ ! -f "$f" ]; then
        echo "qbt-space-guard: $f not found" >&2
        exit 1
    fi
done
set -a
# shellcheck disable=SC1090
source "$QBT_ENV"
# shellcheck disable=SC1090
source "$TG_ENV"
set +a

AVAIL_KB=$(df --output=avail "$MOUNT" | tail -1 | tr -dc '0-9')
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))

qbt_login() {
    docker exec qbittorrent curl -s -c "$COOKIE_JAR" --referer "$QBT_URL" \
        --data-urlencode "username=${QBT_WEBUI_USER}" \
        --data-urlencode "password=${QBT_WEBUI_PASSWORD}" \
        "$QBT_URL/api/v2/auth/login" >/dev/null
}

telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" >/dev/null
}

if [ "$AVAIL_GB" -lt "$PAUSE_BELOW_GB" ]; then
    if [ ! -f "$STATE_FILE" ]; then
        qbt_login
        docker exec qbittorrent curl -s -b "$COOKIE_JAR" --referer "$QBT_URL" \
            -X POST "$QBT_URL/api/v2/torrents/pause" --data-urlencode "hashes=all" >/dev/null
        touch "$STATE_FILE"
        telegram "minisserver qbt-space-guard: /srv/library has ${AVAIL_GB}GB free (below ${PAUSE_BELOW_GB}GB) - all torrents paused."
    fi
elif [ "$AVAIL_GB" -ge "$RESUME_ABOVE_GB" ]; then
    if [ -f "$STATE_FILE" ]; then
        qbt_login
        docker exec qbittorrent curl -s -b "$COOKIE_JAR" --referer "$QBT_URL" \
            -X POST "$QBT_URL/api/v2/torrents/resume" --data-urlencode "hashes=all" >/dev/null
        rm -f "$STATE_FILE"
        telegram "minisserver qbt-space-guard: /srv/library has ${AVAIL_GB}GB free again (above ${RESUME_ABOVE_GB}GB) - torrents resumed."
    fi
fi
