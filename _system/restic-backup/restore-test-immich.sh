#!/bin/bash
# Restore the Immich Postgres dump FROM THE R2 REPOSITORY into a throwaway container and read it back
# (Task 08 section 2). Run as root on the server: sudo /srv/compose/_system/restic-backup/restore-test-immich.sh
# Re-run after any large change (first import, Immich upgrade): row counts must match the live DB.
# Nothing here touches the live immich_postgres container.
set -euo pipefail
WORK=/srv/apps/restore-test; NAME=immich-restore-scratch
IMG=$(docker inspect immich_postgres --format '{{.Config.Image}}')      # the exact pinned image
cleanup() { docker rm -f -v $NAME >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
cleanup; mkdir -m 700 "$WORK"

set -a; . /srv/compose/_system/restic-r2.env; set +a
echo "1. restore latest R2 snapshot's dump -> $WORK"
restic restore latest --tag set --target "$WORK" --include /srv/apps/backup-staging/immich/immich.sql.gz 2>&1 | tail -2
DUMP=$WORK/srv/apps/backup-staging/immich/immich.sql.gz
ls -l "$DUMP" | awk '{print "   restored file:", $5, "bytes"}'
gzip -t "$DUMP" && echo "   gzip -t ok"

echo "2. throwaway Postgres from the same image (no ports, no bind mounts)"
docker run -d --name $NAME -e POSTGRES_PASSWORD=scratch -e POSTGRES_USER=postgres -e POSTGRES_DB=immich \
    -e POSTGRES_INITDB_ARGS=--data-checksums --shm-size=128mb "$IMG" >/dev/null
for i in $(seq 1 60); do docker exec $NAME pg_isready -U postgres -d immich -q && { sleep 4; docker exec $NAME pg_isready -U postgres -d immich -q && break; }; sleep 2; done
docker exec $NAME pg_isready -U postgres -d immich

echo "3. restore, exactly as documented (search_path sed, single transaction, stop on first error)"
gunzip --stdout "$DUMP" \
 | sed "s/SELECT pg_catalog.set_config('search_path', '', false);/SELECT pg_catalog.set_config('search_path', 'public, pg_catalog', true);/g" \
 | docker exec -i $NAME psql --dbname=immich --username=postgres --single-transaction --set ON_ERROR_STOP=on -q >/dev/null
echo "   psql exit: $?"

echo "4. read it back: restored vs LIVE (same queries on both)"
q() { docker exec "$1" psql -U postgres -d immich -At -c "$2"; }
printf '   %-22s %10s %10s\n' table restored live
for t in '"user"' asset album asset_face person geodata_places partner kysely_migrations; do
    printf '   %-22s %10s %10s\n' "$t" "$(q $NAME "select count(*) from $t")" "$(q immich_postgres "select count(*) from $t")"
done
echo "   extensions restored: $(q $NAME "select string_agg(extname, ',' order by extname) from pg_extension")"
