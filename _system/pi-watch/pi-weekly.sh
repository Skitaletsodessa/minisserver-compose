#!/bin/bash
# Task 22: weekly health/updates line for the Pi, pulled over SSH with a forced-command key
# (the key can run only /usr/local/bin/pi-report on the Pi). Nothing is applied automatically
# (CLAUDE.md rule 5); Ivan decides and updates by hand. Telegram goes out from the server.
set -uo pipefail
ENV_FILE=/srv/compose/scrutiny/.env
set -a; source "$ENV_FILE"; set +a
out=$(ssh -T -q -i /home/skit/.ssh/piserver_report -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 \
      -o StrictHostKeyChecking=accept-new skit@192.168.31.5 2>&1) || out="pi-weekly: could not reach the Pi: $out"
echo "$out"
python3 /srv/compose/_system/lib/watchnotify.py send "piserver weekly:
$out"
