#!/usr/bin/env python3
"""Tests of policy-drift-watch. Pure decision logic with a recording stub in place of watchnotify (nothing is sent anywhere: no Telegram, no [TEST] message),
and observe() against a fake AdGuard Api, a REAL snapshot from the schedule service's store and scratch files. Run with the portal test venv:
  PORTAL_SRC=/srv/compose/portal/schedules /srv/apps/portal/venv-test/bin/python test_policy_drift_watch.py"""
import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.environ.get("PORTAL_SRC", "/srv/compose/portal/schedules")
TMP = tempfile.mkdtemp(prefix="drift-")
os.environ["CHILD_SNAPSHOT"] = os.path.join(TMP, "child-policy.json")
os.environ["CHILD_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["CHILD_APPLY_STATUS"] = os.path.join(TMP, "child-apply-status.json")
os.environ["CHILD_SOURCE_FILE"] = os.path.join(TMP, "source.conf")
os.environ["POLICY_DRIFT_STATE"] = os.path.join(TMP, "drift-state.json")
os.environ["PORTAL_SRC"] = PORTAL
sys.path.insert(0, PORTAL)
spec = importlib.util.spec_from_file_location("pdw", os.path.join(HERE, "policy-drift-watch.py"))
pdw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdw)
import childpolicy  # noqa: E402  (the module the watcher imported from /srv/compose/adguard)
import childsnapshot  # noqa: E402
from sched import store as storemod, model, child_invariants  # noqa: E402

TZ = model.TZ
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class Rec:
    def __init__(self):
        self.calls = []

    def alert(self, key, text, tier="A", **kw):
        self.calls.append(("alert", key, tier))

    def clear(self, key, text, **kw):
        self.calls.append(("clear", key))

    def alerts(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "alert"]


def obs(**kw):
    o = {"agh_ok": True, "job_status": {"ok": True, "at": "x"}, "job_age_s": 30, "live_hash": "aaa", "expected_hashes": ["aaa"], "expected_source": "test", "mode": "files"}
    o.update(kw)
    return o


def run(state, o, now):
    n = Rec()
    return n, pdw.evaluate(n, state, o, now)


# ── decision logic ──
n, st = run({}, obs(), 1000)
check("fine: nothing", n.alerts() == [] and st == {"bad_runs": 0, "since": None})
n, st = run({}, obs(live_hash="bbb"), 1000)
check("ONE bad run (the one-minute race at an edge): silent", n.alerts() == [])
n, st = run(st, obs(live_hash="bbb"), 1120)
check("second bad run: tier B, not A", n.alerts() == [("policy-drift", "B")])
n, st = run(st, obs(live_hash="bbb"), 1000 + 14 * 60)
check("14 min: still only tier B", ("policy-drift-persistent", "A") not in n.alerts())
n, st = run(st, obs(live_hash="bbb"), 1000 + 15 * 60)
check("15 min: tier A", ("policy-drift-persistent", "A") in n.alerts())
n, st = run(st, obs(), 5000)
check("fixed: both cleared, state reset", ("clear", "policy-drift") in n.calls and ("clear", "policy-drift-persistent") in n.calls and st == {"bad_runs": 0, "since": None})
n, st2 = run({"bad_runs": 0, "since": None}, obs(), 6000)
check("healthy again and again: no clear noise", n.calls == [])

n, st = run({"bad_runs": 5, "since": 10}, obs(agh_ok=False), 99999)
check("AdGuard not answering: no conclusion, state untouched, no alert (adguard-watch's job)", n.alerts() == [] and st == {"bad_runs": 5, "since": 10})

for name, o, text in (("job failed", obs(job_status={"ok": False, "error": "boom", "at": "x"}), "last run failed: boom"),
                      ("job stalled", obs(job_age_s=600), "has not run for 10 min"),
                      ("no status file", obs(job_status=None), "no apply status"),
                      ("block missing", obs(live_hash=None), "managed block is missing"),
                      ("nothing to compare", obs(expected_hashes=[], expected_source="no usable snapshot: x"), "nothing to compare")):
    reasons = pdw.reasons_of(o)
    check("reason: %s" % name, reasons is not None and any(text in r for r in reasons))
n, st = run({}, obs(job_age_s=600), 1)
n, st = run(st, obs(job_age_s=600), 200)
check("a stalled job alone raises tier B after two runs", n.alerts() == [("policy-drift", "B")])
check("one acceptable hash among several is fine (an edge between two renders)", pdw.reasons_of(obs(live_hash="b", expected_hashes=["a", "b"])) == [])

