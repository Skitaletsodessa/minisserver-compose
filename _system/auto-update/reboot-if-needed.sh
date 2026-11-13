#!/bin/bash
# Task 35 C1 (Ivan 2026-11-13: everything updates automatically; the two machines restart on different nights).
# Reboot THIS machine only when apt left /var/run/reboot-required, only inside the allowed time window, and only when the PEER's AdGuard answers DNS
# (the house must keep a resolver: the server reboots only while the Pi answers, the Pi only while the server answers). Otherwise: postpone and send ONE tier-B message.
# Config comes from /etc/default/minis-auto-reboot (NAME, PEER_DNS, NOTIFY) and can be overridden by the environment (tests): DRY=1 prints instead of rebooting.
# Never run with -x. Exit status 0 always (a postponement is not a failure of the unit).
set -uo pipefail
DEFAULTS=${DEFAULTS_FILE:-/etc/default/minis-auto-reboot}
[ -r "$DEFAULTS" ] && { set -a; . "$DEFAULTS"; set +a; }        # NAME, PEER_DNS, NOTIFY (+ WATCH_* for the notifier); the tests pass DEFAULTS_FILE=/dev/null
NAME=${NAME:-$(hostname)}
PEER_DNS=${PEER_DNS:?PEER_DNS missing}
NOTIFY=${NOTIFY:?NOTIFY missing}
STATE_DIR=${STATE_DIR:-/var/lib/minis-auto-reboot}
LATEST=${LATEST_HHMM:-0745}                                  # a late retry never reboots after this time of day (the 08:00 heartbeat and the first vacuum plan are close)
DRY=${DRY:-0}
REQ=${REQ_FILE:-/var/run/reboot-required}
mkdir -p "$STATE_DIR"
KEY="reboot-postponed-$NAME"

if [ ! -e "$REQ" ]; then
  echo "$NAME: no reboot required"
  $NOTIFY clear "$KEY" "$NAME: no reboot pending any more." >/dev/null 2>&1
  exit 0
fi
now=$(date +%H%M)
if [ "$DRY" != "1" ] && [ "$now" -gt "$LATEST" ]; then
  echo "$NAME: reboot required but it is $now (after $LATEST): left for the next slot"
  exit 0
fi

peer_ok() { dig @"$PEER_DNS" example.com +short +time=3 +tries=1 A 2>/dev/null | grep -qE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; }
if ! peer_ok; then
  sleep "${PEER_RETRY_WAIT:-20}"
  if ! peer_ok; then
    pk=$(tr '\n' ' ' < "$REQ.pkgs" 2>/dev/null | cut -c1-120)
    echo "$NAME: reboot postponed, $PEER_DNS does not answer DNS"
    $NOTIFY alert --tier B "$KEY" "$NAME: a restart is needed (${pk:-updates installed}) but the other AdGuard ($PEER_DNS) does not answer DNS, so the restart is postponed; it is retried every 30 min until $LATEST and again at the next slot. Check the other machine."
    exit 0
  fi
fi

pkgs=$(tr '\n' ' ' < "$REQ.pkgs" 2>/dev/null | sed 's/ *$//')
python3 - "$STATE_DIR/pending-reboot.json" "$pkgs" "$(uname -r)" <<'PY'
import json, sys, time
json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason_pkgs": sys.argv[2], "kernel_before": sys.argv[3]}, open(sys.argv[1], "w"))
PY
if [ "$DRY" = "1" ]; then echo "$NAME: DRY RUN - would reboot now (packages: ${pkgs:-none listed})"; exit 0; fi
echo "$NAME: rebooting now (packages: ${pkgs:-none listed})"
sync
systemctl reboot
exit 0
