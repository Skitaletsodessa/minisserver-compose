#!/usr/bin/env python3
"""Tests for gateway-watch decision logic. Pure: a recording stub replaces watchnotify, nothing is sent anywhere (no Telegram, no [TEST] message)."""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("gw_watch", os.path.join(HERE, "gateway-watch.py"))
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)


class Rec:
    def __init__(self):
        self.calls = []

    def alert(self, key, text, tier="A", **kw):
        self.calls.append(("alert", key, tier))

    def clear(self, key, text, **kw):
        self.calls.append(("clear", key))

    def alerts(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "alert"]


FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


GOOD = ["127.0.0.1:17444", "100.76.61.8:17443", "0.0.0.0:22"]
HEALTH = {"status": "ok", "ha": {"link": "ws"}, "schedule_service": {"link": "ok"}, "audit_chain": {"ok": True, "rows": 9}}


def run(state=None, public=True, full=HEALTH, token_ok=True, addrs=GOOD):
    n = Rec()
    st = gw.evaluate(n, state if state is not None else {}, public, full, token_ok, addrs)
    return n, st


n, _ = run()
check("healthy: no alert", n.alerts() == [])

for bad, what in ((GOOD + ["0.0.0.0:17444"], "gateway on 0.0.0.0"), (["[::]:17444", "127.0.0.1:17444"], "gateway on [::]"),
                  (["127.0.0.1:17444", "0.0.0.0:17443"], "Caddy site on 0.0.0.0"), (["127.0.0.1:17444", "192.168.31.2:17443"], "Caddy site on the LAN address")):
    n, _ = run(addrs=bad)
    check("%s -> tier A exposure" % what, ("gateway-exposure", "A") in n.alerts())
n, _ = run()
check("loopback gateway + tailnet site: exposure is cleared, not raised", ("clear", "gateway-exposure") in n.calls and ("gateway-exposure", "A") not in n.alerts())

st = {}
for i in range(2):
    n, st = run(state=st, public=False, full=None)
check("down for 2 checks: silent", n.alerts() == [])
n, st = run(state=st, public=False, full=None)
check("down for 3 checks: ONE tier-B alert (the house is unaffected, so not A)", n.alerts() == [("gateway-down", "B")])
n, st = run(state=st)
check("answers again: down cleared, counter reset", ("clear", "gateway-down") in n.calls and st["down"] == 0)

n, _ = run(token_ok=False)
check("watcher token unusable: tier B, detailed checks skipped", n.alerts() == [("gateway-watch-token", "B")])

bad_health = dict(HEALTH, status="degraded", ha={"link": "down"})
st = {}
for i in range(2):
    n, st = run(state=st, full=bad_health)
check("degraded for 2 checks: silent", n.alerts() == [])
n, st = run(state=st, full=bad_health)
check("degraded for 3 checks: ONE tier-B alert naming the link", n.alerts() == [("gateway-degraded", "B")])
n, st = run(state=st)
check("healthy again: degraded cleared", ("clear", "gateway-degraded") in n.calls and st["degraded"] == 0)

n, _ = run(full=dict(HEALTH, status="degraded", audit_chain={"ok": False, "rows": 9}))
check("broken audit chain: tier A at once", ("gateway-audit-chain", "A") in n.alerts())
n, _ = run(full=None)
check("authenticated health unavailable (None): no false alarm", n.alerts() == [])

rv = {"id": "f123", "at": "2026-10-20T22:00:00+0200", "action": "add", "list_name": "Bad list", "reason": "probe failed: example.org is blocked now"}
n, st = run(full=dict(HEALTH, filter_reverts=[rv]))
check("a reverted filter-list change: ONE tier-B message naming the list and the reason", n.alerts() == [("gateway-filter-revert-f123", "B")])
n, st = run(state=st, full=dict(HEALTH, filter_reverts=[rv]))
check("the same revert is not announced again", n.alerts() == [])
n, st = run(state=st, full=dict(HEALTH, filter_reverts=[rv, dict(rv, id="f124")]))
check("a second revert is announced on its own", n.alerts() == [("gateway-filter-revert-f124", "B")])
n, st = run(state=st, full=dict(HEALTH, filter_reverts=[]))
check("reverts older than 48 h leave the list: both cleared", ("clear", "gateway-filter-revert-f123") in n.calls and ("clear", "gateway-filter-revert-f124") in n.calls and st["fl_alerted"] == [])
n, _ = run(full=dict(HEALTH))
check("a gateway that does not report reverts (older build): no alert", n.alerts() == [])

