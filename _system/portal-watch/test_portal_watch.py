#!/usr/bin/env python3
"""Tests for portal-watch decision logic. Pure: a recording stub replaces watchnotify, nothing is sent anywhere."""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pw", os.path.join(HERE, "portal-watch.py"))
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)


class Rec:
    def __init__(self):
        self.calls = []

    def alert(self, key, text, tier="A", **kw):
        self.calls.append(("alert", key, tier))

    def clear(self, key, text, **kw):
        self.calls.append(("clear", key))

    def alerts(self):
        return [(k, t) for a, k, *t in self.calls if a == "alert" for t in [t[0]]]


FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


GOOD_ADDRS = ["127.0.0.1:16444", "100.76.61.8:16443", "0.0.0.0:22"]
HEALTH = {"audit_chain": {"ok": True, "rows": 5}, "publish_error": None, "in_sync": True, "render": {"set_hash": "abc"}, "apply": {}}


def run(state=None, now=1000.0, public=True, full=HEALTH, token_ok=True, addrs=GOOD_ADDRS):
    n = Rec()
    st = pw.evaluate(n, state if state is not None else {}, now, public, full, token_ok, addrs)
    return n, st


n, _ = run()
check("healthy: no alert", n.alerts() == [])

n, _ = run(addrs=GOOD_ADDRS + ["0.0.0.0:16444"])
check("service on 0.0.0.0 -> tier A exposure", ("portal-exposure", "A") in n.alerts())
n, _ = run(addrs=["127.0.0.1:16444", "0.0.0.0:16443"])
check("Caddy site on 0.0.0.0 -> tier A exposure", ("portal-exposure", "A") in n.alerts())
n, _ = run(addrs=["127.0.0.1:16444", "192.168.31.2:16443"])
check("Caddy site on the LAN address -> tier A exposure", ("portal-exposure", "A") in n.alerts())
n, _ = run(addrs=["[::]:16444", "127.0.0.1:16444"])
check("service on [::] -> tier A exposure", ("portal-exposure", "A") in n.alerts())

st = {}
for i in range(2):
    n, st = run(state=st, public=False, full=None)
check("down twice: still silent", n.alerts() == [] and st["down"] == 2)
n, st = run(state=st, public=False, full=None)
check("down three times: ONE tier-B alert", n.alerts() == [("portal-down", "B")])
n, st = run(state=st)
check("back up: counter reset and cleared", st["down"] == 0 and ("clear", "portal-down") in n.calls)

bad = dict(HEALTH, audit_chain={"ok": False, "rows": 5, "first_bad_seq": 3})
n, _ = run(full=bad)
check("audit chain broken -> tier A", ("portal-audit-chain", "A") in n.alerts())

n, _ = run(full=dict(HEALTH, publish_error="OSError: disk full"))
check("publish error -> tier B", ("portal-publish", "B") in n.alerts())

out_of_sync = dict(HEALTH, in_sync=False, apply={"result": "pending", "detail": "reload not done"})
n, st = run(full=out_of_sync, now=1000.0)
check("out of sync: silent at first, clock started", n.alerts() == [] and st["unsynced_since"] == 1000.0)
n, st = run(state=st, full=out_of_sync, now=1000.0 + 14 * 60)
check("out of sync 14 min: still silent", n.alerts() == [])
n, st = run(state=st, full=out_of_sync, now=1000.0 + 16 * 60)
check("out of sync 16 min: tier B", n.alerts() == [("portal-unsynced", "B")])
n, st = run(state=st, full=HEALTH, now=1000.0 + 17 * 60)
check("in sync again: cleared, clock dropped", "unsynced_since" not in st and ("clear", "portal-unsynced") in n.calls)

n, _ = run(token_ok=False)
check("token unreadable -> tier B, no crash", n.alerts() == [("portal-watch-token", "B")])
n, _ = run(full=dict(HEALTH, in_sync=False, render={"set_hash": None}))
check("nothing rendered yet is not 'unsynced'", n.alerts() == [])

print("RESULT:", "all passed" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
