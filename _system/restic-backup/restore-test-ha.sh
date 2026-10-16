#!/bin/bash
# Restore the Home Assistant configuration FROM THE LOCAL restic repository into a scratch directory, start a throw-away
# Home Assistant on it WITHOUT ANY NETWORK and read back what it loaded (Task 25 Phase G).
#   sudo /srv/compose/_system/restic-backup/restore-test-ha.sh          # local repository
#   sudo /srv/compose/_system/restic-backup/restore-test-ha.sh r2       # also list + restore two small files from R2
# Never run it with `bash -x`: the scratch copy holds device keys and tokens, the repository password is sourced here.
# Why --network none: a Tuya plug accepts one local connection and a Xiaomi device must not be talked to by a second
# controller; with no network namespace the scratch instance cannot reach any device, the router, or the internet.
# Nothing here touches the live homeassistant container; the scratch copy is deleted on exit.
set -euo pipefail
WORK=/srv/apps/restore-test-ha; NAME=ha-restore-scratch
IMG=$(docker inspect homeassistant --format '{{.Config.Image}}')            # the exact pinned image
LIVE=/srv/apps/homeassistant; LIVEGIT=/srv/compose/homeassistant
cleanup() { docker rm -f -v $NAME >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT
cleanup; mkdir -m 700 "$WORK"

set -a; . /srv/compose/_system/restic-backup/.env; set +a

echo "1. latest 'set' snapshot of the local repository"
restic snapshots --tag set --compact | grep -E "^[0-9a-f]{8} " | tail -n 1

echo "2. restore /srv/apps/homeassistant and /srv/compose/homeassistant -> $WORK"
T0=$(date +%s)
restic restore latest --tag set --target "$WORK" --include /srv/apps/homeassistant --include /srv/compose/homeassistant 2>&1 | tail -2
echo "   restore took $(( $(date +%s) - T0 )) s"
CFG=$WORK$LIVE; GIT=$WORK$LIVEGIT
echo "   files restored: config dir $(find "$CFG" -type f | wc -l), compose dir $(find "$GIT" -type f | wc -l)"
echo "   recorder DB in the restore (must be none, it is excluded by design): $(ls "$CFG"/home-assistant_v2.db* 2>/dev/null | wc -l) files"

echo "3. restored vs live (sha256 prefix; the live files may have changed since the snapshot)"
cmp12() { a=$(sha256sum "$1" 2>/dev/null | cut -c1-12); b=$(sha256sum "$2" 2>/dev/null | cut -c1-12); [ "$a" = "$b" ] && s=same || s=DIFFERENT; printf '   %-44s %s  %s %s\n' "$3" "$s" "${a:-missing}" "${b:-missing}"; }
for f in core.config_entries core.entity_registry core.device_registry core.area_registry http auth; do cmp12 "$CFG/.storage/$f" "$LIVE/.storage/$f" ".storage/$f"; done
for f in configuration.yaml automations.yaml scripts.yaml; do cmp12 "$GIT/$f" "$LIVEGIT/$f" "compose/$f"; done
for c in tuya_local xiaomi_miot; do
    n=$(diff -rq -x __pycache__ "$CFG/custom_components/$c" "$LIVE/custom_components/$c" 2>&1 | wc -l)
    printf '   %-44s %s (%s differing paths)\n' "custom_components/$c" "$([ "$n" = 0 ] && echo same || echo DIFFERENT)" "$n"
done
echo "   config entries by domain, restored | live:"
python3 - "$CFG" "$LIVE" <<'EOF'
import json, sys, collections
def cnt(p):
    d = json.load(open(p + "/.storage/core.config_entries"))
    return collections.Counter(e["domain"] for e in d["data"]["entries"])
a, b = cnt(sys.argv[1]), cnt(sys.argv[2])
for k in sorted(set(a) | set(b)):
    print(f"     {k:<22} {a.get(k, 0):>3} | {b.get(k, 0):>3}")
EOF

echo "4. throw-away Home Assistant: same image, restored config, the git YAMLs mounted read-only as in production, NO network"
docker run -d --name $NAME --network none --memory 1536m -e TZ=Europe/Prague \
    -v "$CFG":/config \
    -v "$GIT/configuration.yaml":/config/configuration.yaml:ro \
    -v "$GIT/automations.yaml":/config/automations.yaml:ro \
    -v "$GIT/scripts.yaml":/config/scripts.yaml:ro "$IMG" >/dev/null
echo "   network mode: $(docker inspect $NAME --format '{{.HostConfig.NetworkMode}}'), published ports: '$(docker port $NAME | tr '\n' ' ')'"
T1=$(date +%s); UP=no
for i in $(seq 1 90); do
    code=$(docker exec $NAME python3 -c "
import urllib.request, urllib.error
try: urllib.request.urlopen('http://127.0.0.1:8123/api/', timeout=3); print(200)
except urllib.error.HTTPError as e: print(e.code)
except Exception: print(0)" 2>/dev/null || echo 0)
    [ "$code" = 401 ] && { UP=yes; break; }
    sleep 3
done
echo "   API answered 401 (alive, login required) after $(( $(date +%s) - T1 )) s: $UP"
echo "   isolation check from inside the scratch container (all must fail):"
docker exec $NAME python3 -c "
import socket
for host, port, what in (('192.168.31.240', 6668, 'a Tuya plug'), ('192.168.31.41', 54321, 'a Xiaomi purifier'), ('1.1.1.1', 53, 'the internet')):
    s = socket.socket(); s.settimeout(3)
    try: s.connect((host, port)); print('     REACHED', what, '<-- BAD')
    except OSError as e: print('     cannot reach', what, '-', e.strerror or type(e).__name__)
    finally: s.close()"

echo "5. what the restored instance loaded"
docker exec $NAME python -m homeassistant --script check_config -c /config >/tmp/ha-restore-cc.txt 2>&1; echo "   check_config exit code: $?  (0 = automations, scripts, integrations valid)"; rm -f /tmp/ha-restore-cc.txt
echo "   custom integrations found:"; docker logs $NAME 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -oE "custom integration [a-z_]+" | sort -u | sed 's/^/     /'
count_auto() { python3 -c "
import sqlite3, sys
try:
    c = sqlite3.connect('file:%s/home-assistant_v2.db?mode=ro' % sys.argv[1], uri=True)
    print(c.execute(\"select count(*) from states_meta where entity_id like 'automation.%'\").fetchone()[0])
except Exception:
    print(0)" "$CFG"; }
echo "   waiting for the scratch instance to write its automation states (up to 300 s; without a network it waits out integration timeouts) ..."
W0=$(date +%s); NA=0
for i in $(seq 1 30); do
    NA=$(count_auto); [ "$NA" -ge 15 ] && break
    sleep 10
done
echo "   automation entities with a state after $(( $(date +%s) - W0 )) s: $NA"
[ "$NA" -ge 15 ] || { echo "   NOT READY - last warnings of the scratch instance:"; docker logs $NAME 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | grep -E "WARNING|ERROR" | tail -8 | cut -c1-200 | sed 's/^/     /'; }
sleep 40
python3 - "$CFG" "$LIVE" <<'EOF'
import sqlite3, sys, collections
def ents(path):
    c = sqlite3.connect(f"file:{path}/home-assistant_v2.db?mode=ro", uri=True)
    return {r[0]: r[1] for r in c.execute("""SELECT m.entity_id, (SELECT s.state FROM states s WHERE s.metadata_id=m.metadata_id ORDER BY s.state_id DESC LIMIT 1)
                                            FROM states_meta m""")}
r, l = ents(sys.argv[1]), ents(sys.argv[2])
print("   entities with a state, by domain: restored | live")
dr, dl = collections.Counter(k.split(".")[0] for k in r), collections.Counter(k.split(".")[0] for k in l)
for k in ("automation", "script", "switch", "fan", "vacuum", "select", "sensor", "button", "number", "binary_sensor"):
    print(f"     {k:<14} {dr.get(k, 0):>4} | {dl.get(k, 0):>4}")
for dom in ("automation", "script"):
    a = {k for k in r if k.startswith(dom + ".")}; b = {k for k in l if k.startswith(dom + ".")}
    print(f"   {dom} ids identical to live: {a == b}  (only restored: {sorted(a - b)}, only live: {sorted(b - a)})")
for e in ("switch.bathroom_towels", "switch.living_room_rack", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "fan.zhimi_cpa4_9832_air_purifier"):
    print(f"   {e}: restored instance = {r.get(e)} (unavailable is expected: it has no network)")
EOF
echo "   one lighting automation, read from the restored YAML: $(grep -c 'sched_plant_lights' "$GIT/automations.yaml") plant-light schedule blocks"

if [ "${1:-}" = r2 ]; then
    echo "6. off-site copy: the same paths in the R2 repository (listing + two small files restored)"
    (
    set -a; . /srv/compose/_system/restic-r2.env; set +a
    restic snapshots --tag set --compact | grep -E "^[0-9a-f]{8} " | tail -n 1
    echo "   files under /srv/apps/homeassistant in the latest R2 snapshot: $(restic ls --recursive latest --tag set /srv/apps/homeassistant 2>/dev/null | grep -c '^/')"
    mkdir -m 700 "$WORK/r2"
    restic restore latest --tag set --target "$WORK/r2" --include /srv/apps/homeassistant/.storage/core.config_entries --include /srv/apps/homeassistant/custom_components/tuya_local/manifest.json 2>&1 | tail -1
    python3 - "$WORK/r2$LIVE" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1] + "/.storage/core.config_entries"))
m = json.load(open(sys.argv[1] + "/custom_components/tuya_local/manifest.json"))
print("   from R2: core.config_entries parses,", len(d["data"]["entries"]), "entries; tuya_local manifest version", m.get("version"))
EOF
    )
fi
echo "done; the scratch container and the restored copy (it holds device keys) are removed now"
