#!/bin/bash
# Daily restic backup of /srv/data (Tier-1) into the local repository on /srv/vault.
# Repository is on the Seagate - a different physical device from /srv/data (NVMe),
# which is the property that matters: Tier-1 and its only local copy are never on
# the same disk. This is still a SINGLE-DEVICE-TYPE backup, not an off-site one -
# do not treat it as satisfying the 3-2-1 rule on its own.
set -euo pipefail

ENV_FILE=/srv/compose/_system/restic-backup/.env
if [ ! -f "$ENV_FILE" ]; then
    echo "restic-backup: $ENV_FILE not found" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

restic backup /srv/data
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
