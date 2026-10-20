#!/usr/bin/env python3
"""Timed runs against a REAL Home Assistant (same pinned image, throw-away container, NO network, template switches backed by input_booleans), Task 29 Part 1.
Reuses the rig of integration_scratch_ha.py: the real hand-written automations/scripts of the repository, the real portal_apply.Applier (real check_config,
real webhook reload, verification through the scratch recorder). The schedule service is a Store object here - after a run is armed nothing touches it again,
which is exactly "the gateway and the schedule service are stopped".

  /srv/apps/portal/venv-test/bin/python tests/integration_runs.py          (run on the server; ~12 min)

Proves: (1) the end of a run switches the plug OFF within a minute of ends_at with the service and applier doing nothing; (2) HA stopped across the end ->
after start-up the plug is OFF by the reconcile, never ON because of the run; (3) HA restarted in the MIDDLE of a run: an ON plug stays ON until the end,
an OFF plug is never switched ON; (4) a cancelled run is withdrawn from HA and its end no longer acts; (5) an extension moves the end; (6) the fail-safe OFF
automation is still generated and untouched; (7) the expired run disappears from HA after the service closes it.
"""
import os
import sys
import time
import json
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import integration_scratch_ha as I       # noqa: E402
from sched import model, store as storemod   # noqa: E402

ACT = {"token": "-", "name": "int", "role": "admin", "acting": {"person": "int-person", "phone": "-"}}
DRYER = "bathroom_towels"
SW = "switch.bathroom_towels"


def on_ts_after(t0):
    for s, ts in I.state_history(SW):
        if s == "on" and ts >= t0:
            return ts
    return None


def off_ts_after(t0):
    for s, ts in I.state_history(SW):
        if s == "off" and ts >= t0:
            return ts
    return None


def ends(run):
    return dt.datetime.fromisoformat(run["ends_at"]).timestamp()


def wait_up(secs=240):
    return I.wait_for(lambda: I.dx("python3", "-c", "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://127.0.0.1:8123/api/', timeout=3)\n"
                                    "except e.HTTPError as x: print(x.code)\nexcept Exception: print(0)").stdout.strip() == "401", secs, 3)


def arm(st, rig, run):
    res = rig.apply()
    status = json.load(open(I.S + "/apply-status.json"))
    return res, storemod.Store.run_armed(st.run(run["id"]), status)


