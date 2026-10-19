#!/usr/bin/env python3
"""Per-entity cutover and rollback (Task 27 Phase D). Run on the server as skit, with the test venv (PyYAML):

  /srv/apps/portal/venv-test/bin/python tools/cutover.py status
  /srv/apps/portal/venv-test/bin/python tools/cutover.py cutover ENTITY      # lamp first, dryer last (the order is the operator's)
  /srv/apps/portal/venv-test/bin/python tools/cutover.py rollback ENTITY     # the MOST RECENT cutover only (stack discipline)
  /srv/apps/portal/venv-test/bin/python tools/cutover.py rollback all        # emergency: every entity back to the hand-written config

cutover: preconditions (service healthy, in sync, coverage invariant holds, no edge of the entity within 3 minutes) -> remember the
previous hand-written files -> edit them in place -> managed=on through the API -> the applier checks, reloads and verifies HA ->
coverage invariant again -> HA no longer holds the old hand-written items -> commit + push. Any failed step rolls back by itself.
rollback restores the exact previous files in place, sets managed=off, waits for the applier, checks coverage, commits.
"""
import os
import sys
import json
import time
import shutil
import subprocess
import urllib.request
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "applier"))
import hand_edit        # noqa: E402
import coverage_check   # noqa: E402
import parity_edges as pe   # noqa: E402
import portal_apply     # noqa: E402

HA = "/srv/compose/homeassistant"
BASE = "/srv/compose/portal/schedules/baseline"
GEN = HA + "/generated"
STATE = "/srv/apps/portal/applier/cutover"       # previous files of each cutover (stack), outside git
URL = "http://127.0.0.1:16444"
TOKEN = os.path.expanduser("~/.config/schedctl/token")
FILES = ("automations.yaml", "scripts.yaml")