nd1 = {"id": "p-aaa", "person": "Дарья", "device": "Pixel Daria", "model": "Pixel 8", "created_at": "2026-10-23T09:00:00+0200"}
n, st = run(full=dict(HEALTH, new_devices=[nd1]))
check("the first run only records the devices that already exist: no message", n.alerts() == [] and st["nd_alerted"] == ["p-aaa"])
n, st = run(state=st, full=dict(HEALTH, new_devices=[nd1, dict(nd1, id="p-bbb")]))
check("a device that appears later: ONE tier-B message", n.alerts() == [("gateway-new-device-p-bbb", "B")])
n, st = run(state=st, full=dict(HEALTH, new_devices=[nd1, dict(nd1, id="p-bbb")]))
check("the same device is not announced again", n.alerts() == [])
n, st = run(state=st, full=dict(HEALTH, new_devices=[dict(nd1, id="p-bbb")]))
check("a device that left the list (revoked or older than 48 h) is cleared quietly", ("clear", "gateway-new-device-p-aaa") in n.calls and n.alerts() == [])
n, _ = run(state={"nd_alerted": []}, full=dict(HEALTH))
check("a gateway build that does not report new devices: no alert", n.alerts() == [])

n, st = run(full=dict(HEALTH, failed_logins_1h={"count": 4, "addresses": 2}))
check("a few failed sign-ins: nothing", n.alerts() == [])
n, st = run(full=dict(HEALTH, failed_logins_1h={"count": 12, "addresses": 3}))
check("a burst of failed sign-ins: ONE tier-B message", n.alerts() == [("gateway-failed-logins", "B")])
n, st = run(full=dict(HEALTH, failed_logins_1h={"count": 1, "addresses": 1}))
check("calm again: cleared", ("clear", "gateway-failed-logins") in n.calls)
n, _ = run(full=dict(HEALTH))
check("a build without the statistic: no alert", n.alerts() == [])

print("\n%d failed" % len(FAILS))
# ── Task 30c: the update channel ──
REL0 = {"published": None, "stopped": False, "pending": 0, "manifest_ok": None, "apk_ok": None, "key_ok": True}
n, st = run(full=dict(HEALTH, release=REL0))
check("channel with nothing published: no alert, state recorded", n.alerts() == [] and st["rel_notified"] == 0)
n, st = run(state={}, full=dict(HEALTH, release=dict(REL0, published=4, manifest_ok=True, apk_ok=True)))
check("first run with something already published: recorded, NOT announced", n.alerts() == [] and st["rel_notified"] == 4)
st = {"rel_notified": 0}
n, st = run(state=st, full=dict(HEALTH, release=dict(REL0, published=4, manifest_ok=True, apk_ok=True)))
check("a release published later: ONE tier-B message", n.alerts() == [("gateway-release-4", "B")] and st["rel_notified"] == 4)
n, st = run(state=st, full=dict(HEALTH, release=dict(REL0, published=4, manifest_ok=True, apk_ok=True)))
check("the same publication is not announced again", n.alerts() == [])
n, st = run(state=st, full=dict(HEALTH, release=dict(REL0, published=5, manifest_ok=True, apk_ok=True)))
check("the next publication is announced on its own", n.alerts() == [("gateway-release-5", "B")])
for field in ("manifest_ok", "apk_ok", "key_ok"):
    n, _ = run(state={"rel_notified": 5}, full=dict(HEALTH, release=dict(REL0, **{"published": 5, "manifest_ok": True, "apk_ok": True, field: False})))
    check("a channel with %s false: tier-B broken alert" % field, ("gateway-release-broken", "B") in n.alerts())
n, _ = run(state={"rel_notified": 5}, full=dict(HEALTH, release=dict(REL0, published=5, manifest_ok=True, apk_ok=True)))
check("a sound channel clears the broken alert", ("clear", "gateway-release-broken") in n.calls)
n, _ = run(full=dict(HEALTH))
check("a gateway build without the channel: no alert", n.alerts() == [])
n = Rec()
gw.evaluate(n, {}, True, HEALTH, True, GOOD, door=[])
check("door checks on and clean: cleared, no alert", n.alerts() == [] and ("clear", "gateway-door-paths") in n.calls)
n = Rec()
gw.evaluate(n, {}, True, HEALTH, True, GOOD, door=["/app/update.json answers an anonymous caller with 200"])
check("door problem: tier A", n.alerts() == [("gateway-door-paths", "A")])
n = Rec()
gw.evaluate(n, {}, True, HEALTH, True, GOOD, door=None)
check("door checks not enabled yet: silent", n.alerts() == [] and not [c for c in n.calls if c[1:2] == ("gateway-door-paths",)])
ANSW = {"/app/update.json": 401, "/app/releases/1/app.apk": 401, "/app/releases": 404, "/v1/releases": 404, "/app/download": 404, "/v1/people": 404}
check("door_problems: the decided answers are clean", gw.door_problems(lambda u: ANSW[u.replace(gw.DOOR_URL, "")]) == [])
for path, got in (("/app/update.json", 200), ("/app/releases/1/app.apk", 200), ("/app/releases", 303), ("/v1/releases", 401), ("/app/download", 200)):
    a2 = dict(ANSW)
    a2[path] = got
    check("door_problems: %s answering %s is caught" % (path, got), len(gw.door_problems(lambda u: a2[u.replace(gw.DOOR_URL, "")])) == 1)
check("door_problems: an unreachable door is not a false alarm", gw.door_problems(lambda u: None) == [])


print("\n%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
