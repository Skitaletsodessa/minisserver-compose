#!/usr/bin/env python3
"""Rehearsal of the Task 31 Part A change on a COPY of the live data (nothing live is touched: the copy is a sqlite .backup in a temp dir, nothing talks to AdGuard).

Runs the OLD code (the schedule service as deployed before Task 31) and the NEW code in separate processes against the same copy:
  1. the copy publishes the same snapshot as the live one (the copy is faithful), plus a SCRATCH child (192.168.31.198) with the old default windows (a maps rule)
     and an active `maps` exception, so the rollback has something to give back;
  2. migrate (NEW code); the migrated snapshot passes the invariants in BOTH code versions and BOTH render the very same block from it (deploy-order safety:
     the host job imports the code from the repository, the service runs from an image - for a while they can be different versions);
  3. the block, before vs after, over a week of sample times: nothing that was allowed became blocked; the only lines added are the Maps lines, and only on Sunday;
  4. the Maps domains are in the block of every child on every day and hour;
  5. rollback (NEW code, data only) + OLD code publishing again gives back the EXACT snapshot checksum from step 1.
Prints PASS/FAIL per check and writes the Sunday block diff to --out.

  python3 maps_rehearsal.py [--out FILE]      OLD_SRC (default /srv/compose/portal/schedules), NEW_SRC, ADG_SRC (the job code)"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile

PY = os.environ.get("REHEARSAL_PY", "/srv/apps/portal/venv-test/bin/python")
OLD = os.environ.get("OLD_SRC", "/srv/compose/portal/schedules")
NEW = os.environ.get("NEW_SRC", "/tmp/t31/sched31")
ADG = os.environ.get("ADG_SRC", "/srv/compose/adguard")
LIVE_DB = os.environ.get("LIVE_DB", "/srv/apps/portal/schedules/schedules.db")
LIVE_SNAP = os.environ.get("LIVE_SNAP", "/srv/apps/portal/schedules/out/child-policy.json")
SD = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com", "ytimg.com"], "roblox": ["roblox.com"]}
SYS = {"token": "-", "name": "rehearsal", "role": "admin"}
SCRATCH_IP = "192.168.31.198"
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


# ── worker side (runs under OLD or NEW code, chosen by PYTHONPATH) ──────────────────────────────────────────────────────────────────────────────────────
def sample_times():
    from sched import model
    out = []
    for d in range(7):
        for h in (0, 8, 12, 15, 16, 18, 19, 23):
            for m in (0, 30):
                out.append(dt.datetime(2026, 10, 19 + d, h, m, tzinfo=model.TZ))
    return out


def worker(argv):
    cmd, data = argv[0], argv[1]
    sys.path.insert(0, ADG)
    from sched import store, child_invariants
    if cmd in ("publish", "add_scratch", "migrate", "rollback"):
        s = store.Store(data)
        if cmd == "add_scratch":
            s.add_child_device(SYS, "Scratch", SCRATCH_IP, label="rehearsal")
            s.add_child_exception(SYS, "child_scratch_rehearsal", "maps", minutes=30)
            print(json.dumps({"rules": [r["id"] for r in s.schedule("child_scratch_rehearsal")["rules"]],
                              "exceptions": [x["target"] for x in s.child_exceptions("child_scratch_rehearsal")]}))
            return
        if cmd == "migrate":
            print(json.dumps(s.migrate_maps_always_open()))
            return
        if cmd == "rollback":
            print(json.dumps(s.rollback_maps_always_open()))
            return
        s.publish()
        snap = json.load(open(os.path.join(data, "out", "child-policy.json")))
        import hashlib
        core = {k: v for k, v in snap.items() if k not in ("generated_at", "audit_seq", "checksum")}
        core["devices"] = [{k: v for k, v in d.items() if k != "version"} for d in core["devices"]]        # the schedule version only ever counts up (also across a rollback)
        norm = hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()
        print(json.dumps({"checksum": snap["checksum"], "norm": norm, "error": s.child_publish_error}))
        return
    if cmd == "render":            # data = a snapshot file
        import childsnapshot
        snap = json.load(open(data))
        probs = child_invariants.verify_snapshot(snap)
        blocks = {t.isoformat(): childsnapshot.render(SD, snap, t, overrides=[])[0] for t in sample_times()}
        print(json.dumps({"problems": probs, "blocks": blocks, "checksum": snap["checksum"]}))
        return
    raise SystemExit("unknown worker command %r" % cmd)


# ── driver ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def run(code, *args):
    env = {**os.environ, "PYTHONPATH": code, "PORTAL_SRC": code, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([PY, os.path.abspath(__file__), "--worker", *args], env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise SystemExit("worker %s failed under %s:\n%s" % (args[0], code, r.stderr[-1500:]))
    return json.loads(r.stdout.strip().splitlines()[-1])


def main():
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    tmp = tempfile.mkdtemp(prefix="maps-rehearsal-")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    subprocess.run(["sqlite3", LIVE_DB, ".backup '%s'" % os.path.join(data, "schedules.db")], check=True, timeout=60)
    live = json.load(open(LIVE_SNAP))

    s0 = run(OLD, "publish", data)
    check("the copy is faithful: OLD code publishes the live snapshot's checksum from it", s0["checksum"] == live["checksum"], "%s vs %s" % (s0["checksum"][:12], live["checksum"][:12]))
    print("   live devices: %s" % [d["name"] for d in live["devices"]])
    sc = run(OLD, "add_scratch", data)
    check("scratch child added with the OLD defaults (a maps rule) and an active maps exception", "maps" in sc["rules"] and sc["exceptions"] == ["maps"], str(sc))
    before_snap = os.path.join(tmp, "before.json")
    open(before_snap, "w").write(open(os.path.join(data, "out", "child-policy.json")).read())
    before = run(OLD, "render", before_snap)
    check("before: OLD code renders the old snapshot without invariant problems", before["problems"] == [], str(before["problems"])[:200])
    s_before = run(OLD, "publish", data)

    m = run(NEW, "migrate", data)
    check("NEW code migrates: every device's maps rule removed, the maps exception cancelled", m and len(m["rules_changed"]) == len(live["devices"]) + 1 and m["exceptions_cancelled"] == 1, str(m))
    again = run(NEW, "migrate", data)
    check("a second run is a no-op (idempotent)", again is None)
    s_after = run(NEW, "publish", data)
    check("NEW code publishes the migrated snapshot (no invariant error)", s_after["error"] is None, str(s_after))
    after_snap = os.path.join(tmp, "after.json")
    open(after_snap, "w").write(open(os.path.join(data, "out", "child-policy.json")).read())
    snap = json.load(open(after_snap))
    check("the migrated snapshot has no maps rule or exception and an empty allow_weekdays, Maps in allow_always",
          '"maps"' not in json.dumps(snap["devices"]) and snap["lists"]["allow_weekdays"] == [] and {"maps.google.com", "maps.googleapis.com", "maps.gstatic.com"} <= set(snap["lists"]["allow_always"]))

    after_new = run(NEW, "render", after_snap)
    after_old = run(OLD, "render", after_snap)
    check("deploy-order safety: the OLD code (host job before its code is updated) accepts the migrated snapshot", after_old["problems"] == [], str(after_old["problems"])[:200])
    check("... and renders exactly the block the NEW code renders from it (every sample time)", after_old["blocks"] == after_new["blocks"])

    maps = ("maps.google.com", "maps.googleapis.com", "maps.gstatic.com")
    ips = [d["ip"] for d in snap["devices"]]
    maps_lines = {"@@||%s^$client=%s" % (d, ip) for d in maps for ip in ips}
    sunday_diff, bad, removed_any, wrong_days = {}, 0, 0, 0
    for t, old_block in before["blocks"].items():
        new_block = after_new["blocks"][t]
        gone, added = set(old_block) - set(new_block), set(new_block) - set(old_block)
        if gone:
            removed_any += 1
        if not added <= maps_lines:
            bad += 1
        wd = dt.datetime.fromisoformat(t).isoweekday()
        if (wd == 7) != bool(added):
            wrong_days += 1
        if wd == 7 and not sunday_diff:
            sunday_diff = {"at": t, "added": sorted(added)}
        if not maps_lines <= set(new_block):
            bad += 1
    check("nothing that was allowed before is blocked after (all %d sample times)" % len(before["blocks"]), removed_any == 0)
    check("the only lines added are Maps lines", bad == 0)
    check("lines are added on Sundays only (Monday-Saturday the block is byte-identical to before)", wrong_days == 0)
    check("the Maps lines are in the block of every child (incl. the scratch child) at every sample time", bad == 0)

    rb = run(NEW, "rollback", data)
    check("rollback restores the scratch child's rules and its exception", rb and "child_scratch_rehearsal" in rb["rules_restored"] and rb["exceptions_revived"] == 1, str(rb))
    s_back = run(OLD, "publish", data)
    check("OLD code publishes after the rollback: the policy is EXACTLY the one from before the migration (rules, lists, exceptions; only the schedule version numbers moved on - they only count up)",
          s_back["norm"] == s_before["norm"], "%s vs %s" % (s_back["norm"][:12], s_before["norm"][:12]))
    back_snap = os.path.join(tmp, "back.json")
    open(back_snap, "w").write(open(os.path.join(data, "out", "child-policy.json")).read())
    back = run(OLD, "render", back_snap)
    check("... and it renders the very same block as before at every sample time", back["blocks"] == before["blocks"])

    if out_path:
        with open(out_path, "w") as f:
            f.write("Sunday %s: lines added by the change (all other sample times: none)\n" % sunday_diff.get("at", "?")[:10])
            for l in sunday_diff.get("added", []):
                f.write("+ " + l + "\n")
    print("scratch dir: %s" % tmp)
    print("RESULT: %s" % ("all checks passed" if not FAILS else "FAILED: %s" % FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker(sys.argv[sys.argv.index("--worker") + 1:])
    else:
        sys.exit(main())
