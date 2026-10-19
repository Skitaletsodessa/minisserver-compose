#!/bin/bash
# Restore test for the portal schedule service (Task 27 Phase F): restore its database copy + applier state from the LOCAL restic
# repository (or R2 with the argument r2) into a scratch directory, open the restored database with the service's own code in a
# throw-away process (no container, no network, no Home Assistant), check integrity and the audit hash chain, and compare with live.
#   sudo /srv/compose/_system/restic-backup/restore-test-portal.sh [r2]
# Never run with `bash -x`: the applier directory holds the reload webhook id.
set -euo pipefail
WORK=/srv/apps/restore-test-portal
SRC=/srv/compose/portal/schedules
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
echo "2. restore the database copy and the applier state"
restic restore latest --tag set --target "$WORK" --include /srv/apps/backup-staging/portal-schedules --include /srv/apps/portal/applier 2>&1 | tail -1
DB=$WORK/srv/apps/backup-staging/portal-schedules/schedules.db
ls -l "$DB" | awk '{print "   restored database:", $5, "bytes"}'
echo "   applier state restored: $(find "$WORK/srv/apps/portal/applier" -type f | wc -l) files (webhook id present: $([ -s "$WORK/srv/apps/portal/applier/webhook-id" ] && echo yes || echo NO))"

echo "3. open the restored database with the service's own code"
mkdir -m 700 "$WORK/data"; cp "$DB" "$WORK/data/schedules.db"
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$SRC python3 - "$WORK/data" <<'PYEOF'
import sys, sqlite3, json
from sched import store
st = store.Store(sys.argv[1])
ok, rows, bad = st.verify_audit()
print("   integrity_check:", st.db.execute("PRAGMA integrity_check").fetchone()[0])
print("   audit chain: %s over %d rows%s" % ("INTACT" if ok else "BROKEN", rows, "" if ok else " (first bad seq %s)" % bad))
ents = st.entities()
print("   entities: %d (%s); managed: %s" % (len(ents), ", ".join(sorted({e["kind"] for e in ents})), ", ".join(e["id"] for e in ents if e["managed"]) or "none"))
print("   tokens (hashes only): %d" % len(st.tokens()))
snap = st.snapshot()
print("   schedules readable: %d rules in total" % sum(len(e["rules"]) for e in snap["entities"]))
# compare with live (read-only)
live = sqlite3.connect("file:/srv/apps/portal/schedules/schedules.db?mode=ro", uri=True)
l_audit = live.execute("select count(*) from audit").fetchone()[0]
print("   live audit rows: %d (restored %d; the difference is what happened since the snapshot)" % (l_audit, rows))
l_rules = {r[0]: r[1] for r in live.execute("select entity_id, rules from schedules")}
r_rules = {r[0]: r[1] for r in st.db.execute("select entity_id, rules from schedules")}
diff = [k for k in r_rules if l_rules.get(k) != r_rules[k]]
print("   schedules identical to live: %s%s" % ("yes" if not diff else "NO, differ: ", ", ".join(diff)))
sys.exit(0 if ok else 1)
PYEOF
echo "done; scratch copy removed"
