#!/usr/bin/env python3
"""A SCRATCH stack for driving the cameras page in a real browser (Task 34b round 2): the real gateway in-process, a FAKE go2rtc that answers like a cold camera (each lens
needs a different time to produce its first byte) with a real short WebM, a synthetic archive (real WebM files) with one event that three lenses recorded 0/1/3 seconds apart.
Nothing here touches the camera, go2rtc or the house.

  python tools/ui_cameras_scratch.py OUTDIR      # prints the port; writes OUTDIR/session-household.txt (mode 600) and keeps running

Run from portal/gateway with the test venv (needs ffmpeg for the sample media). Pair it with tools/ui_cameras.py (Playwright)."""
import calendar
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from helpers import Env, PASSWORD  # noqa: E402
from gw import auth  # noqa: E402

COLD = {"cam_ch0": 0.5, "cam_ch1": 2.0, "cam_ch2": 4.0}              # seconds a lens needs for its first byte (the real ones: 5-20 s)


def make_webm(path, seconds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12", "-t", str(seconds), "-c:v", "libvpx", "-b:v", "120k", "-an", "-f", "webm", "-y", path], check=True)


class FakeGo2rtc:
    def __init__(self, media, frame=None, down=None):
        body = open(media, "rb").read()
        frame_bytes = open(frame, "rb").read() if frame else b""

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                src = urllib.parse.parse_qs(u.query).get("src", [""])[0]
                if u.path == "/api/stream.mp4" and down and os.path.exists(down):             # Task failover 2026-11-17: the lens cannot be opened as video
                    self.send_response(503)
                    self.end_headers()
                elif u.path == "/api/frame.jpeg" and frame_bytes:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(frame_bytes)))
                    self.end_headers()
                    self.wfile.write(frame_bytes)
                elif u.path == "/api":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                elif u.path == "/api/stream.mp4" and src.startswith("cam_ch0_hd"):         # the camera does not deliver its main stream (seen 2026-11-11)
                    time.sleep(1.0)
                    self.send_response(503)
                    self.end_headers()
                elif u.path == "/api/stream.mp4":
                    time.sleep(COLD.get(src.replace("_h264", ""), 0.5))
                    self.send_response(200)
                    self.send_header("Content-Type", "video/webm")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    live_media = os.path.join(outdir, "live.webm")
    make_webm(live_media, 40)
    frame = os.path.join(outdir, "frame.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x36", "-frames:v", "1", frame], check=True)
    g = FakeGo2rtc(live_media, frame, os.path.join(outdir, "live-down"))
    root = os.path.join(outdir, "camera")
    os.makedirs(root, exist_ok=True)
    db = sqlite3.connect(os.path.join(root, "index.sqlite"))
    db.execute("""CREATE TABLE clips (id TEXT PRIMARY KEY, ch INTEGER, kind TEXT, begin TEXT, end TEXT, cam_kb INTEGER,
                  size INTEGER, path TEXT, dur REAL, fps REAL, pulled_at TEXT)""")
    clips = [(0, "2026-11-12 13:15:26"), (1, "2026-11-12 13:15:27"), (2, "2026-11-12 13:15:29"),       # ONE event, seen by three lenses a second or two apart
             (1, "2026-11-12 13:30:00"),                                                              # a second event, one lens
             (0, "2026-11-12 09:00:00"), (1, "2026-11-12 09:00:02"),
             (2, "2026-11-11 22:00:00")]                                                              # the day before
    for i, (ch, begin) in enumerate(clips):
        p = os.path.join(root, begin[:10], "ch%d" % ch, begin[11:].replace(":", "") + ".mp4")
        make_webm(p, 12)
        end = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(calendar.timegm(time.strptime(begin, "%Y-%m-%d %H:%M:%S")) + 12))
        db.execute("INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("%d|%d" % (ch, i), ch, "M", begin, end, 1, os.path.getsize(p), p, 12.0, 12.0, "2026-11-12 14:00:00"))
    n_extra = int(os.environ.get("SCRATCH_EVENTS", "0"))                                        # Task 35c: many events on 12.11 14:00.. every 2 minutes, three lenses each
    if n_extra:
        base = os.path.join(root, "2026-11-12", "ch0", "131526.mp4")
        for k in range(n_extra):
            t = 14 * 3600 + k * 120
            for ch in (0, 1, 2):
                begin = "2026-11-12 %02d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60 + ch)
                p = os.path.join(root, begin[:10], "ch%d" % ch, begin[11:].replace(":", "") + ".mp4")
                os.makedirs(os.path.dirname(p), exist_ok=True)
                subprocess.run(["cp", base, p], check=True)
                end = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(calendar.timegm(time.strptime(begin, "%Y-%m-%d %H:%M:%S")) + 12))
                db.execute("INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("x%d|%d" % (ch, k), ch, "M", begin, end, 1, os.path.getsize(p), p, 12.0, 12.0, "2026-11-12 14:00:00"))
    db.commit()
    db.close()
    cams = [{"id": "cam0", "channel": 0, "src": "cam_ch0", "name": "Вход"}, {"id": "cam1", "channel": 1, "src": "cam_ch1", "name": "Левая"},
            {"id": "cam2", "channel": 2, "src": "cam_ch2", "name": "Правая"}]
    ptz = os.path.join(outdir, "ptz")
    os.makedirs(ptz, exist_ok=True)
    e = Env(camera_opts={"go2rtc_url": g.url, "archive_dir": root, "channels": cams, "ptz_dir": ptz})
    a = {"person_id": "scratch", "phone_id": None, "ip": "scratch"}
    e.people.create(a, "Marta", "household", True, web_login=True)
    raw, _ = e.people.create_setup_link(a, "marta")
    _p, seed = e.people.setup_begin(raw)
    e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed), "1.1.1.1")
    e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", ("marta",))
    cookie, _s = e.people.login("marta", PASSWORD, auth.totp_now(seed), "127.0.0.1")
    path = os.path.join(outdir, "session-household.txt")
    with open(path, "w") as f:
        f.write(cookie)
    os.chmod(path, 0o600)
    with open(os.path.join(outdir, "port.txt"), "w") as f:
        f.write(str(e.port))
    print("scratch gateway on 127.0.0.1:%d" % e.port, flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ui-cameras")
