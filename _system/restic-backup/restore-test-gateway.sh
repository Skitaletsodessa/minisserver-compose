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
for f in ha-token sched-admin sched-household release-manifest.key release-manifest.pub; do
    if [ ! -e "$R/portal/gateway-secrets/$f" ]; then echo "   $f: MISSING in the restore"; continue; fi
    mode=$(stat -c %a "$R/portal/gateway-secrets/$f")
    same=$(cmp -s "$R/portal/gateway-secrets/$f" "/srv/apps/portal/gateway-secrets/$f" && echo identical-to-live || echo DIFFERS-from-live)
    echo "   $f: mode $mode, $same"
done

echo "3b. release store (Task 30c): restored APK files vs live (identical or not; sizes only)"
if [ -d "$R/portal/gateway/releases" ]; then
    n=0
    while IFS= read -r f; do
        b=${f#"$R/portal/gateway/releases/"}; live=/srv/apps/portal/gateway/releases/$b; n=$((n+1))
        echo "   $b: $(stat -c %s "$f") bytes, $(cmp -s "$f" "$live" && echo identical-to-live || echo DIFFERS-from-live)"
    done < <(find "$R/portal/gateway/releases" -type f)
    [ "$n" -gt 0 ] || echo "   releases/ restored but holds no file"
else
    echo "   releases/ MISSING in the restore"
fi
echo "4. open the restored database with the gateway's own code (same image as the live gateway, no network)"
mkdir -m 700 "$WORK/data"; cp "$DB" "$WORK/data/gateway.db"; chown -R 1000:1000 "$WORK/data"
mkdir -m 700 "$WORK/probe"; [ -s "$PROBE_TOKEN_FILE" ] && cp "$PROBE_TOKEN_FILE" "$WORK/probe/token"; chown -R 1000:1000 "$WORK/probe"
LIVE_IMG=$(docker inspect portal-gateway --format "{{.Config.Image}}")          # the tag the live container was created from (compose: portal-gateway:local)
RUN_ID=$(docker inspect portal-gateway --format "{{.Image}}" | cut -c1-19); TAG_ID=$(docker image inspect "$LIVE_IMG" --format "{{.Id}}" | cut -c1-19)
echo "   image: $LIVE_IMG ($TAG_ID); the running container was created from $RUN_ID$([ "$RUN_ID" = "$TAG_ID" ] && echo " = same" || echo " - DIFFERENT: the image was rebuilt since the last recreate")"
docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true -u 1000:1000 --memory 128m \
    -e GW_DATA=/data -e PYTHONDONTWRITEBYTECODE=1 -v "$WORK/data:/data" -v "$WORK/probe:/probe:ro" -v "$R/portal/gateway-secrets:/secrets:ro" -v "$R/portal/gateway/releases:/rel:ro" --entrypoint python "$LIVE_IMG" - <<'PYEOF'
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
import hashlib
rows = db.c.execute("SELECT version_code, version_name, sha256, size, state FROM releases ORDER BY version_code").fetchall()
for r in rows:
    f = "/rel/%d/app.apk" % r[0] if os.path.exists("/rel/%d/app.apk" % r[0]) else None
    h = hashlib.sha256(open(f, "rb").read()).hexdigest() if f and os.path.isfile(f) else None
    good = (h == r[2])
    print("   release %s (code %d, %s): restored file %s the recorded SHA-256" % (r[1], r[0], r[4], "MATCHES" if good else "DOES NOT MATCH" if h else "is MISSING, cannot match"))
    if not good:
        ok = False
if os.path.exists("/secrets/release-manifest.key") and os.path.exists("/secrets/release-manifest.pub"):
    from gw import ed25519
    seed = bytes.fromhex(open("/secrets/release-manifest.key").read().strip())
    pub = bytes.fromhex(open("/secrets/release-manifest.pub").read().strip())
    sig = ed25519.sign(seed, b"restore-test")
    works = ed25519.verify(pub, b"restore-test", sig) and ed25519.public_key(seed) == pub
    print("   restored manifest signing key signs and its public half verifies: %s" % ("yes" if works else "NO"))
    if not works:
        ok = False
else:
    print("   manifest signing key: MISSING in the restore")
    ok = False
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
