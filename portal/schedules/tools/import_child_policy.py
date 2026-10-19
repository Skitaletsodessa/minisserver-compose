#!/usr/bin/env python3
"""Import the CURRENT child policy into the portal's model (Task 27 Phase E) - read-only with respect to AdGuard.

Reads /srv/compose/adguard/child-policy/*.txt, clients.conf and the schedule constants of childpolicy.py (WINDOW, Sunday rule),
and PUTs the result to /v1/child-policy/import (admin token). Nothing is written to AdGuard, the running policy job is not touched.
  python3 tools/import_child_policy.py [--dry-run]          # --dry-run prints the payload summary only
"""
import os
import re
import sys
import json
import urllib.request

ADGUARD = "/srv/compose/adguard"
sys.path.insert(0, ADGUARD)
URL = "http://127.0.0.1:16444"
TOKEN = os.path.expanduser("~/.config/schedctl/token")


def slug(name):
    return "child_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def build_payload():
    import childpolicy as cp
    start, end = cp.WINDOW
    rules = [{"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": "%02d:00" % start, "end": "%02d:00" % end, "variables": {"opens": ",".join(cp.GATED)}},
             {"id": "maps", "days": [1, 2, 3, 4, 5, 6], "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}}]   # Sunday: Maps dropped
    devices = [{"id": slug(name), "name": name, "ip": ip, "mac": mac, "rules": rules} for ip, name, mac in cp.load_clients()]
    lists = {"allow_always": cp.read_list("allow-always.txt"), "allow_weekdays": cp.read_list("allow-weekdays.txt"),
             "allow_extra": cp.extra_list("allow"), "deny_extra": cp.extra_list("deny")}
    return {"devices": devices, "lists": lists, "always_services": ["telegram"]}


def main():
    payload = build_payload()
    print("devices:", [(d["id"], d["ip"]) for d in payload["devices"]])
    print("lists:", {k: len(v) for k, v in payload["lists"].items()})
    if "--dry-run" in sys.argv:
        return 0
    tok = open(TOKEN).read().strip()
    req = urllib.request.Request(URL + "/v1/child-policy/import", data=json.dumps(payload).encode(), method="PUT",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("imported:", r.read().decode())
    except urllib.error.HTTPError as e:
        print("REFUSED", e.code, e.read().decode()[:600])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