def main():
    print("scratch HA timed-run rehearsal; image %s" % I.sh("docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}").stdout.strip()[-20:])
    I.write_config()
    st = I.seed_store(None)
    rig = I.Rig(st)
    try:
        meta0 = json.load(open(st.out + "/render.json"))
        for rel in meta0["files"]:
            p = os.path.join(I.GEN, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            import shutil
            shutil.copyfile(os.path.join(st.out, "render", rel), p)
        up, secs = I.start_ha()
        I.check(up, "scratch HA starts (%.0f s)" % secs)
        if not up:
            print(I.sh("docker", "logs", "--tail", "40", I.NAME).stdout[-3000:])
            return
        I.check(I.wait_for(lambda: "Portal: reload generated schedules (webhook)" in rig.loaded()[0] and "reconcile_all_plugs" in rig.loaded()[1], 240) is True, "HA loaded everything")
        time.sleep(5)
        # the dryer: today's windows removed (skip_date), no gap, wide guardrails (the seed_store widens them); a run is for the gaps between windows
        today = dt.datetime.now(model.TZ).date().isoformat()
        st.add_exception(ACT, DRYER, {"kind": "skip_date", "date": today})
        st.patch_entity(ACT, DRYER, {"managed": True})
        I.check(rig.apply() == "applied", "dryer managed and applied")
        I.post_webhook("test-bathroom_towels-off")
        I.wait_for(lambda: I.last_state(SW) == "off", 30, 2)

        # ── 1. the end of a run, with the service stopped ──
        run = st.start_run(ACT, DRYER, 2)
        res, armed = arm(st, rig, run)
        I.check(res == "applied" and armed, "run accepted, rendered, applied and ARMED (HA has loaded its end)", res)
        autos, _s = rig.loaded()
        I.check(any("run end" in a and DRYER in a for a in autos), "the generated end-of-run automation is loaded in HA", str([a for a in autos if "run end" in a]))
        I.check(any("fail-safe" in a and DRYER in a for a in autos), "the fail-safe OFF automation is still loaded next to it")
        t_on = time.time()
        I.check(I.post_webhook("test-bathroom_towels-on"), "plug switched ON (the gateway's step)")
        I.wait_for(lambda: I.last_state(SW) == "on", 20, 2)
        # from here on nothing talks to the store or the applier: the service and the gateway are 'stopped'
        e1 = ends(run)
        off = I.wait_for(lambda: off_ts_after(t_on + 1), max(60, e1 - time.time() + 150), 3)
        late = (off - e1) if off else None
        I.check(late is not None and 0 <= late <= 65, "run end: plug OFF %.1f s after ends_at %s with the service and the applier idle (within a minute)" % (late if late is not None else -1, run["ends_at"][11:19]))
        c = I.db()
        logs = [d for ts, d in c.execute("""SELECT e.time_fired_ts, d.shared_data FROM events e JOIN event_types t ON e.event_type_id = t.event_type_id
                                           LEFT JOIN event_data d ON e.data_id = d.data_id WHERE t.event_type = 'logbook_entry' AND e.time_fired_ts >= ?""", (t_on,))]
        c.close()
        I.check(any("timed run ended" in (d or "") for d in logs), "the OFF came from the run-end automation (logbook 'timed run ended')")

        # ── 2. HA stopped across the end ──
        time.sleep(1)
        st.expire_runs()
        run2 = st.start_run(ACT, DRYER, 1)
        res, armed = arm(st, rig, run2)
        I.check(res == "applied" and armed, "second run armed")
        I.post_webhook("test-bathroom_towels-on")
        I.wait_for(lambda: I.last_state(SW) == "on", 20, 2)
        t_on2 = time.time()
        I.sh("docker", "stop", "-t", "20", I.NAME)
        e2 = ends(run2)
        while time.time() < e2 + 25:
            time.sleep(2)
        I.sh("docker", "start", I.NAME)
        I.check(bool(wait_up()), "scratch HA started again AFTER the end of the run")
        off = I.wait_for(lambda: off_ts_after(max(t_on2 + 1, e2 - 5)), 150, 3)
        I.check(bool(off), "HA was down across the end: after start-up the plug is OFF (reconcile)")
        hist = [(s, ts) for s, ts in I.state_history(SW) if ts >= t_on2]
        I.check(["on", "off"] == [s for s, ts in hist if s in ("on", "off")][-2:] and hist[-1][0] == "off", "never ON because of the run after the restart: the last state change is OFF", str([s for s, _ in hist]))
        c = I.db()
        logs = [d for ts, d in c.execute("""SELECT e.time_fired_ts, d.shared_data FROM events e JOIN event_types t ON e.event_type_id = t.event_type_id
                                           LEFT JOIN event_data d ON e.data_id = d.data_id WHERE t.event_type = 'logbook_entry' AND e.time_fired_ts >= ?""", (e2,))]
        c.close()
        I.check(any("Reconcile" in (d or "") and "OFF" in (d or "") for d in logs), "the OFF is the start-up reconcile's (logbook 'Reconcile ... OFF')")

        # ── 3. HA restarted in the MIDDLE of a run ──
        time.sleep(1)
        run3 = st.start_run(ACT, DRYER, 4)
        res, armed = arm(st, rig, run3)
        I.check(res == "applied" and armed, "third run (4 min) armed")
        I.post_webhook("test-bathroom_towels-on")
        I.wait_for(lambda: I.last_state(SW) == "on", 20, 2)
        t_on3 = time.time()
        I.sh("docker", "restart", I.NAME)
        I.check(bool(wait_up()), "HA restarted in the middle of the run")
        time.sleep(40)                                                  # the start-up reconcile has run
        e3 = ends(run3)
        I.check(time.time() < e3 - 20, "(the restart left more than 20 s of the run: the check below is meaningful)", "%.0f s left" % (e3 - time.time()))
        I.check(I.last_state(SW) == "on", "mid-run restart: the plug is still ON (the reconcile does not cut a running run)", str(I.last_state(SW)))
        off = I.wait_for(lambda: off_ts_after(time.time() - 30), max(30, e3 - time.time() + 150), 3)
        late = (off - e3) if off else None
        I.check(late is not None and -2 <= late <= 70, "and it is switched OFF at the end of the run (%.1f s after ends_at)" % (late if late is not None else -1))

        # ── 3b. an OFF plug is never switched ON by a run (power cut in the middle of a run) ──
        st.expire_runs()
        run4 = st.start_run(ACT, DRYER, 5)
        res, armed = arm(st, rig, run4)
        I.check(res == "applied" and armed, "fourth run armed")
        I.post_webhook("test-bathroom_towels-off")
        I.wait_for(lambda: I.last_state(SW) == "off", 20, 2)
        t_off4 = time.time()
        I.sh("docker", "restart", I.NAME)
        I.check(bool(wait_up()), "HA restarted during a run whose plug is OFF (as after a power cut)")
        time.sleep(45)
        ons = [ts for s, ts in I.state_history(SW) if s == "on" and ts > t_off4]
        I.check(not ons and I.last_state(SW) == "off", "a plug that is OFF in the middle of a run is NOT switched ON by anything", str(ons))

        # ── 4. cancel ──
        st.cancel_run(ACT, run4["id"])
        res, _a = arm(st, rig, run4)
        I.check(res == "applied" and not any("run end" in a and DRYER in a for a in rig.loaded()[0]), "cancelled run: its end-of-run automation is withdrawn from HA", res)

        # ── 5. extension moves the end ──
        run5 = st.start_run(ACT, DRYER, 3)
        res, armed = arm(st, rig, run5)
        I.check(res == "applied" and armed, "fifth run (3 min) armed")
        I.post_webhook("test-bathroom_towels-on")
        I.wait_for(lambda: I.last_state(SW) == "on", 20, 2)
        t_on5 = time.time()
        ext = st.extend_run(ACT, run5["id"], 2)
        res, armed = arm(st, rig, ext)
        I.check(res == "applied" and armed and ext["ends_at"] > run5["ends_at"], "extended by 2 min, applied and armed", "%s -> %s" % (run5["ends_at"][11:19], ext["ends_at"][11:19]))
        e_old, e_new = ends(run5), ends(ext)
        time.sleep(max(0, e_old + 75 - time.time()))
        I.check(I.last_state(SW) == "on", "the OLD end passed and the plug is still ON (the new end is what HA enforces)")
        off = I.wait_for(lambda: off_ts_after(t_on5 + 1), max(60, e_new - time.time() + 150), 3)
        late = (off - e_new) if off else None
        I.check(late is not None and 0 <= late <= 65, "OFF at the NEW end (%.1f s after)" % (late if late is not None else -1))

        # ── 7. expiry: the service closes the finished run, the render drops it, HA drops its automation ──
        st.clock = lambda: (dt.datetime.now(model.TZ) + dt.timedelta(minutes=260)).isoformat(timespec="seconds")
        n = st.expire_runs()
        st.clock = storemod.now_iso
        res = rig.apply()
        I.check(n >= 1 and res == "applied" and not any("run end" in a and DRYER in a for a in rig.loaded()[0]), "expired runs are dropped from the render and from HA", "expired %d, %s" % (n, res))
    finally:
        I.sh("docker", "rm", "-f", "-v", I.NAME)
        I.sh("sudo", "rm", "-rf", I.S)
        print("scratch container and directory removed")
    bad = [w for ok, w in I.RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(I.RESULTS), len(bad)))
    for w in bad:
        print("  FAILED:", w)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
