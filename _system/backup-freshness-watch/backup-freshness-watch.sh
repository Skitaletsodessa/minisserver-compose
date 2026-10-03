#!/bin/bash
# A FAILED backup already alerts (OnFailure= on the restic-backup* units). This catches the
# other failure mode report-06.md flagged: a backup that silently NEVER RUNS AT ALL (timer
# disabled, box off, a stuck lock) produces no failure to alert on - only a growing gap in the
# snapshot history shows it, and nothing was watching for that gap until now (Task 11 §3).
# Read-only restic commands only. Threshold is an argument so the forced-alert test can lower it
# without editing this file.
set -euo pipefail
THRESHOLD_HOURS=${1:-36}

# Alerts go through watchnotify (Task 24): delivery is logged, a failed send is retried, a still-stale repository is
# repeated (this job runs daily, so the reminder rides on the next run) and a recovery message closes it.
NOTIFY="python3 /srv/compose/_system/lib/watchnotify.py"
key_for() { printf 'backup-stale-%s' "$(printf '%s' "$1" | tr -c 'a-zA-Z0-9' '_')"; }
send_alert() {
    local name=$1 msg=$2
    $NOTIFY alert "$(key_for "$name")" "minisserver BACKUP STALE: $name

$msg"
}
clear_alert() {
    $NOTIFY clear "$(key_for "$1")" "minisserver backup freshness: $1 has a fresh snapshot again."
}

check_repo() {
    local name=$1 envfile=$2
    ( set -a; . "$envfile"; set +a
      restic snapshots --tag set --json 2>/dev/null ) > /tmp/backup-freshness-snaps.json.$$
    local newest
    newest=$(python3 -c "
import json
try:
    d = json.load(open('/tmp/backup-freshness-snaps.json.$$'))
except Exception:
    d = []
print(max((s['time'] for s in d), default='NONE'))
")
    rm -f /tmp/backup-freshness-snaps.json.$$
    if [ "$newest" = "NONE" ]; then
        echo "backup-freshness: $name has NO snapshot tagged 'set' at all" >&2
        send_alert "$name" "no snapshot tagged 'set' exists in this repository at all"
        return
    fi
    local age_h
    age_h=$(python3 -c "
import datetime
t = datetime.datetime.fromisoformat('$newest')
now = datetime.datetime.now(t.tzinfo)
print(int((now - t).total_seconds() / 3600))
")
    echo "backup-freshness: $name newest 'set' snapshot: $newest (${age_h}h old, threshold ${THRESHOLD_HOURS}h)"
    if [ "$age_h" -gt "$THRESHOLD_HOURS" ]; then
        send_alert "$name" "newest snapshot is ${age_h}h old (threshold ${THRESHOLD_HOURS}h): $newest"
    else
        clear_alert "$name"
    fi
}

check_repo "local (/srv/vault)" /srv/compose/_system/restic-backup/.env
check_repo "R2 (off-site)"      /srv/compose/_system/restic-r2.env
