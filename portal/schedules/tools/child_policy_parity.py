#!/usr/bin/env python3
"""Parity proof for Phase E: the rules block rendered from the PORTAL's model equals, line for line, the block the running policy code
(adguard/childpolicy.py:render_block) produces now - for every weekday, every 15 minutes of the day, and every override combination.

Only GET calls reach AdGuard (its service catalogue, to learn the domains of Telegram / YouTube / Roblox); nothing is written, the policy
job is not touched. The portal model is read through the API (/v1/child-policy) and rendered by the portal's own renderer, with the
same service domains handed to both sides.
  /srv/apps/portal/venv-test/bin/python tools/child_policy_parity.py        (any python3 works; PyYAML is not needed)
"""
import os
import sys
import json
import datetime as dt
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, "/srv/compose/adguard")
from sched import child_render, model      # noqa: E402
import childpolicy as cp                   # noqa: E402

URL = "http://127.0.0.1:16444"
TOKEN = os.path.expanduser("~/.config/schedctl/token")


def portal_policy():
    req = urllib.request.Request(URL + "/v1/child-policy", headers={"Authorization": "Bearer " + open(TOKEN).read().strip()})
    return json.load(urllib.request.urlopen(req, timeout=20))


class Catalogue:
    """Stand-in for childpolicy.Api that answers only the read of AdGuard's service catalogue (fetched once, for real)."""
    def __init__(self, data):
        self.data = data

    def call(self, method, path, body=None):
        assert method == "GET" and path == "/blocked_services/all", (method, path)
        return self.data


def main():
    real = cp.Api()
    catalogue = real.call("GET", "/blocked_services/all")        # the only call to AdGuard: a read
    stub = Catalogue(catalogue)
    sd = {t: cp.service_domains(stub, [t]) for t in ("telegram", "youtube", "roblox")}
    policy = portal_policy()
    clients = cp.load_clients()
    ips_portal = [(d["ip"], d["name"]) for d in policy["devices"]]
    print("devices: running policy %s | portal %s" % ([c[0] for c in clients], [d[0] for d in ips_portal]))
    ok = sorted(c[0] for c in clients) == sorted(d[0] for d in ips_portal)
    print("service domains: telegram %d, youtube %d, roblox %d" % tuple(len(sd[k]) for k in ("telegram", "youtube", "roblox")))
    ip0 = clients[0][0]
    combos = [[], [{"client": ip0, "target": "youtube"}], [{"client": ip0, "target": "roblox"}], [{"client": ip0, "target": "maps"}],
              [{"client": ip0, "target": "all"}], [{"client": c[0], "target": "all"} for c in clients],
              [{"client": clients[-1][0], "target": "youtube"}, {"client": clients[-1][0], "target": "maps"}]]
    monday = dt.date(2026, 10, 19)
    checked = bad = 0
    for dayoff in range(7):
        for minute in range(0, 1440, 15):
            now = dt.datetime.combine(monday + dt.timedelta(days=dayoff), dt.time(minute // 60, minute % 60), tzinfo=cp.TZ)
            for ov in combos:
                old = cp.render_block(stub, [(c[0], c[1]) for c in clients], now, ov)
                new = child_render.render_block(policy, sd, now, ov)
                checked += 1
                if old != new:
                    bad += 1
                    if bad <= 3:
                        import difflib
                        print("DIFFERENT at", now.isoformat(), ov)
                        print("\n".join(list(difflib.unified_diff(old, new, "running", "portal", lineterm="", n=0))[:12]))
    # the boundaries that matter most: one minute before / at / after the window edges on a weekday, and Saturday -> Sunday midnight
    for h, m, d in ((15, 59, 0), (16, 0, 0), (18, 59, 0), (19, 0, 0), (23, 59, 5), (0, 0, 6), (17, 0, 6), (0, 0, 0)):
        now = dt.datetime.combine(monday + dt.timedelta(days=d), dt.time(h, m), tzinfo=cp.TZ)
        for ov in combos:
            checked += 1
            if cp.render_block(stub, [(c[0], c[1]) for c in clients], now, ov) != child_render.render_block(policy, sd, now, ov):
                bad += 1
                print("DIFFERENT at boundary", now.isoformat(), ov)
    print("rendered blocks compared: %d, different: %d; device sets equal: %s" % (checked, bad, ok))
    print("child policy parity:", "IDENTICAL" if not bad and ok else "DIFFERENT")
    return 0 if not bad and ok else 1


if __name__ == "__main__":
    sys.exit(main())
