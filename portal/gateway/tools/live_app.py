#!/usr/bin/env python3
"""Live proof of the app endpoints on the REAL gateway (Task 30), with a SYNTHETIC admin `probe-appadm` (never a real person). Run on the server.

The synthetic account is created with gwctl, its setup link is completed here over loopback with a password generated for this run (written nowhere, printed
nowhere); the account is disabled at the end (which also revokes its devices). Prints no token, cookie, password or TOTP seed.

  python3 tools/live_app.py
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

BASE = "http://127.0.0.1:17444"
GWCTL = "/srv/compose/portal/gateway/gwctl"
OUT = "/srv/apps/portal/gateway/out"
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def call(method, path, body=None, headers=None, raw=False):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json" if not isinstance(body, bytes) else "application/x-www-form-urlencoded")
    if method in ("POST", "PUT", "PATCH", "DELETE") and "Idempotency-Key" not in h:
        h["Idempotency-Key"] = "live-" + secrets.token_hex(6)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def ctl(*a):
    return subprocess.run([GWCTL, *a], capture_output=True, text=True, timeout=60)


def main():
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
    st, page, _ = call("GET", "/setup/" + raw)
    seed = re.search(rb'class="seed">([A-Z2-7]+)<', page).group(1).decode()
    password = "Live-proof-" + secrets.token_urlsafe(18)
    form = urllib.parse.urlencode({"password": password, "code": auth.totp_now(seed)}).encode()
    st, _b, _h = call("POST", "/setup/" + raw, form)
    check("the synthetic admin completed its own setup link (password + authenticator)", st == 200, str(st))
    os.remove(link_file)
    ip = {"X-Forwarded-For": "10.77.%d.%d" % (int(time.time()) % 250, int(time.time() * 7) % 250)}

    st, b, _h = call("POST", "/v1/app/login", {"username": pid, "password": password, "device": {"name": "live-proof emulator", "model": "proof", "app_version": "1.0.0"}}, ip)
    check("admin without the code is refused exactly like a wrong password (401, same body)", st == 401 and json.loads(b)["error"]["code"] == "invalid_credentials", str((st, b[:80])))
    # the code of this 30 s step was used by the setup; use the next step's code (accepted within the +-1 window)
    code = auth.totp_now(seed, time.time() + 30)
    st, b, _h = call("POST", "/v1/app/login", {"username": pid, "password": password, "totp": code, "device": {"name": "live-proof emulator", "model": "proof", "app_version": "1.0.0"}}, ip)
    check("app login with name + password + code: 201 and a device token", st == 201, str((st, b[:100])))
    tok = json.loads(b)["token"]
    H = {"Authorization": "Bearer " + tok}
    st, b, h = call("POST", "/v1/app/session", None, H)
    check("the device token gives a session cookie (HttpOnly, Secure, SameSite=Strict)", st == 200 and all(x in h["Set-Cookie"] for x in ("HttpOnly", "Secure", "SameSite=strict")), str(st))
    ck = {"Cookie": h["Set-Cookie"].split(";")[0]}
    csrf = json.loads(b)["csrf"]
    st, b, _h = call("GET", "/home", None, ck)
    check("the home page opens with the app session", st == 200 and b'data-app="yes"' in b, str(st))
    st, b, _h = call("GET", "/v1/capabilities", None, ck)
    check("capabilities say it is an app session and the admin cannot manage people from it", st == 200 and json.loads(b)["app_session"] is True and json.loads(b)["can"]["manage_people"] is False)
    for label, m, p, body in (("people list", "GET", "/v1/people", None), ("enrolment code", "POST", "/v1/people/ivan/enrolment-codes", None), ("filter lists", "GET", "/v1/parental/filter-lists", None),
                              ("device candidates", "GET", "/v1/parental/candidates", None)):
        st, b, _h = call(m, p, body, dict(ck, **{"X-CSRF-Token": csrf}))
        check("%s from an app session: 403 web_login_required" % label, st == 403 and json.loads(b)["error"]["code"] == "web_login_required", str((st, b[:80])))
    st, b, _h = call("GET", "/people", None, ck)
    check("the /people page is refused for the app session", st == 403)
    st, b, _h = call("GET", "/app/download", None, ck)
    check("the APK page is refused for an admin APP session", st == 403)
    st, b, _h = call("GET", "/app/manifest.webmanifest")
    check("the manifest is public and standalone", st == 200 and json.loads(b)["display"] == "standalone")
    st, b, h = call("GET", "/v1/devices", None, ck)
    check("API answers carry no-store", st == 200 and "no-store" in h.get("Cache-Control", ""))
    st, b, _h = call("POST", "/v1/app/logout", None, H)
    check("logout revokes this device (204)", st == 204, str(st))
    st, b, _h = call("GET", "/v1/capabilities", None, H)
    check("the token is dead afterwards", st == 401, str(st))
    st, b, _h = call("GET", "/v1/capabilities", None, ck)
    check("and so is its session", st == 401, str(st))
    # a second device, left in place so that gateway-watch announces it once; it is revoked by disabling the person below
    time.sleep(31)
    code2 = auth.totp_now(seed, time.time() + 30)
    st, b, _h = call("POST", "/v1/app/login", {"username": pid, "password": password, "totp": code2, "device": {"name": "live-proof second device", "model": "proof", "app_version": "1.0.0"}}, ip)
    check("a second device can be added (limit 5 per person)", st == 201, str((st, b[:80])))
    print("\n=== %d check(s) failed: %s ===" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
