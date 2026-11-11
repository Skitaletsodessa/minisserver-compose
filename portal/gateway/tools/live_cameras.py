#!/usr/bin/env python3
"""Live proof of the camera module on the REAL gateway (Task 34), with the SYNTHETIC household parent `probe-phouse` (never a real person): web session, no device
record, no Telegram. Run on the server as skit:   python3 tools/live_cameras.py

Reads only: list, snapshots of the three lenses, a few seconds of live video (h264 and h265) written to a temp file and probed with ffprobe, the clip days/list, one
archived clip with a Range request, the refusals (no session, read-only role is covered by the unit tests), and the audit rows the run produced.
Nothing is written to the camera or the archive; the synthetic parent is disabled again at the end and the setup-link files are deleted."""
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

LOOP = "http://127.0.0.1:17444"
TAILNET = os.environ.get("LIVE_CAM_BASE", "https://minisserver.tail6bf4d5.ts.net:17443")
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
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, (r.read(limit) if limit else r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def ctl(*a):
    return subprocess.run([GWCTL, *a], capture_output=True, text=True, timeout=60)


def probe(path, fmt=None):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height", "-of", "compact", path], capture_output=True, text=True, timeout=60)
    return (r.stdout + r.stderr).strip()


def main():
    ctl("person", "enable", PID)
    ctl("person", "web-login", PID, "on")
    r = ctl("setup-link", PID)
    path = re.search(r"/data/out/(\S+)", r.stdout)
    link_file = os.path.join(OUT, path.group(1)) if path else None
    try:
        raw = open(link_file).read().strip().rsplit("/", 1)[-1]
        st, page, _ = call("GET", LOOP + "/setup/" + raw)
        seed = re.search(rb'class="seed">([A-Z2-7]+)<', page).group(1).decode()
        password = "Live-cam-" + secrets.token_urlsafe(18)
        st, _b, _h = call("POST", LOOP + "/setup/" + raw, urllib.parse.urlencode({"password": password, "code": auth.totp_now(seed)}).encode())
        check("the synthetic parent completed its own setup link", st == 200, str(st))
        os.remove(link_file)
        link_file = None
        st, b, h = call("POST", TAILNET + "/v1/auth/login", {"username": PID, "password": password, "totp": auth.totp_now(seed, time.time() + 30)})
        m = re.search(r"gw_session=([^;]+)", h.get("Set-Cookie") or "")
        check("signed in on the tailnet site (a web session, no device)", st == 200 and bool(m), str(st))
        if not m:
            return 1
        ck = {"Cookie": "gw_session=" + m.group(1)}

        st, b, _ = call("GET", TAILNET + "/v1/cameras")
        check("no session: 401", st == 401, str(st))
        st, b, _ = call("GET", TAILNET + "/v1/cameras", headers=ck)
        j = json.loads(b) if st == 200 else {}
        check("list: three cameras, go2rtc answers, archive summary", st == 200 and len(j.get("cameras", [])) == 3 and j.get("live") is True and j["archive"]["clips"] > 0, str(st) + " " + b[:200].decode("utf-8", "replace"))
        stamp("archive: %s clips, %.1f MB, newest %s" % (j.get("archive", {}).get("clips"), j.get("archive", {}).get("bytes", 0) / 1e6, j.get("archive", {}).get("newest_clip")))

        for cid in ("cam0", "cam1", "cam2"):
            t0 = time.time()
            st, b, h = call("GET", TAILNET + "/v1/cameras/%s/snapshot" % cid, headers=ck, timeout=40)
            check("%s snapshot is a JPEG (%d bytes, %.1f s)" % (cid, len(b), time.time() - t0), st == 200 and b[:2] == b"\xff\xd8" and len(b) > 3000, str(st))
            with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
                f.write(b)
                f.flush()
                stamp("%s frame: %s" % (cid, probe(f.name).replace("\n", " ")))

        for cid, codec in (("cam0", "h264"), ("cam1", "h264"), ("cam2", "h265")):
            t0 = time.time()
            st, b, h = call("GET", TAILNET + "/v1/cameras/%s/live?codec=%s" % (cid, codec), headers=ck, limit=300 * 1024, timeout=60)
            with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
                f.write(b)
                f.flush()
                pr = probe(f.name)
            want = "h264" if codec == "h264" else "hevc"
            check("%s live %s: video/mp4, %d KB in %.1f s, ffprobe sees %s" % (cid, codec, len(b) // 1024, time.time() - t0, want), st == 200 and h.get("Content-Type") == "video/mp4" and want in pr, "%s %s %s" % (st, h.get("Content-Type"), pr[:120]))
        st, b, _ = call("GET", TAILNET + "/v1/cameras/cam0/live?codec=vp9", headers=ck)
        check("an unknown codec is refused (422)", st == 422, str(st))

        st, b, _ = call("GET", TAILNET + "/v1/cameras/archive/days", headers=ck)
        days = json.loads(b)["days"] if st == 200 else []
        check("archive days listed", st == 200 and len(days) > 0, str(st))
        if days:
            st, b, _ = call("GET", TAILNET + "/v1/cameras/archive/clips?day=" + days[-1]["day"], headers=ck)
            clips = json.loads(b)["clips"] if st == 200 else []
            check("clips of %s listed (%d)" % (days[-1]["day"], len(clips)), st == 200 and len(clips) > 0, str(st))
            if clips:
                c = max(clips, key=lambda x: x["size"])
                st, b, h = call("GET", TAILNET + "/v1/cameras/archive/clips/%d/video" % c["id"], headers={**ck, "Range": "bytes=0-65535"}, timeout=60)
                check("a clip answers a Range request with 206 and Content-Range", st == 206 and len(b) <= 65536 and (h.get("Content-Range") or "").startswith("bytes 0-"), "%s %s" % (st, h.get("Content-Range")))
                st, b, h = call("GET", TAILNET + "/v1/cameras/archive/clips/%d/video" % c["id"], headers=ck, timeout=120)
                with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
                    f.write(b)
                    f.flush()
                    pr = probe(f.name)
                check("the whole clip is a valid HEVC mp4 (%d KB)" % (len(b) // 1024), st == 200 and "hevc" in pr, "%s %s" % (st, pr[:100]))
        st, b, _ = call("GET", TAILNET + "/v1/cameras/archive/clips/99999999/video", headers=ck)
        check("an unknown clip is 404", st == 404, str(st))

        tok = open(os.path.expanduser("~/.config/schedctl/token")).read().strip()
        st, b, _ = call("GET", TAILNET + "/v1/audit?limit=40", headers=ck)
        rows = json.loads(b)["audit"] if st == 200 else []
        acts = sorted({x["action"] for x in rows})
        check("the audit shows the live views, the snapshot and the clip play of this session", {"camera_live", "camera_snapshot", "camera_clip_play"} <= set(acts), str(acts))
    finally:
        if link_file and os.path.exists(link_file):
            os.remove(link_file)
        ctl("person", "disable", PID)
        ctl("person", "web-login", PID, "off")
        ctl("clean-out")
    r = ctl("person", "list")
    check("the synthetic parent is disabled again", re.search(PID + r"\s+household\s+parent=True\s+active=False", r.stdout) is not None, r.stdout[:200])
    stamp("RESULT: %s" % ("all checks passed" if not FAILS else "FAILED: %s" % FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
