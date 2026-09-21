#!/bin/bash
# Telegram alert for a failed unit (called by backup-notify@.service via OnFailure=).
# Same bot/credentials as every other alert (scrutiny/.env). Sends the unit name and the last
# few journal lines - never the environment.
set -euo pipefail
UNIT=${1:?unit}
set -a; source /srv/compose/scrutiny/.env; set +a
TAIL=$(journalctl -u "$UNIT" -n 8 --no-pager -o cat 2>/dev/null | cut -c1-200 || true)
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=minisserver BACKUP FAILED: ${UNIT}

${TAIL}" >/dev/null
