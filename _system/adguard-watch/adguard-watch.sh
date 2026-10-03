#!/bin/bash
# Task 20 / 24: proves AdGuard Home is actually answering real queries, not just that port 53 is open. Runs every minute.
# Three consecutive bad checks -> an alert through watchnotify (first message, then a REMINDER every 6 h while it lasts,
# then a recovery message with the outage duration). Used for the origin and, with WATCH_TARGET/WATCH_LABEL, the replica.
#
# A check is "bad" if either:
#   - a normal domain doesn't come back with a real A record (DNS itself is down), or
#   - the known-blocked domain does NOT come back sinkholed (the filtering engine isn't really processing queries)
set -uo pipefail

SERVER=${WATCH_TARGET:-192.168.31.2}      # the same script watches the replica (WATCH_TARGET=192.168.31.5)
LABEL=${WATCH_LABEL:-origin}
NORMAL_DOMAIN=example.com
BLOCKED_DOMAIN=doubleclick.net
COUNT_DIR=${WATCH_COUNT_DIR:-/var/lib/adguard-watch}
COUNT_FILE="$COUNT_DIR/consecutive-failures"
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
KEY="adguard-watch-$LABEL"
mkdir -p "$COUNT_DIR"

# only the IP lines of the answer are kept: dig prints its error text on stdout, which made alert messages 20 lines long
IPRE='^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
normal_answer=$(dig @"$SERVER" "$NORMAL_DOMAIN" +short +time=3 +tries=1 A 2>/dev/null | grep -E "$IPRE" | head -3 | paste -sd' ')
blocked_answer=$(dig @"$SERVER" "$BLOCKED_DOMAIN" +short +time=3 +tries=1 A 2>/dev/null | grep -E "$IPRE" | head -3 | paste -sd' ')

normal_ok=false
[ -n "$normal_answer" ] && normal_ok=true
blocked_ok=false
[ "$blocked_answer" = "0.0.0.0" ] && blocked_ok=true

if $normal_ok && $blocked_ok; then
    echo 0 > "$COUNT_FILE"
    $NOTIFY clear "$KEY" "minisserver adguard-watch: AdGuard Home DNS ($LABEL, $SERVER) recovered (normal and blocked-domain checks both passing again)."
    exit 0
fi

COUNT=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$COUNT" > "$COUNT_FILE"
echo "adguard-watch: check failed (normal_ok=$normal_ok blocked_ok=$blocked_ok), consecutive=$COUNT" >&2

if [ "$COUNT" -ge 3 ]; then
    $NOTIFY alert "$KEY" "minisserver adguard-watch: AdGuard Home DNS ($LABEL, $SERVER) has failed $COUNT consecutive checks.
normal domain ($NORMAL_DOMAIN) answer: ${normal_answer:-<none>}
blocked domain ($BLOCKED_DOMAIN) answer: ${blocked_answer:-<none>} (expected 0.0.0.0)"
fi
exit 0
