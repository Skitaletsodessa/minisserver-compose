#!/bin/bash
# After an update or a reboot of minisserver: one read-only pass over everything the project rules say must be looked at.
# Run:  /srv/compose/_system/post-update-check/post-update-check.sh      (needs sudo for the kernel log and sockets; never with -x)
# It changes nothing. Every line is OK / WARN / FAIL; the exit status is the number of FAIL lines.
fails=0
ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; fails=$((fails + 1)); }

echo "== system"
echo "  uptime: $(uptime -p) | kernel: $(uname -r) | newest installed: $(dpkg -l 'linux-image-6*' 2>/dev/null | awk '/^ii/{print $2}' | sed 's/linux-image-//' | sort -V | tail -1)"
[ -e /var/run/reboot-required ] && warn "reboot is still required" || ok "no reboot pending"
n=$(systemctl --failed --no-legend 2>/dev/null | wc -l); [ "$n" = 0 ] && ok "no failed units" || { fail "$n failed units:"; systemctl --failed --no-legend | cut -c1-110; }
for m in /srv/apps /srv/data /srv/library /srv/vault; do mountpoint -q "$m" && ok "mounted: $m" || fail "NOT mounted: $m"; done

echo "== kernel log of this boot: disk errors (counted per ata port / device, never per /dev/sdX)"
e=$(sudo journalctl -k -b --no-pager 2>/dev/null | grep -E "ata[0-9]+(\.[0-9]+)?: (exception|failed command|error:|hard resetting)|I/O error|nvme.*(error|timeout|reset)" | wc -l)
[ "$e" = 0 ] && ok "0 disk errors in the kernel log" || { fail "$e disk error lines:"; sudo journalctl -k -b --no-pager | grep -E "ata[0-9]+(\.[0-9]+)?: (exception|failed command|error:|hard resetting)|I/O error|nvme.*(error|timeout|reset)" | head -8 | cut -c1-170; }

echo "== containers"
bad=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -v -E "Up .*\(healthy\)|Up [0-9]+ (seconds|minutes|hours|days|weeks)( |$)" | grep -v -E "^relay-test" )
[ -z "$bad" ] && ok "every container is up" || { fail "containers not up:"; echo "$bad" | cut -c1-120; }
unh=$(docker ps --format '{{.Names}} {{.Status}}' | grep -i -E "unhealthy|starting" | cut -c1-100)
[ -z "$unh" ] && ok "no unhealthy / still-starting container" || { warn "check again in a minute:"; echo "$unh"; }
docker ps --format '{{.Names}}' | wc -l | xargs -I{} echo "  containers running: {}"

echo "== network exposure"
ha=$(ss -tln | awk '$4 ~ /:8123$/ {print $4}' | tr '\n' ' ')
[ "$ha" = "127.0.0.1:8123 " ] && ok "HA listens on 127.0.0.1:8123 only" || fail "HA listens on: $ha"
[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://192.168.31.2:8123/ || true)" = "000" ] && ok "HA not reachable on the LAN address" || fail "HA answers on the LAN address"
/srv/compose/portal/gateway/check-bind.sh 2>&1 | tail -1 | sed 's/^/  /'
sudo systemctl start listener-watch.service 2>/dev/null; sleep 4
a=$(python3 -c "import json;d=json.load(open('/var/lib/watch-alerts/listener-watch.json'));print(len(d.get('alerted') or []), len(d.get('missing') or []))" 2>/dev/null)
[ "$a" = "0 0" ] && ok "listener-watch: no unexpected and no missing listener" || warn "listener-watch (unexpected missing): $a"

echo "== name service, tailnet, public doors"
r=$(dig +short +time=3 +tries=1 @192.168.31.2 example.com | head -1); [ -n "$r" ] && ok "AdGuard (server) answers: $r" || fail "AdGuard (server) gives no answer"
b=$(dig +short +time=3 +tries=1 @192.168.31.2 doubleclick.net | head -1); [ "$b" = "0.0.0.0" ] && ok "a blocked name is sinkholed" || warn "blocked name answered: $b"
r=$(dig +short +time=3 +tries=1 @192.168.31.5 example.com | head -1); [ -n "$r" ] && ok "AdGuard replica (Pi) answers" || warn "replica gives no answer"
tailscale status >/dev/null 2>&1 && ok "tailscale is up" || fail "tailscale is not up"
for u in "https://minisserver.tail6bf4d5.ts.net:10000/api/server/ping 200 Immich" "https://minisserver.tail6bf4d5.ts.net:8443/ping 200 Navidrome" "https://minisserver.tail6bf4d5.ts.net/app/update.json 401 portal-door"; do
  set -- $u; c=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$1" || true); [ "$c" = "$2" ] && ok "$3 public door $c" || fail "$3 public door answered $c (expected $2)"
done
c=$(curl -s -o /dev/null -m 15 -w '%{http_code}' https://relay.ivandeliver.email/ || true); [ "$c" = "403" ] && ok "relay door 403 (Cloudflare Access)" || warn "relay door answered $c"

echo "== home assistant and the schedules"
python3 - <<'PY'
import json, urllib.request
TOK = open("/srv/apps/portal/gateway-secrets/ha-token").read().strip()
try:
    st = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/states", headers={"Authorization": "Bearer " + TOK}), timeout=30).read())
except Exception as e:
    print("  FAIL  HA API: %s" % e); raise SystemExit(0)
by = {s["entity_id"]: s["state"] for s in st}
print("  OK    HA API answers, %d entities" % len(by))
for e in ("vacuum.eta_tiger", "vacuum.xiaomi_c102gl_cadb_robot_cleaner", "vacuum.xiaomi_ov51gl_e160_robot_cleaner", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner"):
    print("  %-5s %s = %s" % ("OK" if by.get(e) not in (None, "unavailable", "unknown") else "WARN", e, by.get(e)))
off = [e for e in ("switch.bathroom_towels", "switch.bedroom_parrot", "switch.living_room_rack", "switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver", "switch.citrus_2nd_floor") if by.get(e) in (None, "unavailable")]
print("  %-5s plugs unavailable: %s" % ("OK" if not off else "WARN", off or "none"))
PY
(cd /srv/compose/portal/schedules/applier && timeout 200 python3 portal_apply.py 2>&1 | tail -1 | sed 's/^/  applier: /')

echo "== alerts that are active right now (tier A first)"
python3 - <<'PY'
import glob, json, time
rows = []
for f in glob.glob("/var/lib/watch-alerts/*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if isinstance(d, dict) and d.get("text"):
        rows.append((d.get("tier", "A"), time.strftime("%m-%d %H:%M", time.localtime(d.get("since", 0))), d["text"].split("\n")[0][:110]))
for t, s, x in sorted(rows):
    print("  [%s] %s %s" % (t, s, x))
print("  (%d active)" % len(rows))
PY
echo; [ "$fails" = 0 ] && echo "RESULT: no FAIL" || echo "RESULT: $fails FAIL"
exit "$fails"
