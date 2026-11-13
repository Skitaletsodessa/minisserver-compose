#!/bin/bash
# Task 35 C3: after an update or a restart. Runs the project's checks (post-update-check.sh), appends ONE summary line to summary.log (git-tracked, pushed),
# and on any FAIL sends ONE tier-B message that NAMES what was updated; on OK it clears that alert.
#   usage: post-update-run.sh REASON [WAIT_SECONDS]      REASON = boot | apt | containers | manual
# Environment overrides (tests): NOTIFY, CHECK, SUMMARY, STATE_DIR, APT_HISTORY, UPDLOG, AU_DIR, NOGIT=1.   Never run with -x.
set -uo pipefail
REASON=${1:-manual}
WAIT=${2:-0}
DEFAULTS=${DEFAULTS_FILE:-/etc/default/minis-auto-reboot}
[ -r "$DEFAULTS" ] && { set -a; . "$DEFAULTS"; set +a; }
HOST=$(hostname)
DIR=${AU_DIR:-/srv/compose/_system/auto-update}
NOTIFY=${NOTIFY:-python3 /srv/compose/_system/lib/watchnotify.py}
CHECK=${CHECK:-/srv/compose/_system/post-update-check/post-update-check.sh}
SUMMARY=${SUMMARY:-$DIR/summary.log}
STATE=${STATE_DIR:-/var/lib/minis-auto-reboot}
APT_HISTORY=${APT_HISTORY:-/var/log/apt/history.log}
UPDLOG=${UPDLOG:-$DIR/container-updates.jsonl}
mkdir -p "$STATE"
sleep "$WAIT"
since=$(cat "$STATE/last-run" 2>/dev/null || echo "1970-01-01 00:00:00")

out=$($CHECK 2>&1)
fails=$(printf '%s\n' "$out" | grep -E '^ +FAIL' | sed 's/^ *FAIL *//' | cut -c1-110)
nfail=$(printf '%s' "$fails" | grep -c .)

IFS='|' read -r apt_list ctr_list kern < <(python3 "$DIR/updates-since.py" "$since" "$APT_HISTORY" "$UPDLOG" "$STATE/pending-reboot.json")
trim() { printf '%s' "$1" | sed 's/^ *//; s/ *$//'; }
apt_list=$(trim "${apt_list:--}"); ctr_list=$(trim "${ctr_list:--}"); kern=$(trim "${kern:--}")
line="$(date '+%Y-%m-%d %H:%M') | $HOST | $REASON | kernel: $kern | apt: $apt_list | containers: $ctr_list | "
if [ "$nfail" = 0 ]; then line="${line}check: OK"; else line="${line}check: FAIL($nfail): $(printf '%s' "$fails" | head -3 | tr '\n' ';' | cut -c1-240)"; fi
echo "$line" >> "$SUMMARY"
rm -f "$STATE/pending-reboot.json"
date '+%Y-%m-%d %H:%M:%S' > "$STATE/last-run"

if [ "$nfail" = 0 ]; then
  $NOTIFY clear "post-update-$HOST" "$HOST: the check after the update passes again." >/dev/null 2>&1
else
  $NOTIFY alert --tier B "post-update-$HOST" "$HOST: the check after an update ($REASON) found $nfail FAIL. Updated: apt: $apt_list; containers: $ctr_list; kernel: $kern. FAIL: $(printf '%s' "$fails" | head -4 | tr '\n' ';' | cut -c1-300). Details: ssh minis /srv/compose/_system/post-update-check/post-update-check.sh"
fi
if [ "${NOGIT:-0}" != "1" ]; then
  ( cd /srv/compose && git add "${SUMMARY#/srv/compose/}" >/dev/null 2>&1 && git commit -qm "auto-update: $REASON summary $(date +%F)" >/dev/null 2>&1 && git push -q >/dev/null 2>&1 ) || true
fi
echo "$line"
exit 0
