"""The camera module through the gateway (Task 34): a fake go2rtc (real HTTP), a synthetic clip archive (sqlite index + mp4 files), synthetic people.
Role matrix (parent household only), snapshot, live relay (codec, limits, audit), archive days/clips, clip playback with ranges, path safety."""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, PASSWORD  # noqa: E402
from gw import auth, config  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"J" * 400 + b"\xff\xd9"
CHUNK = b"M" * 65536


class FakeGo2rtc:
    def __init__(self):
        outer = self
        self.requests = []
        self.down = False
        self.fail_stream = 0                       # the next N stream requests answer 500 (go2rtc's cold start of a transcoded source)

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if outer.down:
                    self.send_response(503)
                    self.end_headers()
                    return
                u = urllib.parse.urlparse(self.path)
                if u.path == "/api":
                    body = b'{"version":"fake"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/frame.jpeg":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(JPEG)))
                    self.end_headers()
                    self.wfile.write(JPEG)
                elif u.path == "/api/stream.mp4" and outer.fail_stream > 0:
                    outer.fail_stream -= 1
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"streams: exec/rtsp\n")
                elif u.path == "/api/stream.mp4":
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.end_headers()
                    for _ in range(3):
                        self.wfile.write(CHUNK)
                        self.wfile.flush()
                else:
                    self.send_response(404)
                    self.end_headers()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


