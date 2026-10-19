#!/usr/bin/env python3
"""Integration rehearsal against a REAL Home Assistant (same pinned image) in a throw-away container with NO network.

Nothing here touches the live homeassistant container, the plugs, or any real device: the scratch instance has template switches
backed by input_booleans, the real hand-written automations.yaml / scripts.yaml of the repository (so the generated files coexist
with them exactly as in production) and the same four include lines in configuration.yaml. The real portal_apply.Applier runs
against it: real check_config, real webhook reload, real verification through the scratch recorder database.

  /srv/apps/portal/venv-test/bin/python tests/integration_scratch_ha.py [--quick]     (run on the server; takes ~12 min, --quick skips the real-time edge test)

Proves, on the real thing: labelled include blocks work for automations AND scripts; the webhook reload applies a change without a
restart; unchanged running scripts survive reloads; removed items disappear; an invalid render is rejected by check_config and rolled
back; a schedule edge fires on time; the fail-safe switches a hand-started run OFF; the start-up reconcile corrects a plug.
"""
import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import subprocess
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "applier"))
import portal_apply                      # noqa: E402
from sched import store as storemod, model, render   # noqa: E402

NAME = "portal-ha-scratch"
S = "/srv/apps/portal/scratch"
CFG, GEN = S + "/config", S + "/generated"
REPO_HA = "/srv/compose/homeassistant"
PORTAL_WH = "scratch-portal-reload-0123456789abcdef"
PLUGS = ["bathroom_towels", "living_room_rack", "livingroom_rack_big", "kitchen_orchids", "minisserver", "bedroom_parrot"]
QUICK = "--quick" in sys.argv
RESULTS = []


def check(ok, what, extra=""):
    RESULTS.append((ok, what))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", what, ("  -- " + extra) if extra else ""), flush=True)
    return ok


def sh(*a, **kw):
    return subprocess.run(list(a), capture_output=True, text=True, **kw)


def dx(*a):
    return sh("docker", "exec", NAME, *a)


def post_webhook(wid):
    code = "import urllib.request as u; r=u.urlopen(u.Request('http://127.0.0.1:8123/api/webhook/%s', data=b'{}', method='POST'), timeout=10); print(r.status)" % wid
    r = dx("python3", "-c", code)
    if r.stdout.strip() != "200":
        print("      webhook %s failed: rc=%s out=%r err=%r" % (wid[:12], r.returncode, r.stdout[:80], r.stderr[-300:]), flush=True)
    return r.stdout.strip() == "200"


def now_prague():
    return dt.datetime.now(model.TZ)


def db():
    return sqlite3.connect("file:%s/home-assistant_v2.db?mode=ro" % CFG, uri=True, timeout=10)


def state_history(entity):
    c = db()
    try:
        return c.execute("""SELECT s.state, s.last_updated_ts FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
                            WHERE m.entity_id = ? ORDER BY s.last_updated_ts""", (entity,)).fetchall()
    finally:
        c.close()


def last_state(entity):
    h = state_history(entity)
    return h[-1][0] if h else None


