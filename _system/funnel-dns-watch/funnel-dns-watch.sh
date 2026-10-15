#!/bin/bash
# funnel-dns-watch.sh — checks that Tailscale Funnel DNS records are publicly resolvable.
# After 3 consecutive NXDOMAIN checks: auto-repairs (tailscale funnel reset + re-apply).
# If DNS is still broken in the next cycle after auto-repair: alerts via Telegram.
set -uo pipefail

HOSTNAME=minisserver.tail6bf4d5.ts.net
RESOLVERS=(1.1.1.1 8.8.8.8)
COUNT_DIR=/var/lib/funnel-dns-watch
COUNT_FILE="$COUNT_DIR/consecutive-failures"
AUTOFIX_FILE="$COUNT_DIR/autofix-timestamp"
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
KEY=funnel-dns
IPRE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'

mkdir -p "$COUNT_DIR"

public_ip=""
for resolver in "${RESOLVERS[@]}"; do
    while IFS= read -r ip; do
        # 100.x.x.x is Tailscale private — record exists only inside the tailnet via MagicDNS,
        # not a real Funnel relay address
        if echo "$ip" | grep -qE "$IPRE" && ! echo "$ip" | grep -q '^100\.'; then
            public_ip="$ip"
            break 2
        fi
    done < <(dig @"$resolver" +short +time=5 +tries=2 A "$HOSTNAME" 2>/dev/null)
done

if [ -n "$public_ip" ]; then
    PREV=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
    echo 0 > "$COUNT_FILE"
    if [ "$PREV" -ge 3 ] && [ -f "$AUTOFIX_FILE" ]; then
        $NOTIFY clear "$KEY" "minisserver funnel-dns-watch: Funnel DNS восстановлен после автофикса ($HOSTNAME → $public_ip)"
        rm -f "$AUTOFIX_FILE"
    else
        $NOTIFY clear "$KEY" "minisserver funnel-dns-watch: Funnel DNS OK ($HOSTNAME → $public_ip)"
    fi
    exit 0
fi

COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$COUNT" > "$COUNT_FILE"
echo "funnel-dns-watch: NXDOMAIN от ${RESOLVERS[*]}, consecutive=$COUNT" >&2

if [ "$COUNT" -ge 3 ]; then
    if [ ! -f "$AUTOFIX_FILE" ]; then
        # Первый раз: пробуем автофикс
        echo "$(date +%s)" > "$AUTOFIX_FILE"
        echo "funnel-dns-watch: запускаю автофикс tailscale funnel" >&2
        sudo tailscale funnel reset 2>&1 || true
        sleep 2
        sudo tailscale funnel --bg --https=10000 http://127.0.0.1:10000 2>&1 || true
        sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8443 2>&1 || true
        echo 0 > "$COUNT_FILE"
        $NOTIFY send "minisserver funnel-dns-watch: Funnel DNS не работал ($COUNT проверок). Автофикс применён — следующая проверка через 5 мин."
    else
        # Автофикс уже был, но DNS всё ещё не работает → алерт
        $NOTIFY alert "$KEY" "minisserver funnel-dns-watch: Funnel DNS сломан ($COUNT проверок после автофикса).
$HOSTNAME не резолвится через ${RESOLVERS[*]}.
Автофикс не помог — нужна ручная проверка:
  sudo tailscale funnel status
  sudo tailscale status"
    fi
fi
exit 0
