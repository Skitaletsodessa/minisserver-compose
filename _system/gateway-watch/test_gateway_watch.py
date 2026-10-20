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

print("\n%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
