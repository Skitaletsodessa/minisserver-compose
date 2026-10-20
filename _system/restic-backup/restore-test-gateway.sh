#!/bin/bash
# Restore test for the portal gateway (Task 28 Phase I): restore the database copy, the audit mirror and the secret files from the LOCAL restic repository
# (or R2 with the argument r2) into a scratch directory, open the restored database with the gateway's own code inside a throw-away container made from the
# SAME image as the live gateway (no network, read-only, no Home Assistant, no schedule service), and check: integrity, the audit hash chain, people, that an
# enrolled device token still authenticates against the RESTORED database, and that the restored secret files are identical to the live ones.
#   sudo /srv/compose/_system/restic-backup/restore-test-gateway.sh [r2]
# Never run with `bash -x`: it handles real token files (it prints no secret, only comparisons).
set -euo pipefail
WORK=/srv/apps/restore-test-gateway
PROBE_TOKEN_FILE=${PROBE_TOKEN_FILE:-/home/skit/.config/gw-probe/probe-house}
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT
cleanup; mkdir -m 700 "$WORK"

if [ "${1:-}" = r2 ]; then
    set -a; . /srv/compose/_system/restic-r2.env; set +a; WHERE="R2 (off-site)"
else
    set -a; . /srv/compose/_system/restic-backup/.env; set +a; WHERE="local repository"
fi
echo "1. latest 'set' snapshot of the $WHERE"
restic snapshots --tag set --compact | grep -E "^[0-9a-f]{8} " | tail -n 1
echo "2. restore the database copy, the audit mirror and the secret files"
restic restore latest --tag set --target "$WORK" --include /srv/apps/backup-staging/portal-gateway --include /srv/apps/portal/gateway --include /srv/apps/portal/gateway-secrets 2>&1 | tail -1
R=$WORK/srv/apps
DB=$R/backup-staging/portal-gateway/gateway.db
ls -l "$DB" | awk '{print "   restored database:", $5, "bytes"}'
echo "   restored out/ (must be absent: one-time links are not backed up): $([ -e "$R/portal/gateway/out" ] && echo PRESENT-BAD || echo absent)"
echo "   restored live db files (must be absent, the copy above replaces them): $(ls "$R"/portal/gateway/gateway.db* 2>/dev/null | wc -l)"

echo "3. secret files: restored vs live (identical or not; no content is shown)"
for f in ha-token sched-admin sched-household; do
    if [ ! -e "$R/portal/gateway-secrets/$f" ]; then echo "   $f: MISSING in the restore"; continue; fi
    mode=$(stat -c %a "$R/portal/gateway-secrets/$f")
    same=$(cmp -s "$R/portal/gateway-secrets/$f" "/srv/apps/portal/gateway-secrets/$f" && echo identical-to-live || echo DIFFERS-from-live)
    echo "   $f: mode $mode, $same"
done

echo "4. open the restored database with the gateway's own code (same image as the live gateway, no network)"
mkdir -m 700 "$WORK/data"; cp "$DB" "$WORK/data/gateway.db"; chown -R 1000:1000 "$WORK/data"
mkdir -m 700 "$WORK/probe"; [ -s "$PROBE_TOKEN_FILE" ] && cp "$PROBE_TOKEN_FILE" "$WORK/probe/token"; chown -R 1000:1000 "$WORK/probe"
LIVE_IMG=$(docker inspect portal-gateway --format "{{.Config.Image}}")          # the tag the live container was created from (compose: portal-gateway:local)
RUN_ID=$(docker inspect portal-gateway --format "{{.Image}}" | cut -c1-19); TAG_ID=$(docker image inspect "$LIVE_IMG" --format "{{.Id}}" | cut -c1-19)
echo "   image: $LIVE_IMG ($TAG_ID); the running container was created from $RUN_ID$([ "$RUN_ID" = "$TAG_ID" ] && echo " = same" || echo " - DIFFERENT: the image was rebuilt since the last recreate")"
docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true -u 1000:1000 --memory 128m \
    -e GW_DATA=/data -e PYTHONDONTWRITEBYTECODE=1 -v "$WORK/data:/data" -v "$WORK/probe:/probe:ro" --entrypoint python "$LIVE_IMG" - <<'PYEOF'
import os, sys
from gw import db as dbmod, people as peoplemod
db = dbmod.DB("/data")
print("   integrity_check:", db.c.execute("PRAGMA integrity_check").fetchone()[0])
ok, rows, bad = db.verify_audit()
print("   audit chain: %s over %d rows%s" % ("INTACT" if ok else "BROKEN", rows, "" if ok else " (first bad seq %s)" % bad))
P = peoplemod.People(db)
ppl = P.list()
print("   people: %s" % ", ".join("%s(%s%s)" % (p["id"], p["role"], "" if p["active"] else ", inactive") for p in ppl))
print("   enrolled devices: %d (hashes only)" % len(P.phones()))
tok = "/probe/token"
if os.path.exists(tok):
    pr = P.phone_principal(open(tok).read().strip())
    print("   an enrolled device token authenticates against the RESTORED database: %s" % ("yes (%s)" % pr["person_id"] if pr else "NO"))
    if not pr:
        ok = False
else:
    print("   device-token check: skipped (no probe token file on this machine)")
sys.exit(0 if ok else 1)
PYEOF
echo "5. live comparison (read-only)"
live=$(sqlite3 "file:/srv/apps/portal/gateway/gateway.db?mode=ro" "select count(*) from audit" 2>/dev/null || echo "?")
echo "   live audit rows: $live (the restored copy is the snapshot; the difference is what happened since)"
echo "done; scratch copy removed"
