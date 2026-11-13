#!/bin/bash
# Task 35 C3: ExecStartPost of apt-daily-upgrade.service. If that pass installed packages since the last post-update run, start the check in the background
# (minis-post-update@apt.service, after a short wait). Never fails the apt unit.
DEFAULTS=${DEFAULTS_FILE:-/etc/default/minis-auto-reboot}
[ -r "$DEFAULTS" ] && { set -a; . "$DEFAULTS"; set +a; }
DIR=${AU_DIR:-/srv/compose/_system/auto-update}
STATE=${STATE_DIR:-/var/lib/minis-auto-reboot}
mkdir -p "$STATE"
since=$(cat "$STATE/last-run" 2>/dev/null || echo "1970-01-01 00:00:00")
first=$(python3 "$DIR/updates-since.py" "$since" /var/log/apt/history.log /dev/null /dev/null 2>/dev/null | cut -d"|" -f1 | tr -d " ")
if [ -n "$first" ] && [ "$first" != "-" ]; then
  systemctl start --no-block minis-post-update@apt.service
fi
exit 0
