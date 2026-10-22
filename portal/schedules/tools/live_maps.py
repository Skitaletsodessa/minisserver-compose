#!/usr/bin/env python3
"""LIVE application of Task 31 Part A on the real house, with an automatic revert (Ivan 2026-10-22: Maps always open, Sundays included).  Run on the server as skit.

What it does, in order (every step is printed with a time; a failed check after the service has been replaced triggers the REVERT):
  0  pre-flight: services healthy, parity green, the managed block and the 4 real children's lines recorded (origin AND replica), check_host answers for the
     reference names x every real child recorded, a backup of the data (sqlite .backup + audit.jsonl + the snapshot) in /srv/apps/portal/schedules/migrations/
  1  a DNS load (>= 2000 lookups per resolver, ordinary and unique names) starts and keeps running to the end
  2  the NEW image is built from the staged tree and replaces portal-schedules (the old image is kept as portal-schedules:pre-maps); on start the service runs
     the audited migration and publishes the new snapshot
  3  the host job applies it (<= 75 s); the block is compared: nothing allowed before may be blocked, only Maps lines may be added (Mon-Sat: none at all)
  4  replica identical, parity run green, policy-drift-watch run quiet, check_host: the Maps domains allowed for every real child, every other reference name
     answers exactly as before
  5  the load stops: 0 failures, >= 2000 lookups per resolver
REVERT (any failure from step 2 on): service stopped, data rolled back (new code, data only), old image restored and started (it republishes the old snapshot),
the job applies it, the block must equal the recorded one again.

  live_maps.py [--stage /tmp/t31/sched31] [--dry-run]       --dry-run stops after the pre-flight (records, writes nothing, starts nothing)"""
import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dnsstress  # noqa: E402

STAGE = sys.argv[sys.argv.index("--stage") + 1] if "--stage" in sys.argv else "/tmp/t31/sched31"
DRY = "--dry-run" in sys.argv
DATA = "/srv/apps/portal/schedules"
COMPOSE = "/srv/compose/portal/schedules/compose.yaml"
REAL = ["192.168.31.60", "192.168.31.61", "192.168.31.30"]
MAPS = ["maps.google.com", "maps.googleapis.com", "maps.gstatic.com"]
# a name that only Google Maps' tiles are likely to need, listed so the report shows they are NOT opened by this change (they answer exactly as before)
MAPS_CANDIDATES = ["mt0.google.com", "khms0.google.com", "streetviewpixels-pa.googleapis.com", "clients4.google.com"]
REFERENCE = ["youtube.com", "www.youtube.com", "roblox.com", "www.roblox.com", "api.telegram.org", "t.me", "telegram.org", "api.accuweather.com", "accounts.google.com",
             "www.tiktok.com", "www.google.com", "connectivitycheck.gstatic.com", "play.googleapis.com", "mtalk.google.com", "fcm.googleapis.com", "time.android.com",
             "doubleclick.net", "example.com", "never-allowed.example.net", "facebook.com", "instagram.com", "discord.com", "twitch.tv", "www.wikipedia.org"]
SERVERS = ["192.168.31.2", "192.168.31.5"]
FAILS = []
STATE = {"replaced": False}


