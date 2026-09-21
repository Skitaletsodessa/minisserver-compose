#!/bin/bash
# Makes the consistent database copies listed in backup-set.conf into /srv/apps/backup-staging.
# A live database is never file-copied while its owner runs (that restores to a corrupt DB often
# enough not to risk it):
#   sqlite    -> `sqlite3 -readonly .backup` (no downtime), then PRAGMA integrity_check must say "ok"
#   stopcopy  -> stop the container, cp -a, start it, and verify it answers again
# Fails loudly (non-zero) on any problem; restic-backup.sh does not run restic after a failed dump.
set -euo pipefail

SET=/srv/compose/_system/restic-backup/backup-set.conf
STAGE=/srv/apps/backup-staging
mkdir -p -m 700 "$STAGE"

restart_pending=""
health_url=""
restart() {
    [ -n "$restart_pending" ] || return 0
    docker start "$restart_pending" >/dev/null
    for _ in $(seq 1 30); do
        curl -sf -o /dev/null "$health_url" && { restart_pending=""; return 0; }
        sleep 2
    done
    echo "backup-dump: $restart_pending did not answer after restart" >&2
    return 1
}
trap restart EXIT

while read -r kind a b c d _; do
    case "$kind" in ""|"#"*) continue ;; esac
    case "$kind" in
    sqlite)
        name=$a; src=$b
        mkdir -p -m 700 "$STAGE/$name"
        out="$STAGE/$name/$(basename "$src")"
        rm -f "$out.new"
        sqlite3 -readonly "$src" ".timeout 20000" ".backup '$out.new'" </dev/null
        chk=$(sqlite3 "$out.new" "PRAGMA integrity_check")
        [ "$chk" = "ok" ] || { echo "backup-dump: integrity_check FAILED for $name: $chk" >&2; exit 1; }
        mv -f "$out.new" "$out"
        echo "dumped sqlite $name ($(stat -c %s "$out") bytes, integrity ok)"
        ;;
    stopcopy)
        name=$a; container=$b; src=$c; health_url=$d
        rm -rf "$STAGE/$name.new"
        restart_pending=$container
        docker stop "$container" >/dev/null </dev/null
        cp -a "$src" "$STAGE/$name.new"
        restart          # start + verify BEFORE the slow parts; downtime = the copy only
        rm -rf "$STAGE/$name"
        mv "$STAGE/$name.new" "$STAGE/$name"
        echo "copied $name while $container was stopped ($(du -sb "$STAGE/$name" | cut -f1) bytes)"
        ;;
    esac
done < "$SET"
