#!/usr/bin/env python3
"""Live proof of the UPDATE CHANNEL (Task 30c) on the REAL gateway through the public door, with the synthetic admin probe-appadm (never a real person). Prints no token, key, password or seed.
  LIVE_APP_BASE=https://minisserver.tail6bf4d5.ts.net python3 tools/live_update.py
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway")
from gw import auth  # noqa: E402

BASE = "http://127.0.0.1:17444"                       # the setup link is completed over loopback (the public door has no /setup)
API = os.environ.get("LIVE_APP_BASE", BASE)              # everything else goes here: set LIVE_APP_BASE=https://minisserver.tail6bf4d5.ts.net to prove the PUBLIC door
GWCTL = "/srv/compose/portal/gateway/gwctl"
OUT = "/srv/apps/portal/gateway/out"
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def call(method, path, body=None, headers=None, raw=False, base=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json" if not isinstance(body, bytes) else "application/x-www-form-urlencoded")
    if method in ("POST", "PUT", "PATCH", "DELETE") and "Idempotency-Key" not in h:
        h["Idempotency-Key"] = "live-" + secrets.token_hex(6)
    req = urllib.request.Request((base or API) + path, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def ctl(*a):
    return subprocess.run([GWCTL, *a], capture_output=True, text=True, timeout=60)



def main():
    """Task 30c live proof of the update channel on the REAL gateway through the PUBLIC door, with the SYNTHETIC admin `probe-appadm`. Nothing is published:
    the channel is shown as it is for a phone when no release is approved yet (update:false), the APK of a pending release is not served, the update key opens only
    its two paths, an anonymous caller and a wrong key get the identical 401, the admin paths do not exist at the door."""
    pid = "probe-appadm"
    r = ctl("person", "list")
    if pid not in r.stdout:
        check("synthetic admin created", ctl("person", "add", "Probe AppAdm", "admin", "--parent").returncode == 0)
    ctl("person", "enable", pid)
    ctl("web-login", pid, "on")
    r = ctl("setup-link", pid)
    path = re.search(r"/data/out/(\S+)", r.stdout)
    link_file = os.path.join(OUT, path.group(1)) if path else None
    raw = open(link_file).read().strip().rsplit("/", 1)[-1]
    st, page, _ = call("GET", "/setup/" + raw, base=BASE)
    seed = re.search(rb'class="seed">([A-Z2-7]+)<', page).group(1).decode()
    password = "Live-proof-" + secrets.token_urlsafe(18)
    form = urllib.parse.urlencode({"password": password, "code": auth.totp_now(seed)}).encode()
    st, _b, _h = call("POST", "/setup/" + raw, form, base=BASE)
    check("the synthetic admin completed its own setup link", st == 200, str(st))
    os.remove(link_file)

    st, b, _h = call("GET", "/app/update.json")
    a401 = (st, b)
    check("PUBLIC DOOR: anonymous /app/update.json = 401", st == 401, str(st))
    st, b, _h = call("GET", "/app/update.json", None, {"Authorization": "Bearer ukey_" + "0" * 43})
    check("PUBLIC DOOR: a wrong key gets the IDENTICAL 401 (same body)", (st, b) == a401, str((st, b[:60])))
    st, b, _h = call("GET", "/app/releases/4/app.apk")
    check("PUBLIC DOOR: anonymous APK = 401 (the same body)", (st, b) == a401, str(st))

    time.sleep(1)
    code = auth.totp_now(seed, time.time() + 30)
    st, b, _h = call("POST", "/v1/app/login", {"username": pid, "password": password, "totp": code, "device": {"name": "live-proof update (30c)", "model": "proof", "app_version": "1.1.0"}})
    check("app login through the PUBLIC door: 201, a device token and an update key", st == 201 and json.loads(b).get("update_key", "").startswith("ukey_"), str((st, b[:60])))
    tok = json.loads(b)["token"]
    ukey = json.loads(b)["update_key"]
    HT = {"Authorization": "Bearer " + tok}
    HU = {"Authorization": "Bearer " + ukey}

    for label, H in (("the update key", HU), ("the device token", HT)):
        st, b, h = call("GET", "/app/update.json", None, H)
        j = json.loads(b) if st == 200 else {}
        check("%s opens /app/update.json (nothing approved yet: update=false), no-store" % label, st == 200 and j.get("update") is False and "no-store" in h.get("Cache-Control", ""), str((st, b[:100])))
        st, b, _h = call("GET", "/app/releases/4/app.apk", None, H)
        check("%s: the APK of a PENDING release is not served (404)" % label, st == 404, str(st))
    for label, p in (("capabilities", "/v1/capabilities"), ("devices", "/v1/devices"), ("session", "/v1/app/session")):
        m = "POST" if p == "/v1/app/session" else "GET"
        st, b, _h = call(m, p, None, HU)
        check("the update key opens NOTHING else: %s = 401" % label, st == 401, str(st))
    for label, m, p in (("release page", "GET", "/app/releases"), ("release API", "GET", "/v1/releases"), ("publish", "POST", "/v1/releases/4/publish"), ("kill switch", "POST", "/v1/releases/stop")):
        st, b, _h = call(m, p, None if m == "GET" else {}, HT)
        check("PUBLIC DOOR: %s does not exist (404) even with a valid device token" % label, st == 404, str(st))
    st, b, _h = call("POST", "/v1/app/update-key", None, HT)
    check("a phone that signed in earlier can fetch a fresh update key with its device token", st in (200, 201) and json.loads(b).get("update_key", "").startswith("ukey_"), str((st, b[:20])))
    new_key = json.loads(b)["update_key"] if st in (200, 201) else ""
    st, b, _h = call("GET", "/app/update.json", None, HU)
    check("the old update key is dead after a new one is issued", st == 401, str(st))
    st, b, _h = call("GET", "/app/update.json", None, {"Authorization": "Bearer " + new_key})
    check("the new update key works", st == 200, str(st))
    st, b, _h = call("POST", "/v1/app/update-key", None, {"Authorization": "Bearer " + new_key})
    check("an update key cannot mint another one", st == 401, str(st))

    # the rate limit at Caddy: 12 APK requests per 5 minutes per real client address (the earlier requests of this run count too)
    codes = [call("GET", "/app/releases/4/app.apk", None, {"Authorization": "Bearer " + new_key})[0] for _ in range(14)]
    check("PUBLIC DOOR: the APK path is rate limited (429 after the allowance)", 429 in codes, str(codes))

    # leave the device in place for the watcher (one «Новое устройство» message was agreed with Ivan), then revoke it by disabling the person
    time.sleep(float(os.environ.get("LIVE_UPDATE_WAIT", "80")))
    ctl("person", "disable", pid)
    st, b, _h = call("GET", "/app/update.json", None, {"Authorization": "Bearer " + new_key})
    check("disabling the person kills the update key", st == 401, str(st))
    print("\n=== %d check(s) failed: %s ===" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