def stamp(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def check(name, cond, detail=""):
    stamp("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def sh(cmd, timeout=600):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def agh(path):
    pw = open("/home/skit/adguard-admin-password.txt").read().strip()
    req = urllib.request.Request("http://127.0.0.1:3000/control" + path, headers={"Authorization": "Basic " + base64.b64encode(("admin:" + pw).encode()).decode()})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def replica(path):
    env = dict(l.strip().split("=", 1) for l in open("/srv/compose/adguard-sync/.env") if "=" in l and not l.startswith("#"))
    pw = env["REPLICA1_PASSWORD"].strip("\"'")
    req = urllib.request.Request("https://piserver.tail6bf4d5.ts.net/control" + path, headers={"Authorization": "Basic " + base64.b64encode(("admin:" + pw).encode()).decode()})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def svc(path):
    tok = open(os.path.expanduser("~/.config/schedctl/token")).read().strip()
    req = urllib.request.Request("http://127.0.0.1:16444" + path, headers={"Authorization": "Bearer " + tok})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())


def block(inst):
    rules = (agh if inst == "origin" else replica)("/filtering/status").get("user_rules") or []
    if "! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)" in rules:
        b = rules.index("! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)")
        e = rules.index("! END child-policy")
        return rules[b:e + 1]
    return []


def blk_hash(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def verdict(inst, name, ip):
    r = (agh if inst == "origin" else replica)("/filtering/check_host?name=%s&client=%s" % (urllib.parse.quote(name), ip))
    return "blocked" if str(r.get("reason", "")).startswith("Filtered") else "allowed"


def verdicts(inst):
    return {"%s|%s" % (ip, n): verdict(inst, n, ip) for ip in REAL for n in REFERENCE + MAPS + MAPS_CANDIDATES}


def wait_for(cond, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:                                                    # noqa: BLE001 - a service being restarted answers nothing for a moment
            pass
        time.sleep(step)
    return False


def child_lines(lines):
    return [l for l in lines if any(l.endswith("$client=" + ip) for ip in REAL)]


def applied_checksum():
    try:
        return json.load(open(DATA + "/child-apply-status.json")).get("snapshot_checksum")
    except (OSError, ValueError):
        return None


def snap_checksum():
    return json.load(open(DATA + "/out/child-policy.json"))["checksum"]


def preflight():
    stamp("=== 0. pre-flight ===")
    h = svc("/v1/health")
    check("schedule service healthy, audit chain ok, snapshot in sync with what the job applied", h["status"] == "ok" and h["audit_chain"]["ok"] and h.get("in_sync") is True, str(h)[:300])
    st = json.load(open(DATA + "/child-apply-status.json"))
    check("the host job applies from the snapshot and its last run was ok", st["source"] == "snapshot" and st["ok"] and st["snapshot_checksum"] == snap_checksum(), str(st)[:200])
    ob, rb = block("origin"), block("replica")
    check("replica block equals origin block now", ob == rb and len(ob) > 100, "%d vs %d lines" % (len(ob), len(rb)))
    rc, out, err = sh("sudo systemctl start adguard-parity.service")                 # a oneshot: start waits for it and returns its exit status
    check("a parity run now is green", rc == 0, err[-200:])
    return ob, snap_checksum()


def revert(why, before_block, before_verdicts, backup_dir):
    stamp("!!! REVERT: %s" % why)
    sh("docker stop portal-schedules", 120)
    rc, out, err = sh("docker run --rm -u 1000:1000 -v %s:/data -e PORTAL_DATA=/data -e PYTHONDONTWRITEBYTECODE=1 portal-schedules:local python -c "
                      "\"from sched import store; s=store.Store('/data'); print(s.rollback_maps_always_open())\"" % DATA, 120)
    stamp("data rollback (new code, data only): rc=%s %s %s" % (rc, out[-300:], err[-300:]))
    sh("docker tag portal-schedules:pre-maps portal-schedules:local")
    rc, out, err = sh("docker compose -f %s up -d --force-recreate portal-schedules" % COMPOSE, 300)
    stamp("old image restored and started: rc=%s %s" % (rc, err[-200:]))
    ok = wait_for(lambda: svc("/v1/health")["status"] == "ok", 90)
    ok2 = wait_for(lambda: applied_checksum() == snap_checksum(), 150)
    ok3 = wait_for(lambda: blk_hash(block("origin")) == blk_hash(before_block), 90)
    stamp("revert result: service ok=%s, job applied the restored snapshot=%s, the block equals the recorded one=%s" % (ok, ok2, ok3))
    stamp("backup of the data before the change: %s" % backup_dir)
    return ok and ok2 and ok3


def main():
    ob, snap0 = preflight()
    v_before_o = verdicts("origin")
    v_before_r = verdicts("replica")
    wd = dt.datetime.now().isoweekday()
    stamp("recorded: block %s (%d lines), %d verdicts per instance; today is weekday %d (%s)" % (blk_hash(ob), len(ob), len(v_before_o), wd, "Sunday" if wd == 7 else "Mon-Sat: Maps is already open today, so the block must come out byte-identical"))
    stamp("Maps domains today, per real child (origin): %s" % sorted({v_before_o["%s|%s" % (ip, d)] for ip in REAL for d in MAPS}))
    stamp("reference (non-Maps) names allowed today: %d of %d" % (sum(1 for ip in REAL for n in REFERENCE if v_before_o["%s|%s" % (ip, n)] == "allowed"), len(REAL) * len(REFERENCE)))
    stamp("Maps tile candidates today (NOT in this change): %s" % {n: v_before_o["%s|%s" % (REAL[0], n)] for n in MAPS_CANDIDATES})
    if DRY:
        stamp("--dry-run: stopping here (nothing written, nothing started)")
        return 0

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = "%s/migrations/maps-pre-%s" % (DATA, ts)
    os.makedirs(backup, exist_ok=True)
    sh(["sqlite3", DATA + "/schedules.db", ".backup '%s/schedules.db'" % backup])
    for f in ("audit.jsonl", "out/child-policy.json"):
        sh(["cp", "-p", "%s/%s" % (DATA, f), backup + "/" + os.path.basename(f)])
    check("backup written (sqlite .backup, audit.jsonl, the snapshot)", os.path.exists(backup + "/schedules.db") and os.path.getsize(backup + "/schedules.db") > 50000, backup)

    stamp("=== 1. DNS load starts (kept running to the end) ===")
    stress = dnsstress.Stress(SERVERS, rate=14).start()

    stamp("=== 2. build the new image and replace the service ===")
    sh("docker tag portal-schedules:local portal-schedules:pre-maps")
    old_id = sh("docker image inspect portal-schedules:pre-maps --format '{{.Id}}'")[1]
    rc, out, err = sh("docker build -q -t portal-schedules:local %s" % STAGE, 900)
    if not check("image built from the staged tree", rc == 0, err[-300:]):
        sh("docker tag portal-schedules:pre-maps portal-schedules:local")
        stress.stop()
        return 1
    new_id = sh("docker image inspect portal-schedules:local --format '{{.Id}}'")[1]
    check("the new image differs from the old one", new_id != old_id)
    STATE["replaced"] = True
    rc, out, err = sh("docker compose -f %s up -d --force-recreate portal-schedules" % COMPOSE, 300)
    stamp("compose: rc=%s %s" % (rc, err[-200:]))
    t0 = time.time()
    ok = wait_for(lambda: svc("/v1/health")["status"] == "ok", 90)
    run_id = sh("docker inspect portal-schedules --format '{{.Image}}'")[1]
    good = check("service back ok on the NEW image", ok and run_id == new_id, "image %s" % run_id[:19])
    audit = svc("/v1/audit?limit=5")["audit"] if ok else []
    mig = next((a for a in audit if a["action"] == "child_maps_always_open"), None)
    good = check("the audited migration is on record (child_maps_always_open)", mig is not None, str([a["action"] for a in audit])) and good
    if mig:
        stamp("migration: rules changed on %s, exceptions cancelled %s" % (sorted(mig["before"]["rules"]), mig["before"]["exceptions"]))
    h = svc("/v1/health") if ok else {}
    good = check("health: audit chain ok, no publish error", ok and h.get("audit_chain", {}).get("ok") and h.get("publish_error") is None and h["child_policy"]["publish_error"] is None, str(h)[:200]) and good
    snap1 = snap_checksum()
    good = check("a new snapshot was published (checksum moved)", snap1 != snap0) and good
    if not good:
        revert("service step failed", ob, v_before_o, backup)
        stress.stop()
        return 1

    stamp("=== 3. the host job applies it ===")
    applied = wait_for(lambda: applied_checksum() == snap1, 150, 2)
    stamp("applied after %.0f s (service start to job)" % (time.time() - t0))
    ab = block("origin")
    gone, added = set(ob) - set(ab), set(ab) - set(ob)
    maps_lines = {"@@||%s^$client=%s" % (d, ip) for d in MAPS for ip in REAL}
    ok = check("job applied the new snapshot", applied)
    ok = check("nothing that was allowed before is blocked now (0 lines removed)", not gone, str(sorted(gone))[:300]) and ok
    ok = check("only Maps lines were added", added <= maps_lines, str(sorted(added - maps_lines))[:300]) and ok
    if wd != 7:
        ok = check("Monday-Saturday: the block is BYTE-IDENTICAL to before (hash %s)" % blk_hash(ob), ab == ob, "%d lines added" % len(added)) and ok
    else:
        ok = check("Sunday: exactly the 9 Maps lines were added", added == maps_lines, str(len(added))) and ok
    stamp("block hash before %s after %s; lines before %d after %d; added %d removed %d" % (blk_hash(ob), blk_hash(ab), len(ob), len(ab), len(added), len(gone)))
    if not ok:
        revert("block check failed", ob, v_before_o, backup)
        stress.stop()
        return 1

    stamp("=== 4. replica, parity, drift watch, check_host ===")
    rep = wait_for(lambda: block("replica") == block("origin"), 120)
    ok = check("replica block equals the origin block", rep)
    rc, out, err = sh("sudo systemctl start adguard-parity.service")
    ok = check("parity run green", rc == 0, err[-200:]) and ok
    rc, out, err = sh("sudo systemctl start policy-drift-watch.service")
    ok = check("policy-drift-watch run finished without failure (it keeps no fixed baseline: it renders the expected block from the snapshot with the same code, so the new arrangement IS its baseline)", rc == 0, err[-200:]) and ok
    v_after_o, v_after_r = verdicts("origin"), verdicts("replica")
    changed = {k: (v_before_o[k], v_after_o[k]) for k in v_before_o if v_before_o[k] != v_after_o[k]}
    ok = check("check_host answers: every reference name answers exactly as before for every real child, origin (%d verdicts)" % (len(REAL) * len(REFERENCE)),
               not {k for k in changed if k.split("|")[1] in REFERENCE}, str(changed)[:300]) and ok
    ok = check("the Maps candidates NOT added (mt0, khms0, streetview, clients4) answer exactly as before", not {k for k in changed if k.split("|")[1] in MAPS_CANDIDATES}, str(changed)[:300]) and ok
    ok = check("Maps domains allowed for every real child, origin", all(v_after_o["%s|%s" % (ip, d)] == "allowed" for ip in REAL for d in MAPS)) and ok
    ok = check("Maps domains allowed for every real child, replica", all(v_after_r["%s|%s" % (ip, d)] == "allowed" for ip in REAL for d in MAPS)) and ok
    ok = check("replica verdicts equal origin verdicts (all %d)" % len(v_after_o), v_after_o == v_after_r, str({k for k in v_after_o if v_after_o[k] != v_after_r[k]})[:200]) and ok
    ok = check("a name that must stay blocked is still blocked for every child", all(v_after_o["%s|never-allowed.example.net" % ip] == "blocked" for ip in REAL)) and ok
    if not ok:
        revert("verification failed", ob, v_before_o, backup)
        stress.stop()
        return 1

    stamp("=== 5. DNS load result ===")
    need = 2000
    t_end = time.time() + 240
    while time.time() < t_end and min(s["lookups"] for s in stress.snapshot().values()) < need:
        time.sleep(5)
    res = stress.stop()
    for s, v in res.items():
        stamp("resolver %s: %d lookups, %d failed, slowest %.2f s %s" % (s, v["lookups"], v["failed"], v["max_s"], v["failures"][:3]))
    ok = check(">= %d lookups per resolver" % need, all(v["lookups"] >= need for v in res.values()))
    ok = check("0 failed lookups on both resolvers during the whole change", all(v["failed"] == 0 for v in res.values())) and ok
    stamp("backup of the data before the change: %s ; old image kept as portal-schedules:pre-maps (%s)" % (backup, old_id[:19]))
    stamp("RESULT: %s" % ("all checks passed" if not FAILS else "FAILED: %s" % FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
