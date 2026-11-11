#!/usr/bin/env python3
"""Live proof of the single app gate THROUGH THE REAL FUNNEL URL (Task 34b, ADR 0009 amendment, option B), with the SYNTHETIC household parent `probe-phouse`.
Run on the server as skit:   python3 tools/live_door.py
Ivan approved ONE "new device" Telegram message for this run (the app login below creates a device). The device is revoked and the person disabled at the end.

Checks: unlisted and admin routes are 404 through the door, anonymous app routes 401, a WEB session on an app route 403 (door_requires_app_session), an app login + session
exchange work, the app session reads the cameras (list, snapshot, live 300 KB, one clip) and the first outside open is recorded in /v1/health. Throughput is measured
server -> Funnel -> server (an upper-bound proxy for the house upload, not a phone on mobile data)."""
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway")
from gw import auth  # noqa: E402

DOOR = os.environ.get("LIVE_DOOR", "https://minisserver.tail6bf4d5.ts.net")
TAILNET = "https://minisserver.tail6bf4d5.ts.net:17443"
LOOP = "http://127.0.0.1:17444"
GWCTL = "/srv/compose/portal/gateway/gwctl"
OUT = "/srv/apps/portal/gateway/out"
PID = "probe-phouse"
FAILS = []


def stamp(m):
    print("%s  %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def check(name, cond, detail=""):
    stamp("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(method, url, body=None, headers=None, limit=None, timeout=60):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded" if isinstance(body, bytes) else "application/json")
    if method != "GET":
        h.setdefault("Idempotency-Key", "live-" + secrets.token_hex(6))
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = OPENER.open(req, timeout=timeout)
        return r.status, (r.read(limit) if limit else r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def ctl(*a):
    return subprocess.run([GWCTL, *a], capture_output=True, text=True, timeout=60)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "compact", path], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def main():
    phone_id = None
    ctl("person", "enable", PID)
    ctl("person", "web-login", PID, "on")
    r = ctl("setup-link", PID)
    path = re.search(r"/data/out/(\S+)", r.stdout)
    link_file = os.path.join(OUT, path.group(1)) if path else None
    try:
        raw = open(link_file).read().strip().rsplit("/", 1)[-1]
        st, page, _ = call("GET", LOOP + "/setup/" + raw)
        seed = re.search(rb'class="seed">([A-Z2-7]+)<', page).group(1).decode()
        password = "Live-door-" + secrets.token_urlsafe(18)
        st, _b, _h = call("POST", LOOP + "/setup/" + raw, urllib.parse.urlencode({"password": password, "code": ""}).encode())     # a household parent may skip the authenticator
        check("the synthetic parent completed its own setup link", st == 200, str(st))
        os.remove(link_file)
        link_file = None

        # ── the door as the internet sees it ──
        for p in ("/v1/zzz-not-a-route", "/v1/people", "/v1/auth/session", "/v1/audit", "/login", "/app/download", "/v1/health", "/people", "/v1/parental/filter-lists", "/app/releases"):
            st, b, _ = call("GET", DOOR + p)
            check("door %s -> 404" % p, st == 404, str(st))
        for p in ("/v1/cameras", "/v1/capabilities", "/v1/devices", "/app/update.json"):
            st, b, _ = call("GET", DOOR + p)
            check("door %s anonymous -> 401" % p, st == 401, str(st))
        st, b, h = call("GET", DOOR + "/static/app.css")
        check("door /static/app.css is public (200)", st == 200, str(st))

        # ── a web session (password + code) is refused at the door, accepted on the tailnet ──
        st, b, h = call("POST", TAILNET + "/v1/auth/login", {"username": PID, "password": password})
        m = re.search(r"gw_session=([^;]+)", h.get("Set-Cookie") or "")
        check("web login on the tailnet site works (a web session)", st == 200 and bool(m), str(st))
        web = {"Cookie": "gw_session=" + m.group(1)} if m else {}
        st, b, _ = call("GET", TAILNET + "/v1/capabilities", headers=web)
        check("the web session works on the tailnet", st == 200, str(st))
        st, b, _ = call("GET", DOOR + "/v1/capabilities", headers=web)
        check("the SAME web session through the door -> 403 door_requires_app_session", st == 403 and b"door_requires_app_session" in b, "%s %s" % (st, b[:80]))
        st, b, _ = call("POST", DOOR + "/v1/auth/login", {"username": PID, "password": password})
        check("web login through the door -> 404", st == 404, str(st))
        st, b, _ = call("GET", TAILNET + "/v1/people", headers=web)
        # (household: refused by role on the tailnet; the point is it is not 404 there)
        check("an admin route exists on the tailnet (not 404)", st != 404, str(st))

        # ── the app path: login through the door, session exchange, cameras ──
        t0 = time.time()
        st, b, h = call("POST", DOOR + "/v1/app/login", {"username": PID, "password": password, "device": {"name": "Live-door probe", "model": "synthetic", "app_version": "0.0.0"}})
        j = json.loads(b) if st == 201 else {}
        check("app login through the door (201, a device token)", st == 201 and "token" in j, "%s %s" % (st, b[:100]))
        tok = j.get("token")
        phone_id = j.get("phone_id")
        if not tok:
            return 1
        bearer = {"Authorization": "Bearer " + tok}
        st, b, h = call("POST", DOOR + "/v1/app/session", headers=bearer)
        m = re.search(r"gw_session=([^;]+)", h.get("Set-Cookie") or "")
        check("session exchange through the door (an app session)", st == 200 and bool(m), str(st))
        app = {"Cookie": "gw_session=" + m.group(1)}
        st, b, _ = call("GET", DOOR + "/v1/capabilities", headers=app)
        check("the app session passes the door on an app route", st == 200, str(st))
        st, b, _ = call("GET", DOOR + "/v1/capabilities", headers=bearer)
        check("a device token passes the door too", st == 200, str(st))
        st, b, _ = call("GET", DOOR + "/v1/people", headers=bearer)
        check("an admin route stays 404 even with a valid device token", st == 404, str(st))

        st, b, _ = call("GET", DOOR + "/v1/cameras", headers=app)
        j = json.loads(b) if st == 200 else {}
        check("cameras list through the door: three lenses", st == 200 and len(j.get("cameras", [])) == 3, str(st))
        names = [c["name"] for c in j.get("cameras", [])]
        stamp("names: %s" % names)
        for cid in ("cam0", "cam1", "cam2"):
            t = time.time()
            st, b, _ = call("GET", DOOR + "/v1/cameras/%s/snapshot" % cid, headers=app, timeout=60)
            check("%s snapshot through the door (%d KB, %.1f s)" % (cid, len(b) // 1024, time.time() - t), st == 200 and b[:2] == b"\xff\xd8", str(st))
        t = time.time()
        st, b, h = call("GET", DOOR + "/v1/cameras/cam0/live?codec=h264", headers=app, limit=300 * 1024, timeout=90)
        dt = time.time() - t
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(b)
            f.flush()
            pr = probe(f.name)
        check("live h264 through the door: %d KB in %.1f s, ffprobe sees h264" % (len(b) // 1024, dt), st == 200 and "h264" in pr, "%s %s" % (st, pr[:100]))
        st, b, _ = call("GET", DOOR + "/v1/cameras/archive/days", headers=app)
        days = json.loads(b)["days"] if st == 200 else []
        check("archive days through the door", st == 200 and len(days) > 0, str(st))
        if days:
            st, b, _ = call("GET", DOOR + "/v1/cameras/archive/clips?day=" + days[0]["day"], headers=app)
            clips = json.loads(b)["clips"] if st == 200 else []
            if clips:
                c = max(clips, key=lambda x: x["size"])
                t = time.time()
                st, b, h = call("GET", DOOR + "/v1/cameras/archive/clips/%d/video" % c["id"], headers={**app, "Range": "bytes=0-65535"}, timeout=90)
                first = time.time() - t
                check("clip first 64 KB through the door in %.2f s (206)" % first, st == 206, str(st))
                t = time.time()
                st, b, h = call("GET", DOOR + "/v1/cameras/archive/clips/%d/video" % c["id"], headers=app, timeout=300)
                dt = time.time() - t
                stamp("hairpin (the server resolves its own Funnel name to its tailnet address): clip %.1f MB in %.1f s = %.0f Mbit/s - NOT an internet measurement, only proves the transfer works" % (len(b) / 1e6, dt, len(b) * 8 / 1e6 / max(dt, 0.01)))
                check("the whole clip arrived (200)", st == 200 and len(b) == c["size"], "%s %d vs %d" % (st, len(b), c["size"]))
        st, b, _ = call("GET", DOOR + "/v1/health", headers=bearer)         # health is closed at the door: the proof reads it on the loopback with the same token
        st, b, _ = call("GET", LOOP + "/v1/health", headers=bearer)
        j = json.loads(b) if st == 200 else {}
        check("the first outside camera open is recorded for this device (health.camera_outside_new)", any(x["id"] == phone_id for x in j.get("camera_outside_new", [])), str(j.get("camera_outside_new"))[:120])
    finally:
        if link_file and os.path.exists(link_file):
            os.remove(link_file)
        if phone_id:
            ctl("phone-revoke", phone_id)
        ctl("person", "disable", PID)
        ctl("person", "web-login", PID, "off")
        ctl("clean-out")
    r = ctl("person", "list")
    check("the synthetic parent is disabled again", re.search(PID + r"\s+household\s+parent=True\s+active=False", r.stdout) is not None, r.stdout[:200])
    stamp("RESULT: %s" % ("all checks passed" if not FAILS else "FAILED: %s" % FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
