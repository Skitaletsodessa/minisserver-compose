#!/usr/bin/env python3
"""Task 27a item 4: live proof of the fail-safe OFF on a REAL plug (the towel dryer, at midday, Ivan starts it by hand).

Weakens nothing for more than ~15 minutes and always restores (also on error or timeout):
  1. rules -> two 2-minute slots (06:30-06:32, 18:30-18:32; no edge today before the restore), guardrail max_on_minutes -> 2   [both through the API, audited]
  2. wait until Home Assistant loaded the 2-minute fail-safe (aliases carry the hash)
  3. wait for Ivan to switch the dryer ON by hand; measure how long until the fail-safe switches it OFF
  4. guardrail back to the original, rules back with `undo`; verify HA runs exactly the original generation again
Log: /tmp/failsafe-live-test.log. Run on the server as skit.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import datetime as dt

sys.path.insert(0, "/srv/compose/portal/schedules/applier")
import portal_apply

CTL = "/srv/compose/portal/schedules/schedctl"
ENT = "bathroom_towels"
SW = "switch.bathroom_towels"
DB = "file:/srv/apps/homeassistant/home-assistant_v2.db?mode=ro"
LOG = open("/tmp/failsafe-live-test.log", "w", buffering=1)


def say(m):
    LOG.write("[%s] %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), m))


def ctl(*a):
    r = subprocess.run([CTL] + list(a), capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def health():
    rc, out = ctl("health")
    return json.loads(out) if rc == 0 else {}


def wait_sync(timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        h = health()
        if h.get("in_sync"):
            return True
        time.sleep(3)
    return False


def loaded_aliases():
    a, _s = portal_apply.Applier({}).loaded()
    return sorted(x for x in a if x.startswith("Sch %s " % ENT))


def history(since):
    c = sqlite3.connect(DB, uri=True, timeout=20)
    try:
        return c.execute("""SELECT s.state, s.last_updated_ts FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
                            WHERE m.entity_id = ? AND s.last_updated_ts > ? ORDER BY s.last_updated_ts""", (SW, since)).fetchall()
    finally:
        c.close()


def events(since):
    c = sqlite3.connect(DB, uri=True, timeout=20)
    try:
        return c.execute("""SELECT e.time_fired_ts, d.shared_data FROM events e JOIN event_types t ON e.event_type_id = t.event_type_id
                            LEFT JOIN event_data d ON e.data_id = d.data_id WHERE t.event_type IN ('automation_triggered','logbook_entry') AND e.time_fired_ts > ?
                            ORDER BY e.time_fired_ts""", (since,)).fetchall()
    finally:
        c.close()


def main():
    original_aliases = loaded_aliases()
    say("original generation: %s" % original_aliases)
    rc, out = ctl("show", ENT)
    before = json.loads(out)
    assert [r["end"] for r in before["rules"]] == ["08:00", "20:00"] and before["guardrails"]["max_on_minutes"] == 120, "unexpected starting state"
    restored = False
    try:
        rc, out = ctl("put", ENT, "/tmp/dryer-test-rules.json")
        say("put test rules rc=%s" % rc)
        assert rc == 0, out
        rc, out = ctl("guardrails", ENT, "/tmp/dryer-guardrails-test.json")
        say("guardrail max_on_minutes=2 rc=%s" % rc)
        assert rc == 0, out
        assert wait_sync(), "applier did not reach in_sync"
        time.sleep(35)                                   # recorder commit
        al = loaded_aliases()
        say("HA runs: %s" % al)
        assert any("fail-safe OFF after 2 min" in a for a in al), "HA does not run the 2-minute fail-safe"
        t_ready = time.time()
        say("READY - switch the dryer ON by hand now (deadline 12 minutes)")
        t_on = None
        while time.time() - t_ready < 12 * 60 and t_on is None:
            for s, ts in history(t_ready - 30):
                if s == "on":
                    t_on = ts
            time.sleep(3)
        if t_on is None:
            say("NO hand start within 12 minutes: test not performed, restoring")
        else:
            say("dryer ON at %s" % dt.datetime.fromtimestamp(t_on).strftime("%H:%M:%S.%f")[:-3])
            t_off = None
            while time.time() - t_on < 6 * 60 and t_off is None:
                for s, ts in history(t_on):
                    if s == "off":
                        t_off = ts
                time.sleep(2)
            if t_off is None:
                say("FAIL: dryer still ON %d s after the hand start - restoring and switching nothing else" % (time.time() - t_on))
            else:
                say("dryer OFF at %s -> ON for %.1f s (limit 120 s + at most one 60 s tick)" % (dt.datetime.fromtimestamp(t_off).strftime("%H:%M:%S.%f")[:-3], t_off - t_on))
                time.sleep(8)
                for ts, d in events(t_on - 5):
                    j = json.loads(d) if d else {}
                    if "fail-safe" in json.dumps(j):
                        say("  event %s: %s" % (dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), json.dumps(j, ensure_ascii=False)[:200]))
    finally:
        rc, out = ctl("guardrails", ENT, "/tmp/dryer-guardrails-original.json")
        say("guardrail restored rc=%s %s" % (rc, "" if rc == 0 else out[:200]))
        rc, out = ctl("undo", ENT)
        say("rules undo rc=%s %s" % (rc, "" if rc == 0 else out[:200]))
        ok = wait_sync()
        time.sleep(35)
        now_al = loaded_aliases()
        say("restored: in_sync=%s; HA generation identical to the original: %s" % (ok, now_al == original_aliases))
        if now_al != original_aliases:
            say("  now: %s" % now_al)
        rc, out = ctl("show", ENT)
        after = json.loads(out)
        say("rules after: %s; guardrails after: %s" % ([(r["id"], r["start"], r["end"]) for r in after["rules"]], after["guardrails"]))
        rc, out = ctl("audit", ENT, "8")
        for a in json.loads(out)["audit"]:
            say("  audit #%s %s %s" % (a["seq"], a["action"], a["actor"]["name"]))
        restored = True
        say("DONE")


if __name__ == "__main__":
    main()