def wait_for(fn, timeout, every=3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except sqlite3.Error:
            pass
        time.sleep(every)
    return None


def has(autos, fragment):
    return any(a.startswith('Sch ') and fragment in a for a in autos)


def hhmm(t):
    return "%02d:%02d" % (t.hour, t.minute)


# ── scratch instance ──────────────────────────────────────────────────────────

def write_config():
    sh("sudo", "rm", "-rf", S)
    for d in (S, CFG, GEN, GEN + "/automations", GEN + "/scripts"):
        os.makedirs(d, mode=0o700, exist_ok=True)
    switches, booleans, tests = [], [], []
    for p in PLUGS:
        booleans.append("  %s:\n    name: %s" % (p, p))
        switches.append("    - name: %s\n      state: \"{{ is_state('input_boolean.%s', 'on') }}\"\n"
                        "      turn_on:\n        service: input_boolean.turn_on\n        target: {entity_id: input_boolean.%s}\n"
                        "      turn_off:\n        service: input_boolean.turn_off\n        target: {entity_id: input_boolean.%s}" % (p, p, p, p))
        for act in ("on", "off"):
            tests.append("- id: test_%s_%s\n  alias: 'TEST %s %s'\n  trigger:\n    - platform: webhook\n      webhook_id: test-%s-%s\n      local_only: true\n"
                         "  action:\n    - service: switch.turn_%s\n      target: {entity_id: switch.%s}" % (p, act, p, act, p, act, act, p))
    booleans.append("  long_done:\n    name: long_done")
    cfg = """homeassistant:
  name: scratch
  time_zone: Europe/Prague
http:
  server_host: 127.0.0.1
recorder:
  commit_interval: 3
logbook:
webhook:
input_boolean:
%s
template:
  - switch:
%s
automation manual: !include automations.yaml
automation generated: !include_dir_merge_list generated/automations/
script manual: !include scripts.yaml
script generated: !include_dir_merge_named generated/scripts/
""" % ("\n".join(booleans), "\n".join(switches))
    open(CFG + "/configuration.yaml", "w").write(cfg)
    open(CFG + "/secrets.yaml", "w").write("portal_reload_webhook: %s\n" % PORTAL_WH)
    autos = open(REPO_HA + "/automations.yaml").read()
    autos += """
# ── portal: reload the generated schedules (static, hand-written; the only way the applier reaches HA) ──
- id: portal_apply_reload
  alias: 'Portal: reload generated schedules (webhook)'
  mode: single
  trigger:
    - platform: webhook
      webhook_id: !secret portal_reload_webhook
      allowed_methods: [POST]
      local_only: true
  action:
    - service: script.reload
    - service: automation.reload
"""
    autos += "\n# ── scratch-only test controls ──\n" + "\n".join(tests) + """
- id: test_long_start
  alias: 'TEST start long script'
  trigger: [{platform: webhook, webhook_id: test-long-start, local_only: true}]
  action: [{service: script.turn_on, target: {entity_id: script.test_long}}]
"""
    open(CFG + "/automations.yaml", "w").write(autos)
    scripts = open(REPO_HA + "/scripts.yaml").read()
    scripts += """
test_long:
  alias: 'TEST long running script'
  sequence:
    - delay: {minutes: 6}
    - service: input_boolean.turn_on
      target: {entity_id: input_boolean.long_done}
"""
    open(CFG + "/scripts.yaml", "w").write(scripts)
    os.chmod(CFG + "/secrets.yaml", 0o600)


def start_ha():
    img = sh("docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}").stdout.strip()
    sh("docker", "rm", "-f", "-v", NAME)
    r = sh("docker", "run", "-d", "--name", NAME, "--network", "none", "--memory", "1024m", "-e", "TZ=Europe/Prague",
           "-v", CFG + ":/config", "-v", GEN + ":/config/generated:ro", img)
    assert r.returncode == 0, r.stderr
    t0 = time.time()

    def up():
        r = dx("python3", "-c", "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://127.0.0.1:8123/api/', timeout=3)\n"
                               "except e.HTTPError as x: print(x.code)\nexcept Exception: print(0)")
        return r.stdout.strip() == "401"
    return wait_for(up, 240, 3) is True, time.time() - t0


class Rig(portal_apply.Applier):
    def __init__(self, st):
        super().__init__({"out": st.out, "gen": GEN, "status": S + "/apply-status.json", "state": S + "/applier", "webhook_file": S + "/wh",
                          "ha_db": CFG + "/home-assistant_v2.db", "verify_timeout": 90, "verify_every": 3}, log=lambda m: print("      applier:", m, flush=True))
        open(S + "/wh", "w").write(PORTAL_WH)
        self.alerts, self.pushes = [], []

    def alert(self, key, text, clear=False):
        self.alerts.append((key, "clear" if clear else "alert"))

    def check_config(self):
        r = dx("python", "-m", "homeassistant", "--script", "check_config", "-c", "/config")
        ok, why = portal_apply.judge_check_config(r.returncode, r.stdout + r.stderr)
        self.log("check_config: %s %s" % ("ok" if ok else "REJECTED", why))
        return ok, why

    def reload(self):
        return (True, "HTTP 200") if post_webhook(PORTAL_WH) else (False, "webhook failed")

    def git_commit_push(self, message):
        self.pushes.append(message)
        return True, "stub"


def seed_store(windows):
    """A store with the real entity list but test guardrails wide enough for rules relative to 'now'."""
    d = tempfile.mkdtemp(prefix="sched-int-")
    st = storemod.Store(d)
    seed = json.load(open(os.path.join(ROOT, "seed", "initial.json")))
    for e in seed["entities"]:
        if e["kind"] == "plug":
            e["guardrails"] = {"max_on_minutes": 1440, "allowed_from": "00:00", "allowed_to": "23:59", "min_gap_minutes": 0}
    p = os.path.join(d, "seed.json")
    json.dump(seed, open(p, "w"))
    st.seed(p, {"token": "-", "name": "int", "role": "admin"})
    return st


def main():
    sysact = {"token": "-", "name": "int", "role": "admin"}
    print("scratch HA rehearsal; image %s" % sh("docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}").stdout.strip()[-20:])
    write_config()
    st = seed_store(None)
    rig = Rig(st)
    try:
        # S0: baseline - nothing managed: only inert generated files
        meta0 = json.load(open(st.out + "/render.json"))
        for rel in meta0["files"]:
            p = os.path.join(GEN, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copyfile(os.path.join(st.out, "render", rel), p)
        up, secs = start_ha()
        check(up, "scratch HA starts with labelled include blocks for automations and scripts (%.0f s)" % secs)
        if not up:
            print(sh("docker", "logs", "--tail", "40", NAME).stdout[-3000:])
            return
        check(wait_for(lambda: last_state("script.sch_fingerprint") is not None, 120) is True, "generated fingerprint script is loaded at start-up")
        check(wait_for(lambda: "Portal: reload generated schedules (webhook)" in rig.loaded()[0] and "reconcile_all_plugs" in rig.loaded()[1], 240) is True, "HA finished loading automations and scripts (webhook automation present)")
        time.sleep(5)
        autos, scripts = rig.loaded()
        check("Portal: reload generated schedules (webhook)" in autos and "reconcile_all_plugs" in scripts, "hand-written automations and scripts are loaded next to the generated ones")

        # S1: manage two plugs, apply for real
        for e in ("plant_rack1", "bathroom_towels"):
            st.patch_entity(sysact, e, {"managed": True})
        res = rig.apply()
        check(res == "applied", "real apply: check_config + webhook reload + verification through the recorder -> applied", res)
        autos, scripts = rig.loaded()
        check(has(autos, "plant_rack1 day ON") and "sch_reconcile_bathroom_towels" in scripts, "generated automations and scripts are loaded after the reload (no restart)")
        check(rig.apply() == "verified", "re-verification with nothing to apply")

        # S2: a long-running script survives reloads
        check(post_webhook("test-long-start"), "start the long-running test script")
        time.sleep(8)
        check(last_state("script.test_long") == "on", "test script is running before the reload")
        v = st.schedule("plant_rack1")["version"]
        st.put_schedule(sysact, "plant_rack1", [{"id": "day", "days": [1, 2, 3, 4, 5, 6, 7], "start": "07:41", "end": "20:00"}], v)
        res = rig.apply()
        check(res == "applied", "edit applied by webhook reload", res)
        alias = [a for a in rig.loaded()[0] if "plant_rack1 day ON" in a][0]
        check("07:41" in alias, "the new time is what HA loaded", alias)
        time.sleep(6)
        check(last_state("script.test_long") == "on", "the running script was NOT killed by script.reload / automation.reload")

        # S3: removal
        st.patch_entity(sysact, "bathroom_towels", {"managed": False})
        res = rig.apply()
        autos, scripts = rig.loaded()
        check(res == "applied" and not [k for k in autos if k.startswith("Sch bathroom_towels")] and "sch_reconcile_bathroom_towels" not in scripts,
              "an un-managed entity's automations and script disappear from HA", res)

        # S4: an invalid render is rejected by the REAL check_config and rolled back
        good = rig.read_gen()
        orig = render.render

        def bad_render(snapshot):
            out = orig(snapshot)
            k = [p for p in out["files"] if p.startswith("automations/sch_plant_rack1")][0]
            out["files"][k] = out["files"][k].replace('platform: "time"', 'platform: "no_such_platform"')
            return out
        import sched.store as ss
        ss.render.render = bad_render
        try:
            v = st.schedule("plant_rack1")["version"]
            st.put_schedule(sysact, "plant_rack1", [{"id": "day", "days": [1, 2, 3, 4, 5, 6, 7], "start": "07:42", "end": "20:00"}], v)
        finally:
            ss.render.render = orig
        res = rig.apply()
        check(res == "failed" and rig.read_gen() == good, "an invalid generated file is rejected by check_config and the previous files are restored", res)
        check(("portal-apply", "alert") in rig.alerts, "the failure raised the (tier-B) alert")
        check(any("plant_rack1 day ON 07:41" in a for a in rig.loaded()[0]), "HA still runs the previous generation")
        st.undo(sysact, "plant_rack1")                      # back to a good render (07:41 -> same rules as before the bad edit)
        ss.render.render = orig
        res = rig.apply(force=True)
        check(res in ("applied", "verified"), "after undo the good render applies again", res)

        # S5: fail-safe really switches a hand-started run off (guardrail 2 min; the plug's own window is a 2-minute slot far from now)
        t = now_prague()
        slot = [x for x in ("05:10", "09:10", "13:10", "17:10", "21:10") if abs((int(x[:2]) * 60 + int(x[3:])) - (t.hour * 60 + t.minute)) > 60][0]
        end_slot = "%02d:%02d" % (int(slot[:2]), int(slot[3:]) + 2)
        v = st.schedule("plant_rack1")["version"]
        st.put_schedule(sysact, "plant_rack1", [{"id": "day", "days": [1, 2, 3, 4, 5, 6, 7], "start": slot, "end": end_slot}], v)
        st.set_guardrails(sysact, "plant_rack1", {"max_on_minutes": 2, "allowed_from": "00:00", "allowed_to": "23:59", "min_gap_minutes": 0})
        res = rig.apply()
        check(res in ("applied", "verified"), "fail-safe limit of 2 min applied", res)
        post_webhook("test-living_room_rack-off")              # the start-up reconcile had switched it ON (it was inside the old window): clear it first
        wait_for(lambda: last_state("switch.living_room_rack") == "off", 30, 2)
        time.sleep(4)
        t_on = time.time()
        print("      S5 host time at ON: %s; scratch container started at %s, restarts so far %s" % (
            now_prague().strftime("%H:%M:%S"), sh("docker", "inspect", NAME, "--format", "{{.State.StartedAt}}").stdout.strip(),
            sh("docker", "inspect", NAME, "--format", "{{.RestartCount}}").stdout.strip()), flush=True)
        check(post_webhook("test-living_room_rack-on"), "plug switched ON by hand (test webhook), far outside its window")
        wait_for(lambda: last_state("switch.living_room_rack") == "on", 20, 2)
        off = wait_for(lambda: last_state("switch.living_room_rack") == "off", 240, 5)
        print("      S5 host time at the end of the wait: %s; container started at %s" % (
            now_prague().strftime("%H:%M:%S"), sh("docker", "inspect", NAME, "--format", "{{.State.StartedAt}}").stdout.strip()), flush=True)
        check(bool(off), "fail-safe switched the hand-started run OFF (%.0f s after ON; limit 120 s + up to one minute tick)" % (time.time() - t_on))
        print("      --- recorder: switch.living_room_rack history and automation/logbook events ---")
        for s_, ts in state_history("switch.living_room_rack"):
            print("      ", dt.datetime.fromtimestamp(ts, model.TZ).strftime("%H:%M:%S"), s_)
        c = db()
        for ts, typ, data in c.execute("""SELECT e.time_fired_ts, t.event_type, d.shared_data FROM events e JOIN event_types t ON e.event_type_id = t.event_type_id
                                          LEFT JOIN event_data d ON e.data_id = d.data_id WHERE t.event_type IN ('logbook_entry', 'automation_triggered')
                                          ORDER BY e.time_fired_ts"""):
            print("      ", dt.datetime.fromtimestamp(ts, model.TZ).strftime("%H:%M:%S"), typ, (data or "")[:140])
        c.close()

        # S6: start-up reconcile
        check(post_webhook("test-bedroom_parrot-on"), "parrot plug ON by hand outside any window (hand-written reconcile covers it)")
        wait_for(lambda: last_state("switch.bedroom_parrot") == "on", 20, 2)
        sh("docker", "restart", NAME)
        up = wait_for(lambda: dx("python3", "-c", "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://127.0.0.1:8123/api/', timeout=3)\n"
                                                    "except e.HTTPError as x: print(x.code)\nexcept Exception: print(0)").stdout.strip() == "401", 240, 3)
        check(bool(up), "scratch HA restarted")
        off = wait_for(lambda: last_state("switch.bedroom_parrot") == "off", 120, 4)
        now = now_prague()
        in_win = 660 <= now.hour * 60 + now.minute < 1080
        check(bool(off) or in_win, "start-up reconcile corrected the parrot plug (inside its 11:00-18:00 window it would stay ON)",
              "now %s" % hhmm(now))

        # S7: a schedule edge fires on time (real clock)
        if not QUICK:
            t = now_prague() + dt.timedelta(minutes=3)
            start, end = t.replace(second=0, microsecond=0), t.replace(second=0, microsecond=0) + dt.timedelta(minutes=2)
            if start.hour in (2, 3) or end.hour in (2, 3) or end.day != start.day:
                print("SKIP  edge test (would fall into the DST hours / midnight)")
            else:
                st.set_guardrails(sysact, "plant_rack2", {"max_on_minutes": 1440, "allowed_from": "00:00", "allowed_to": "23:59", "min_gap_minutes": 0})
                st.patch_entity(sysact, "plant_rack2", {"managed": True})
                v = st.schedule("plant_rack2")["version"]
                st.put_schedule(sysact, "plant_rack2", [{"id": "day", "days": [1, 2, 3, 4, 5, 6, 7], "start": hhmm(start), "end": hhmm(end)}], v)
                post_webhook("test-livingroom_rack_big-off")      # the start-up reconcile switched it ON (old window): start from OFF
                wait_for(lambda: last_state("switch.livingroom_rack_big") == "off", 30, 2)
                time.sleep(4)
                t_ref = time.time()
                res = rig.apply()
                check(res == "applied", "temporary schedule %s-%s applied" % (hhmm(start), hhmm(end)), res)

                def first_after(state, ts0):
                    for s_, ts in state_history("switch.livingroom_rack_big"):
                        if s_ == state and ts >= ts0:
                            return ts
                    return None
                on_ts = wait_for(lambda: first_after("on", t_ref), 400, 4)
                late = (on_ts - start.timestamp()) if on_ts else None
                check(late is not None and -2 <= late <= 12, "ON edge fired at %s (%+.1f s)" % (hhmm(start), late if late is not None else 0))
                off_ts = wait_for(lambda: first_after("off", (on_ts or t_ref) + 1), 300, 4)
                late = (off_ts - end.timestamp()) if off_ts else None
                check(late is not None and -2 <= late <= 12, "OFF edge fired at %s (%+.1f s)" % (hhmm(end), late if late is not None else 0))
    finally:
        sh("docker", "rm", "-f", "-v", NAME)
        sh("sudo", "rm", "-rf", S)
        print("scratch container and directory removed")
    bad = [w for ok, w in RESULTS if not ok]
    print("\n%d checks, %d failed" % (len(RESULTS), len(bad)))
    for w in bad:
        print("  FAILED:", w)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
