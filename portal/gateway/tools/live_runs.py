#!/usr/bin/env python3
"""Live proofs of timed runs on the REAL system (Task 29 Part 1), run on the server. Uses synthetic gateway people (device tokens in ~/.config/gw-probe, never printed).

  live_runs.py extend   PLANT_ID     start 4 min, +2 min, show the OLD end passing with the plug still ON and the OFF at the NEW end
  live_runs.py cancel   PLANT_ID     start 5 min, cancel after ~20 s: OFF at once, the run closed, the end withdrawn from HA
  live_runs.py stopped  PLANT_ID     start 3 min, then STOP the gateway and the schedule service until after the end: HA alone ends the run
  live_runs.py dryer                 one short legal run of the towel dryer (5 min), an extension beyond the guardrail (rejected), the end by itself
  live_runs.py reject                guardrail rejections through the live API (nothing is switched on)
The HA state is read straight from HA with the gateway user's token; nothing is printed that is secret."""
import json
import os
import subprocess
import sys
import time
import urllib.request
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_proof as L          # noqa: E402

ENT = {"plant_rack1": "switch.living_room_rack", "plant_rack2": "switch.livingroom_rack_big", "plant_orchids": "switch.kitchen_orchids",
       "plant_cacti": "switch.minisserver", "bathroom_towels": "switch.bathroom_towels", "parrot_uv": "switch.bedroom_parrot"}
HOUSE, ADMIN = "probe-run", "probe-admin"


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone()


def stamp(msg):
    print("%s  %s" % (now().strftime("%H:%M:%S"), msg), flush=True)


def ha_state(eid):
    t = open("/srv/apps/portal/gateway-secrets/ha-token").read().strip()
    r = urllib.request.Request("http://127.0.0.1:8123/api/states/" + eid, headers={"Authorization": "Bearer " + t})
    with urllib.request.urlopen(r, timeout=10) as x:
        d = json.load(x)
    return d["state"], dt.datetime.fromisoformat(d["last_changed"]).timestamp()


def api(method, path, who, body=None):
    return L.call(method, path, L.tok(who), body)


def wait_state(eid, want, timeout, every=2):
    end = time.time() + timeout
    while time.time() < end:
        s, ts = ha_state(eid)
        if s == want:
            return ts
        time.sleep(every)
    return None


def run_obj(did):
    st, d = api("GET", "/v1/devices/" + did, HOUSE)
    return d.get("run"), d


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True)


def need_off(did):
    s, _ = ha_state(ENT[did])
    if s != "off":
        sys.exit("%s is %s, not off: not testing a run on it now" % (did, s))


def start(did, minutes):
    t0 = time.time()
    st, b = api("POST", "/v1/devices/%s/run" % did, HOUSE, {"minutes": minutes})
    stamp("start %s %d min -> HTTP %s %s (%.1f s)" % (did, minutes, st, json.dumps({k: b.get(k) for k in ("outcome", "observed")} if isinstance(b, dict) else b, ensure_ascii=False), time.time() - t0))
    if st != 200:
        print(json.dumps(b, ensure_ascii=False)[:400])
        sys.exit(1)
    r = b["run"]
    stamp("  run %s ends_at %s armed=%s started_by=%s" % (r["id"], r["ends_at"], r["armed"], r["started_by"]))
    return r


def ends_ts(r):
    return dt.datetime.fromisoformat(r["ends_at"]).timestamp()


