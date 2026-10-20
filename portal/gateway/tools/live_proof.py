#!/usr/bin/env python3
"""Live proof helper for Task 28 Phase J (stdlib only, run on the server). Talks to the REAL gateway as a SYNTHETIC person (never as Ivan).
No token or code is ever printed; tokens live in mode-600 files.

  live_proof.py enrol  PERSON_ID                  read /srv/apps/portal/gateway/out/enrol-PERSON.txt, enrol a device, store the token in ~/.config/gw-probe/PERSON
  live_proof.py state  PERSON_ID                  capabilities + devices + vacuums + services as that person sees them (names, states, stale flags)
  live_proof.py switch PERSON_ID DEVICE on|off    one switch call; prints the result object (status/confirmed/observed)
  live_proof.py get    PERSON_ID PATH             GET and print JSON
  live_proof.py ha-admin-refused                  the gateway's own HA token must be refused an admin-only service (homeassistant.check_config)
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("GW_BASE", "http://127.0.0.1:17444")
CONF = os.path.expanduser("~/.config/gw-probe")
OUT = "/srv/apps/portal/gateway/out"


def call(method, path, token=None, body=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        h.setdefault("Idempotency-Key", "live-" + os.urandom(6).hex())
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:200]


def tok(person):
    p = os.path.join(CONF, person)
    if os.stat(p).st_mode & 0o077:
        sys.exit("token file must be mode 600")
    return open(p).read().strip()


def show(o):
    print(json.dumps(o, indent=1, ensure_ascii=False))


def main(a):
    if len(a) < 2:
        print(__doc__)
        return 2
    c = a[1]
    if c == "enrol":
        person = a[2]
        code = open(os.path.join(OUT, "enrol-%s.txt" % person)).read().strip()
        st, b = call("POST", "/v1/enrol", body={"code": code, "device": {"label": "live-proof probe", "model": "scripted client"}})
        if st != 201:
            print("enrol refused", st, b)
            return 1
        os.makedirs(CONF, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(CONF, person), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(b["token"] + "\n")
        print("enrolled; phone", b.get("phone_id"), "token stored in", os.path.join(CONF, person), "(mode 600)")
        return 0
    if c == "state":
        t = tok(a[2])
        st, cap = call("GET", "/v1/capabilities", t)
        print("capabilities:", st)
        show(cap)
        st, d = call("GET", "/v1/devices", t)
        print("devices:", st)
        for x in d["devices"]:
            print("  %-16s %-20s state=%-11s stale=%s switchable=%s power=%s" % (x["id"], x.get("name"), x.get("state"), x.get("stale"), x.get("can_switch"), x.get("power_w")))
        st, v = call("GET", "/v1/vacuums", t)
        print("vacuums:", st)
        for x in v["vacuums"]:
            print("  %-12s %-22s status=%-10s stale=%s scenarios=%s" % (x["id"], x.get("name"), x.get("status"), x.get("stale"), [s["id"] for s in x.get("scenarios", [])]))
        st, s = call("GET", "/v1/services", t)
        print("services:", st)
        for x in s["services"]:
            print("  %-14s status=%s %s" % (x["id"], x.get("status"), x.get("detail") or ""))
        return 0
    if c == "switch":
        st, b = call("POST", "/v1/devices/%s/switch" % a[3], tok(a[2]), {"on": a[4] == "on"})
        print("HTTP", st)
        show(b)
        return 0 if st == 200 else 1
    if c == "get":
        st, b = call("GET", a[3], tok(a[2]))
        print("HTTP", st)
        show(b)
        return 0
    if c == "ha-admin-refused":
        # the REAL token of the REAL gateway user against the REAL HA, a harmless admin-only service
        t = open("/srv/apps/portal/gateway-secrets/ha-token").read().strip()
        for path, label in (("/api/services/homeassistant/check_config", "homeassistant.check_config (admin only)"), ("/api/services/automation/reload", "automation.reload (admin only)")):
            req = urllib.request.Request("http://127.0.0.1:8123" + path, data=b"{}", method="POST", headers={"Authorization": "Bearer " + t, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    print("NOT REFUSED:", label, r.status)
                    return 1
            except urllib.error.HTTPError as e:
                print("refused:", label, "HTTP", e.code)
        req = urllib.request.Request("http://127.0.0.1:8123/api/", headers={"Authorization": "Bearer " + t})
        with urllib.request.urlopen(req, timeout=15) as r:
            print("the same token reads the API: HTTP", r.status)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