# ── observe() with a real snapshot ──
SYSTEM = {"token": "-", "name": "test", "role": "admin"}
RULES = [{"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}]


class FakeApi:
    def __init__(self):
        self.user_rules = []
        self.fail = False

    def call(self, method, path, body=None):
        if self.fail:
            raise OSError("down")
        if path == "/filtering/status":
            return {"user_rules": list(self.user_rules)}
        if path == "/blocked_services/all":
            return {"blocked_services": [{"id": "telegram", "rules": ["||t.me^"]}, {"id": "youtube", "rules": ["||youtube.com^"]}, {"id": "roblox", "rules": ["||roblox.com^"]}]}
        raise AssertionError(path)


store = storemod.Store(os.path.join(TMP, "svc"))
store.seed(os.path.join(PORTAL, "seed", "initial.json"), SYSTEM)
store.import_child_policy(SYSTEM, {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "", "rules": RULES}],
                                  "lists": {"allow_always": sorted(child_invariants.REQUIRED_ALWAYS), "allow_weekdays": [], "allow_extra": [], "deny_extra": []}, "always_services": ["telegram"]})
store.publish()
shutil.copyfile(os.path.join(store.out, "child-policy.json"), os.environ["CHILD_SNAPSHOT"])
api = FakeApi()
childpolicy.Api = lambda: api
now = dt.datetime(2026, 10, 21, 12, 0, 20, tzinfo=TZ)                       # a Wednesday, outside the window
sd = {"telegram": ["t.me"], "youtube": ["youtube.com"], "roblox": ["roblox.com"]}
block, _ = childsnapshot.render(sd, childsnapshot.load()["snap"], now)
status = {"at": now.isoformat(timespec="seconds"), "ok": True, "rules_hash": pdw.rules_hash(block), "source": "snapshot"}
json.dump(status, open(os.environ["CHILD_APPLY_STATUS"], "w"))
open(os.environ["CHILD_SOURCE_FILE"], "w").write("snapshot\n")

api.user_rules = ["||unrelated.example.org^"] + block + ["! after"]
o = pdw.observe(now)
check("snapshot mode, the block in AdGuard is the rendered one: no reason", pdw.reasons_of(o) == [] and o["expected_source"].startswith("snapshot"))
api.user_rules = [r for r in api.user_rules if "t.me" not in r]
o = pdw.observe(now)
check("snapshot mode, an allow rule of the always-on set is gone from AdGuard: DRIFT", any("differ" in r for r in pdw.reasons_of(o)))
api.user_rules = ["||unrelated.example.org^"]
check("snapshot mode, the managed block is gone: DRIFT", any("missing" in r for r in pdw.reasons_of(pdw.observe(now))))
# an edge between two minutes: the job has not applied the new window yet (the block of 75 s ago is still acceptable)
edge = dt.datetime(2026, 10, 21, 16, 0, 20, tzinfo=TZ)
old, _ = childsnapshot.render(sd, childsnapshot.load()["snap"], edge - dt.timedelta(seconds=75))
api.user_rules = old
json.dump(dict(status, at=edge.isoformat(timespec="seconds")), open(os.environ["CHILD_APPLY_STATUS"], "w"))
check("at a window edge the previous minute's block is still acceptable", pdw.reasons_of(pdw.observe(edge)) == [])
json.dump(status, open(os.environ["CHILD_APPLY_STATUS"], "w"))
api.fail = True
o = pdw.observe(now)
check("AdGuard unreachable: no conclusion (None)", pdw.reasons_of(o) is None)
api.fail = False
# files mode compares with what the job said it applied
open(os.environ["CHILD_SOURCE_FILE"], "w").write("files\n")
api.user_rules = block
o = pdw.observe(now)
check("files mode: live block equals the hash the job reported", pdw.reasons_of(o) == [] and o["expected_source"] == "the hash the job reported")
api.user_rules = block[:-2] + ["@@||hand-edit.example.org^$client=192.168.31.60"] + block[-1:]
check("files mode: a hand edit inside the managed block is DRIFT", any("differ" in r for r in pdw.reasons_of(pdw.observe(now))))
old_status = dict(status, at=(now - dt.timedelta(minutes=9)).isoformat(timespec="seconds"))
json.dump(old_status, open(os.environ["CHILD_APPLY_STATUS"], "w"))
api.user_rules = block
check("a job that stopped writing its status 9 min ago is a reason even if the block is right", any("has not run for 9 min" in r for r in pdw.reasons_of(pdw.observe(now))))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
