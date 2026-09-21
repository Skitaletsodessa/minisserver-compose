#!/bin/bash
# restic backup of the backup set (backup-set.conf) to one of two repositories.
#   restic-backup.sh local      -> /srv/vault (Seagate), also backs up /srv/data as its own snapshot
#   restic-backup.sh r2         -> Cloudflare R2, the off-site copy (set only)
#   restic-backup.sh r2-prune   -> monthly: forget + prune the R2 repository (operation-heavy)
# Each run makes fresh consistent database copies first (backup-dump.sh) and does not run restic
# if that fails. Any non-zero exit triggers the Telegram alert via OnFailure= in the unit.
set -euo pipefail

TARGET=${1:?usage: restic-backup.sh local|r2|r2-prune}
DIR=/srv/compose/_system/restic-backup
SET=$DIR/backup-set.conf

case "$TARGET" in
local)
    ENV_FILE=$DIR/.env
    KEEP=(--keep-daily 14 --keep-weekly 8 --keep-monthly 12)   # unchanged from before Task 06
    PRUNE=(--prune)                                            # local prune is free
    ;;
r2|r2-prune)
    ENV_FILE=/srv/compose/_system/restic-r2.env
    KEEP=(--keep-daily 30 --keep-monthly 24)
    PRUNE=()                                                   # daily: never prune (class-A/B ops)
    [ "$TARGET" = r2-prune ] && PRUNE=(--prune)
    ;;
*) echo "unknown target: $TARGET" >&2; exit 2 ;;
esac

[ -f "$ENV_FILE" ] || { echo "restic-backup: $ENV_FILE not found" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [ "$TARGET" = r2-prune ]; then
    restic forget "${KEEP[@]}" "${PRUNE[@]}"
    exit 0
fi

# one run at a time on this host: the dump staging dir is shared by both targets
exec 9>/run/lock/restic-backup.lock
flock -w 900 9

"$DIR/backup-dump.sh"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
awk '$1=="path"{print $2}'       "$SET" > "$TMP/paths"
awk '$1=="exclude"{print $2}'    "$SET" > "$TMP/excludes"
awk '$1=="path-local"{print $2}' "$SET" > "$TMP/paths-local"

# every path must exist: a typo or a vanished directory must fail loudly, not back up less
while read -r p; do
    [ -e "$p" ] || { echo "restic-backup: path in backup-set.conf does not exist: $p" >&2; exit 1; }
done < <(cat "$TMP/paths"; [ "$TARGET" = local ] && cat "$TMP/paths-local" || true)

if [ "$TARGET" = local ]; then
    restic backup --tag data --files-from "$TMP/paths-local"
fi
restic backup --tag set --files-from "$TMP/paths" --exclude-file "$TMP/excludes"
restic forget "${KEEP[@]}" "${PRUNE[@]}"