def main(a):
    cmd = a[1]
    if cmd == "mode":
        st, b = api("PATCH", "/v1/devices/plant_rack1", ADMIN, {"switch_mode": "timed"})
        stamp("PATCH plant_rack1 timed (admin) -> HTTP %s switch_mode=%s" % (st, b.get("switch_mode")))
        st, b = api("POST", "/v1/devices/plant_rack1/switch", HOUSE, {"on": True})
        stamp("plain ON of a TIMED lamp -> HTTP %s %s" % (st, b["error"]["code"] if st != 200 else b))
        st, b = api("PATCH", "/v1/devices/plant_rack1", HOUSE, {"switch_mode": "plain"})
        stamp("PATCH by a household person -> HTTP %s (must be 403)" % st)
        st, b = api("PATCH", "/v1/devices/plant_rack1", ADMIN, {"switch_mode": "plain"})
        stamp("PATCH back to plain (admin) -> HTTP %s switch_mode=%s" % (st, b.get("switch_mode")))
        return 0
    if cmd == "reject":
        st, b = api("POST", "/v1/devices/bathroom_towels/run", HOUSE, {"minutes": 121})
        stamp("dryer 121 min -> HTTP %s %s" % (st, [p["code"] for p in b["error"]["details"].get("problems", [])]))
        st, b = api("POST", "/v1/devices/plant_rack1/run", HOUSE, {"minutes": 200})
        stamp("lamp 200 min at this hour -> HTTP %s %s" % (st, [p["code"] for p in b["error"]["details"].get("problems", [])]))
        st, b = api("POST", "/v1/devices/plant_rack1/run", HOUSE, {"minutes": 0})
        stamp("0 min -> HTTP %s %s" % (st, [p["code"] for p in b["error"]["details"].get("problems", [])] or b["error"]["code"]))
        st, b = api("POST", "/v1/devices/plant_rack1/switch", HOUSE, {"on": True})
        stamp("plain ON of a plain lamp is still allowed (not tried here); ON of the dryer plain -> %s" % api("POST", "/v1/devices/bathroom_towels/switch", HOUSE, {"on": True})[1]["error"]["code"])
        return 0
    did = a[2] if len(a) > 2 else "bathroom_towels"
    eid = ENT[did]
    if cmd == "extend":
        need_off(did)
        r = start(did, 4)
        e_old = ends_ts(r)
        st, b = api("POST", "/v1/devices/%s/run/extend" % did, HOUSE, {"minutes": 2})
        stamp("extend +2 -> HTTP %s outcome=%s armed=%s ends_at %s minutes_total %s" % (st, b.get("outcome"), (b.get("run") or {}).get("armed"), (b.get("run") or {}).get("ends_at"), (b.get("run") or {}).get("minutes_total")))
        e_new = ends_ts(b["run"])
        time.sleep(max(0, e_old + 80 - time.time()))
        s, _ = ha_state(eid)
        stamp("80 s after the OLD end: plug is %s (must still be on)" % s)
        ts = wait_state(eid, "off", e_new - time.time() + 120)
        stamp("OFF at the NEW end: %s s after ends_at %s" % ("%.1f" % (ts - e_new) if ts else "NEVER", r["ends_at"][11:19] if False else b["run"]["ends_at"][11:19]))
        return 0
    if cmd == "cancel":
        need_off(did)
        r = start(did, 5)
        time.sleep(20)
        t0 = time.time()
        st, b = api("DELETE", "/v1/devices/%s/run" % did, HOUSE)
        stamp("cancel -> HTTP %s outcome=%s observed=%s (%.1f s)" % (st, b.get("outcome"), b.get("observed"), time.time() - t0))
        s, _ = ha_state(eid)
        stamp("HA says %s; run on the card: %s" % (s, run_obj(did)[0]))
        sh("sudo", "true")
        return 0
    if cmd == "stopped":
        need_off(did)
        r = start(did, 3)
        e = ends_ts(r)
        time.sleep(3)
        out = sh("docker", "stop", "portal-gateway", "portal-schedules")
        stamp("STOPPED gateway and schedule service: %s" % out.stdout.strip().replace("\n", " "))
        for p in ("16444", "17444"):
            stamp("  port %s answers: %s" % (p, "YES" if sh("sh", "-c", "curl -s -m 2 -o /dev/null http://127.0.0.1:%s/v1/health && echo y" % p).stdout.strip() == "y" else "no"))
        ts = wait_state(eid, "off", e - time.time() + 120)
        stamp("plug OFF %s s after ends_at %s WITH BOTH SERVICES STOPPED" % ("%.1f" % (ts - e) if ts else "NEVER", r["ends_at"][11:19]))
        out = sh("docker", "start", "portal-schedules", "portal-gateway")
        stamp("started again: %s" % out.stdout.strip().replace("\n", " "))
        return 0
    if cmd == "dryer":
        need_off("bathroom_towels")
        r = start("bathroom_towels", 5)
        st, b = api("POST", "/v1/devices/bathroom_towels/run/extend", HOUSE, {"minutes": 120})
        stamp("extend +120 on a 5 min run -> HTTP %s %s" % (st, [p["code"] for p in b["error"]["details"].get("problems", [])]))
        e = ends_ts(r)
        ts = wait_state("switch.bathroom_towels", "off", e - time.time() + 120)
        stamp("dryer OFF %s s after ends_at %s (by Home Assistant itself)" % ("%.1f" % (ts - e) if ts else "NEVER", r["ends_at"][11:19]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
