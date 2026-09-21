#!/bin/bash
# Makes the consistent database copies listed in backup-set.conf into /srv/apps/backup-staging.
# A live database is never file-copied while its owner runs (that restores to a corrupt DB often
# enough not to risk it):
#   sqlite    -> `sqlite3 -readonly .backup` (no downtime), then PRAGMA integrity_check must say "ok"
#   pgdump    -> pg_dump inside the running container (no downtime), gzip -t + completion trailer checked
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
    pgdump)
        # pg_dump from the RUNNING container: a consistent MVCC snapshot, no downtime. Same
        # command and format as Immich's documented CLI backup (--clean --if-exists, gzip), so
        # the file is also accepted by Immich's own restore. No -t: a tty would turn \n into \r\n.
        # --rsyncable: plain gzip shifts the whole stream after any early change, so restic could not
        # dedup day N against day N-1 (measured 2026-09-21 on real dumps: +17.5 MiB/day vs +2.5 MiB/day).
        name=$a; container=$b; pguser=$c; pgdb=$d
        mkdir -p -m 700 "$STAGE/$name"
        out="$STAGE/$name/$pgdb.sql.gz"
        rm -f "$out.new"
        docker exec "$container" pg_dump --clean --if-exists --dbname="$pgdb" --username="$pguser" </dev/null | gzip --rsyncable > "$out.new"
        gzip -t "$out.new"
        # pg_dump writes this trailer last: its absence means a truncated dump
        gzip -dc "$out.new" | tail -n 5 | grep -q "PostgreSQL database dump complete" \
            || { echo "backup-dump: pg_dump of $name is truncated (no completion trailer)" >&2; exit 1; }
        mv -f "$out.new" "$out"
        echo "dumped postgres $name ($(stat -c %s "$out") bytes, gzip ok, trailer ok)"
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