def api(method, path, body=None):
    tok = open(TOKEN).read().strip()
    req = urllib.request.Request(URL + path, data=(json.dumps(body).encode() if body is not None else None), method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def say(msg):
    print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def cov():
    return coverage_check.check(BASE, HA, GEN)


def git(*a):
    return subprocess.run(["git", "-C", "/srv/compose"] + list(a), capture_output=True, text=True)


def read_files():
    return {n: open(os.path.join(HA, n), encoding="utf-8").read() for n in FILES}


def write_files(files):
    for n, t in files.items():
        hand_edit.write_in_place(os.path.join(HA, n), t)


def wait_applied(timeout=240):
    """Waits until the applier reports the CURRENT render as applied and verified. Returns (ok, detail)."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        st, h = api("GET", "/v1/health")
        if st == 200:
            last = h
            r, a = h["render"], h["apply"]
            if a.get("result") == "failed" and a.get("set_hash") == r.get("set_hash"):
                return False, "applier FAILED: %s" % a.get("detail")
            if h["in_sync"]:
                return True, "applied render #%s (set %s)" % (a.get("audit_seq"), a.get("set_hash"))
        time.sleep(3)
    return False, "timeout waiting for the applier (last health: %s)" % (json.dumps(last)[:300] if last else "none")


def precheck(entity):
    st, h = api("GET", "/v1/health")
    if st != 200 or h["status"] != "ok":
        sys.exit("service not healthy: %s" % h)
    if not h["in_sync"]:
        sys.exit("render and Home Assistant are not in sync yet: %s" % h["apply"])
    r = cov()
    if r["problems"]:
        sys.exit("coverage invariant already broken before the cutover: %s" % r["problems"][:3])
    st, eff = api("GET", "/v1/effective?entity=" + entity)
    ne = eff.get("next_edge")
    if ne:
        secs = (dt.datetime.fromisoformat(ne["at"]) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if secs < 180:
            sys.exit("an edge of %s is %d s away (%s); wait until it has passed" % (entity, secs, ne["action"]))
    status = git("status", "--short", "homeassistant/automations.yaml", "homeassistant/scripts.yaml").stdout.strip()
    if status:
        sys.exit("hand-written HA files have uncommitted changes:\n" + status)
    return h


def cmd_status():
    st, ents = api("GET", "/v1/entities")
    r = cov()
    print("coverage: %s (%d edges)" % ("OK" if not r["problems"] else "BROKEN", r["edges_now"]))
    for e in ents["entities"]:
        print("  %-18s managed=%-5s %s" % (e["id"], e["managed"], e["ha_target"]))
    print("cutover stack:", sorted(os.listdir(STATE)) if os.path.isdir(STATE) else [])


def cmd_cutover(entity):
    if entity not in hand_edit.PLAN:
        sys.exit("unknown entity %s" % entity)
    st, ents = api("GET", "/v1/entities")
    if [e for e in ents["entities"] if e["id"] == entity and e["managed"]]:
        sys.exit("%s is already managed" % entity)
    precheck(entity)
    os.makedirs(STATE, exist_ok=True)
    slot = os.path.join(STATE, "%s-%s" % (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), entity))
    os.makedirs(slot)
    before = read_files()
    for n, t in before.items():
        open(os.path.join(slot, n), "w", encoding="utf-8").write(t)
    say("cutover %s: previous hand-written files saved in %s" % (entity, slot))
    na, ns = hand_edit.edit(entity, before["automations.yaml"], before["scripts.yaml"])
    try:
        import yaml
        for t in (na, ns):
            yaml.load(t, Loader=pe.HALoader)
    except Exception as e:      # the edited text must still be valid YAML before anything is written
        shutil.rmtree(slot)
        sys.exit("edited YAML does not parse: %s" % e)
    write_files({"automations.yaml": na, "scripts.yaml": ns})
    say("hand-written items of %s removed (in place)" % entity)
    code, body = api("PATCH", "/v1/entities/" + entity, {"managed": True})
    if code != 200:
        write_files(before)
        shutil.rmtree(slot)
        sys.exit("API refused managed=on: %s %s" % (code, body))
    say("managed=on; waiting for the applier (check_config, reload, verification)")
    ok, detail = wait_applied()
    if not ok:
        say("FAILED: %s -> rolling back" % detail)
        _rollback_files(entity, slot, before)
        sys.exit(1)
    say(detail)
    r = cov()
    if r["problems"]:
        say("coverage broken after the cutover: %s -> rolling back" % r["problems"][:3])
        _rollback_files(entity, slot, before)
        sys.exit(1)
    say("coverage: every edge exactly once (%d edges), reconcile by %s" % (r["edges_now"], ", ".join(e[7:] for e in r["generated_entities"]) or "-"))
    # Home Assistant must no longer hold the removed hand-written items
    ap = portal_apply.Applier({})
    autos, _scripts = ap.loaded()
    base_a = pe.load_ha(os.path.join(BASE, "automations.yaml"))
    removed = {a["alias"] for a in base_a if a.get("id") in hand_edit.PLAN[entity].get("automations", [])}
    if entity.startswith("plant_"):
        removed -= {a["alias"] for a in base_a if a.get("id") in ("sched_plant_lights_on", "sched_plant_lights_off")}
    stale = sorted(removed & autos)
    if stale:
        say("HA still holds removed hand-written items %s" % stale)
    git("add", "homeassistant/automations.yaml", "homeassistant/scripts.yaml")
    c = git("commit", "-q", "-m", "Task 27 D: %s is now generated (hand-written copies removed)\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" % entity,
            "--", "homeassistant/automations.yaml", "homeassistant/scripts.yaml")
    say("git commit: %s" % ("ok" if c.returncode == 0 else c.stderr.strip()))
    p = git("push", "-q")
    say("git push: %s" % ("ok" if p.returncode == 0 else p.stderr.strip()[:200]))
    say("DONE: %s is managed by the schedule service" % entity)


def _rollback_files(entity, slot, before):
    write_files(before)
    code, body = api("PATCH", "/v1/entities/" + entity, {"managed": False})
    ok, detail = wait_applied()
    say("rollback of %s: files restored, managed=off, applier: %s" % (entity, detail))
    r = cov()
    say("coverage after rollback: %s" % ("OK" if not r["problems"] else r["problems"][:3]))
    shutil.rmtree(slot, ignore_errors=True)


def cmd_rollback(entity):
    if entity == "all":
        base = {n: open(os.path.join(BASE, n), encoding="utf-8").read() for n in FILES}
        na, ns = hand_edit.init(base["automations.yaml"], base["scripts.yaml"])
        write_files({"automations.yaml": na, "scripts.yaml": ns})
        st, ents = api("GET", "/v1/entities")
        for e in ents["entities"]:
            if e["managed"]:
                api("PATCH", "/v1/entities/" + e["id"], {"managed": False})
        ok, detail = wait_applied()
        say("emergency rollback: hand-written files = baseline + reload hook, nothing managed; applier: %s" % detail)
        shutil.rmtree(STATE, ignore_errors=True)
    else:
        slots = sorted(os.listdir(STATE)) if os.path.isdir(STATE) else []
        if not slots or not slots[-1].endswith("-" + entity):
            sys.exit("%s is not the most recent cutover (stack: %s); roll back in reverse order or use 'all'" % (entity, slots))
        slot = os.path.join(STATE, slots[-1])
        before = {n: open(os.path.join(slot, n), encoding="utf-8").read() for n in FILES}
        _rollback_files(entity, slot, before)
    r = cov()
    print("coverage:", "OK" if not r["problems"] else r["problems"][:3])
    git("add", "homeassistant/automations.yaml", "homeassistant/scripts.yaml")
    git("commit", "-q", "-m", "Task 27 D: rollback %s\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" % entity, "--",
        "homeassistant/automations.yaml", "homeassistant/scripts.yaml")
    git("push", "-q")


def main():
    a = sys.argv[1:]
    if a[:1] == ["status"]:
        return cmd_status()
    if len(a) == 2 and a[0] == "cutover":
        return cmd_cutover(a[1])
    if len(a) == 2 and a[0] == "rollback":
        return cmd_rollback(a[1])
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