def make_archive(root):
    os.makedirs(root, exist_ok=True)
    db = sqlite3.connect(os.path.join(root, "index.sqlite"))
    db.execute("""CREATE TABLE clips (id TEXT PRIMARY KEY, ch INTEGER, kind TEXT, begin TEXT, end TEXT, cam_kb INTEGER,
                  size INTEGER, path TEXT, dur REAL, fps REAL, pulled_at TEXT)""")
    rows = [("0|a", 0, "M", "2026-11-12 09:00:00", "2026-11-12 09:00:30", 10, 5000, "/srv/library/camera/2026-11-12/ch0/090000-090030.mp4", 30.0),
            ("1|b", 1, "M", "2026-11-12 09:10:00", "2026-11-12 09:10:20", 10, 3000, "/srv/library/camera/2026-11-12/ch1/091000-091020.mp4", 20.0),
            ("0|c", 0, "M", "2026-11-11 22:00:00", "2026-11-11 22:00:10", 10, 2000, "/srv/library/camera/2026-11-11/ch0/220000-220010.mp4", 10.0),
            ("0|evil", 0, "M", "2026-11-12 10:00:00", "2026-11-12 10:00:10", 10, 10, "/srv/library/camera/../../etc/passwd", 10.0),
            ("0|gone", 0, "M", "2026-11-12 11:00:00", "2026-11-12 11:00:10", 10, 10, "/srv/library/camera/2026-11-12/ch0/110000-110010.mp4", 10.0)]
    for i, (cid, ch, kind, b, e, kb, size, path, dur) in enumerate(rows):
        db.execute("INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, ch, kind, b, e, kb, size, path, dur, 12.0, "2026-11-12 12:00:00"))
    db.commit()
    db.close()
    for rel, size in (("2026-11-12/ch0/090000-090030.mp4", 5000), ("2026-11-12/ch1/091000-091020.mp4", 3000), ("2026-11-11/ch0/220000-220010.mp4", 2000)):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(bytes(range(256)) * (size // 256) + b"x" * (size % 256))


def setUpModule():
    g = FakeGo2rtc()
    root = os.path.join(tempfile.mkdtemp(prefix="gw-cam-"), "camera")
    make_archive(root)
    cams = [{"id": "cam0", "channel": 0, "src": "cam_ch0", "name": "Двор — поворотная"}, {"id": "cam1", "channel": 1, "src": "cam_ch1", "name": "Двор — камера 2"},
            {"id": "cam2", "channel": 2, "src": "cam_ch2", "name": "Двор — камера 3"}]
    e = BaseCase.ENV = Env(camera_opts={"go2rtc_url": g.url, "archive_dir": root, "channels": cams})
    e.g2, e.archive_root = g, root
    a = {"person_id": "test", "phone_id": None, "ip": "test"}
    for name, role, parent in (("Bob", "household", False), ("Gus", "admin", False)):
        e.people.create(a, name, role, parent)
        code, _ = e.people.create_enrolment_code(a, name.lower())
        e.tokens[name.lower()] = e.people.enrol(code, {"label": name + "-phone", "model": "Test"}, "10.0.0.1")["token"]
    e.seeds = {}
    for name, role, parent in (("Marta", "household", True), ("Kid Sitter", "household", False), ("Webadmin", "admin", True)):      # web accounts for the page tests
        e.people.create(a, name, role, parent, web_login=True)
        pid = name.lower().replace(" ", "-")
        raw, _ = e.people.create_setup_link(a, e.people.get(pid)["id"])
        _p, seed = e.people.setup_begin(raw)
        e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed), "1.1.1.1")
        e.seeds[pid] = seed


def tearDownModule():
    BaseCase.ENV.g2.stop()
    BaseCase.ENV.close()


class Cam(BaseCase):
    def setUp(self):
        super().setUp()
        from gw import cameras as camerasmod
        camerasmod.RETRY_PAUSE = 0.01
        self.e.g2.requests.clear()
        self.e.g2.down = False
        self.e.g2.fail_stream = 0
        self.e.o["app"].live_open = 0
        self.e.o["cameras"].sessions.clear()

    def audit(self, action):
        return [r for r in self.e.db.audit_rows(500) if r["action"] == action]

    def test_roles(self):
        e = self.e
        self.assertEqual(e.req("GET", "/v1/cameras", who="ivan").status_code, 200)
        self.assertEqual(e.req("GET", "/v1/cameras", who="dasha").status_code, 200)
        self.assertEqual(e.req("GET", "/v1/cameras", who="ola").status_code, 403)       # read-only watcher
        self.assertEqual(e.req("GET", "/v1/cameras", who="bob").status_code, 403)       # household without the parent flag
        self.assertEqual(e.req("GET", "/v1/cameras", who="gus").status_code, 403)       # admin without the parent flag
        self.assertEqual(e.req("GET", "/v1/cameras", token="", who="nobody").status_code, 401)
        e.g2.requests.clear()
        for path in ("/v1/cameras/cam0/snapshot", "/v1/cameras/cam0/live", "/v1/cameras/archive/days", "/v1/cameras/archive/clips?day=2026-11-12", "/v1/cameras/archive/clips/1/video"):
            self.assertEqual(e.req("GET", path, who="ola").status_code, 403, path)
            self.assertEqual(e.req("GET", path, who="bob").status_code, 403, path)
        self.assertEqual(len(e.g2.requests), 0)                                          # a refused caller never reaches go2rtc

    def test_list(self):
        r = self.e.req("GET", "/v1/cameras", who="ivan").json()
        self.assertEqual([c["id"] for c in r["cameras"]], ["cam0", "cam1", "cam2"])
        self.assertTrue(r["live"])
        self.assertEqual(r["archive"]["clips"], 5)
        self.assertEqual(r["archive"]["newest_clip"], "2026-11-12 11:00:00")
        caps = self.e.req("GET", "/v1/capabilities", who="ivan").json()
        self.assertTrue(caps["modules"]["cameras"])

    def test_module_off(self):
        cams = self.e.o["cameras"]
        keep, cams.base = cams.base, ""
        try:
            self.assertEqual(self.e.req("GET", "/v1/cameras", who="ivan").status_code, 404)
        finally:
            cams.base = keep

    def test_snapshot_and_audit_throttle(self):
        e = self.e
        r = e.req("GET", "/v1/cameras/cam1/snapshot", who="ivan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(r.content, JPEG)
        self.assertIn("src=cam_ch1", e.g2.requests[-1])
        e.req("GET", "/v1/cameras/cam1/snapshot", who="ivan")
        self.assertEqual(len([a for a in self.audit("camera_snapshot") if a["target"] == "cam1"]), 1)    # not one row per refresh
        self.assertEqual(e.req("GET", "/v1/cameras/nope/snapshot", who="ivan").status_code, 404)
        e.g2.down = True
        self.assertEqual(e.req("GET", "/v1/cameras/cam0/snapshot", who="ivan").status_code, 503)

    def test_live(self):
        e = self.e
        live_rows = len(self.audit("camera_live"))
        r = e.req("GET", "/v1/cameras/cam0/live", who="ivan")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "video/mp4")
        self.assertEqual(len(r.content), 3 * len(CHUNK))
        self.assertIn("src=cam_ch0_h264", e.g2.requests[-1])                           # the default is the transcoded stream
        e.req("GET", "/v1/cameras/cam2/live?codec=h265", who="dasha")
        self.assertIn("src=cam_ch2", e.g2.requests[-1])
        self.assertNotIn("_h264", e.g2.requests[-1])
        self.assertEqual(e.req("GET", "/v1/cameras/cam0/live?codec=vp9", who="ivan").status_code, 422)
        self.assertEqual(len(self.audit("camera_live")) - live_rows, 2)                  # every live view is audited (the 422 one is not a view)
        e.o["app"].live_open = 4
        self.assertEqual(e.req("GET", "/v1/cameras/cam0/live", who="ivan").status_code, 429)
        e.o["app"].live_open = 0
        e.g2.down = True
        self.assertEqual(e.req("GET", "/v1/cameras/cam0/live", who="ivan").status_code, 503)
        self.assertEqual(e.o["app"].live_open, 0)                                        # a failed open does not leak a slot

    def test_live_retries_a_cold_start_once(self):
        e = self.e
        e.g2.fail_stream = 1
        r = e.req("GET", "/v1/cameras/cam1/live?codec=h265", who="ivan")
        self.assertEqual(r.status_code, 200)                                              # the first open failed, the second worked
        self.assertEqual(len([q for q in e.g2.requests if "/api/stream.mp4" in q]), 2)
        e.g2.fail_stream = 3
        self.assertEqual(e.req("GET", "/v1/cameras/cam1/live?codec=h265", who="ivan").status_code, 503)    # three failures in a row are reported, not retried for ever
        self.assertEqual(e.o["app"].live_open, 0)

    def test_h264_attaches_a_plain_consumer_first(self):
        e = self.e
        e.g2.requests.clear()
        r = e.req("GET", "/v1/cameras/cam1/live", who="ivan")                              # default codec = h264
        self.assertEqual(r.status_code, 200)
        streams = [urllib.parse.parse_qs(urllib.parse.urlparse(q).query)["src"][0] for q in e.g2.requests if "/api/stream.mp4" in q]
        self.assertEqual(streams, ["cam_ch1", "cam_ch1_h264"])                            # a cold source cannot be transcoded: the plain stream holds it first
        e.g2.requests.clear()
        e.g2.fail_stream = 1                                                              # the plain open fails once, is retried, then the transcode opens
        self.assertEqual(e.req("GET", "/v1/cameras/cam2/live", who="ivan").status_code, 200)
        e.g2.fail_stream = 3                                                              # the plain stream never starts: 503, no transcode attempted, no slot leaked
        e.g2.requests.clear()
        self.assertEqual(e.req("GET", "/v1/cameras/cam2/live", who="ivan").status_code, 503)
        self.assertFalse([q for q in e.g2.requests if "_h264" in q])
        self.assertEqual(e.o["app"].live_open, 0)

    def test_archive_lists(self):
        e = self.e
        days = e.req("GET", "/v1/cameras/archive/days", who="ivan").json()["days"]
        self.assertEqual([d["day"] for d in days], ["2026-11-12", "2026-11-11"])
        self.assertEqual(days[0]["by_channel"], {"cam0": 3, "cam1": 1})
        clips = e.req("GET", "/v1/cameras/archive/clips?day=2026-11-12", who="ivan").json()["clips"]
        self.assertEqual([c["begin"][11:] for c in clips], ["09:00:00", "09:10:00", "10:00:00", "11:00:00"])
        only1 = e.req("GET", "/v1/cameras/archive/clips?day=2026-11-12&camera=cam1", who="ivan").json()["clips"]
        self.assertEqual([c["camera"] for c in only1], ["cam1"])
        self.assertEqual(e.req("GET", "/v1/cameras/archive/clips?day=yesterday", who="ivan").status_code, 400)
        self.assertEqual(e.req("GET", "/v1/cameras/archive/clips?day=2026-11-12&camera=nope", who="ivan").status_code, 404)

    def test_clip_playback_ranges_and_safety(self):
        e = self.e
        clips = e.req("GET", "/v1/cameras/archive/clips?day=2026-11-12", who="ivan").json()["clips"]
        by = {c["begin"][11:]: c["id"] for c in clips}
        full = e.req("GET", "/v1/cameras/archive/clips/%d/video" % by["09:00:00"], who="ivan")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(len(full.content), 5000)
        part = e.req("GET", "/v1/cameras/archive/clips/%d/video" % by["09:00:00"], who="dasha", headers={"Range": "bytes=100-199"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(len(part.content), 100)
        self.assertEqual(part.content, full.content[100:200])
        self.assertTrue(part.headers["content-range"].startswith("bytes 100-199/"))
        plays = self.audit("camera_clip_play")
        self.assertEqual(len(plays), 1)                                                  # the first request audited, a later seek is not another play
        self.assertEqual(e.req("GET", "/v1/cameras/archive/clips/%d/video" % by["10:00:00"], who="ivan").status_code, 404)    # row points outside the archive
        self.assertEqual(e.req("GET", "/v1/cameras/archive/clips/%d/video" % by["11:00:00"], who="ivan").status_code, 404)    # file is gone
        self.assertEqual(e.req("GET", "/v1/cameras/archive/clips/99999/video", who="ivan").status_code, 404)


class Page(BaseCase):
    def login(self, who, ip):
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", (who,))
        r = self.e.client.post("/login", data={"username": who, "password": PASSWORD, "totp": auth.totp_now(self.e.seeds[who])}, headers={"X-Forwarded-For": ip}, follow_redirects=False)
        m = re.search(r"gw_session=([^;]+)", r.headers.get("set-cookie", ""))
        return {"Cookie": "gw_session=" + m.group(1)}

    def get(self, path, ck=None):
        return self.e.client.get(path, headers=ck or {}, follow_redirects=False)

    def test_page_for_parents_only_with_the_media_csp_and_the_tab(self):
        ck = self.login("marta", "6.6.6.1")
        r = self.get("/cameras", ck)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="cam-root"', r.text)
        self.assertIn('src="/static/cameras.js"', r.text)
        self.assertIn("media-src 'self'", r.headers["content-security-policy"])
        self.assertIn('href="/cameras"', self.get("/home", ck).text)                       # the tab appears for a parent
        ck2 = self.login("kid-sitter", "6.6.6.2")
        self.assertEqual(self.get("/cameras", ck2).status_code, 403)
        self.assertNotIn('href="/cameras"', self.get("/home", ck2).text)
        self.assertEqual(self.get("/cameras").status_code, 303)                            # no session: to the login page
        self.assertEqual(self.get("/cameras", self.login("webadmin", "6.6.6.3")).status_code, 200)

    def test_page_is_clean_and_off_when_the_module_is_off(self):
        ck = self.login("marta", "6.6.6.4")
        html = self.get("/cameras", ck).text
        self.assertNotRegex(html, r"\sstyle\s*=")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertNotRegex(html, r"https?://")
        cams = self.e.o["cameras"]
        keep, cams.base, config.FEATURES["cameras"] = cams.base, "", False
        try:
            self.assertEqual(self.get("/cameras", ck).status_code, 404)
            self.assertNotIn('href="/cameras"', self.get("/home", ck).text)
        finally:
            cams.base, config.FEATURES["cameras"] = keep, True

    def test_the_script_builds_the_page_without_innerhtml_and_every_text_key_exists(self):
        r = self.e.client.get("/static/cameras.js")
        self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/javascript"))
        self.assertNotIn("innerHTML", r.text)
        self.assertNotIn("eval(", r.text)
        self.assertNotIn(".style.", r.text.replace("// ", ""))                              # no inline styling from script either
        from gw.strings import S, ERRORS
        used = set(re.findall(r'\bT\("([a-z0-9_]+)"', r.text))
        self.assertGreater(len(used), 12)
        self.assertEqual(sorted(k for k in used if k not in S), [], "strings the page uses but the table lacks")
        for k in ("camera_unavailable", "too_many_streams", "not_available"):
            self.assertIn(k, ERRORS)
        for k in S:
            if k.startswith("cam_"):
                self.assertNotRegex(S[k], r"[A-Za-z]{3,}", k)                                 # plain Russian for the parents: no Latin words


if __name__ == "__main__":
    unittest.main()
