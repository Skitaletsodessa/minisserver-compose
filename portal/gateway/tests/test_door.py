"""Task 34 phase 4 / ADR 0009 amendment "single app gate": the door table walked against the REAL route table. Requests that carry the door header (what Caddy's public site
adds) must reach only the routes flagged allow, and only with an APP credential; everything else, and every route nobody classified, is 404."""
import os
import re
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, PASSWORD  # noqa: E402
from gw import auth, door  # noqa: E402
from test_cameras import FakeGo2rtc, make_archive  # noqa: E402

DOOR = {"X-Portal-Door": "funnel"}
SAMPLE = {"id": "x", "xid": "x", "rid": "x", "token": "t", "scenario": "x", "code": "1", "cid": "1", "path": "app.css", "name": "icon-192.png"}


def concrete(template):
    return re.sub(r"\{([a-z_]+)(?::[a-z]+)?\}", lambda m: SAMPLE[m.group(1)], template)


def setUpModule():
    g = FakeGo2rtc()
    root = os.path.join(tempfile.mkdtemp(prefix="gw-door-"), "camera")
    make_archive(root)
    cams = [{"id": "cam0", "channel": 0, "src": "cam_ch0", "name": "A"}, {"id": "cam1", "channel": 1, "src": "cam_ch1", "name": "B"}]
    e = BaseCase.ENV = Env(camera_opts={"go2rtc_url": g.url, "archive_dir": root, "channels": cams})
    e.g2 = g
    a = {"person_id": "test", "phone_id": None, "ip": "t"}
    e.people.create(a, "Webadmin", "admin", True, web_login=True)                    # one person with BOTH a web login and a phone (device token)
    raw, _ = e.people.create_setup_link(a, e.people.get("webadmin")["id"])
    _p, seed = e.people.setup_begin(raw)
    e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed), "1.1.1.1")
    e.seed = seed
    code, _ = e.people.create_enrolment_code(a, "webadmin")
    e.tokens["webadmin"] = e.people.enrol(code, {"label": "Webadmin-phone", "model": "Test"}, "10.0.0.1")["token"]


def tearDownModule():
    BaseCase.ENV.g2.stop()
    BaseCase.ENV.close()


class Gate(BaseCase):
    _ip = 0

    def ip(self):
        Gate._ip += 1
        return "8.8.%d.%d" % (Gate._ip // 250, Gate._ip % 250 + 1)

    def web_cookie(self):
        e = self.e
        e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", ("webadmin",))
        r = e.client.post("/login", data={"username": "webadmin", "password": PASSWORD, "totp": auth.totp_now(e.seed)}, headers={"X-Forwarded-For": self.ip()}, follow_redirects=False)
        return {"Cookie": "gw_session=" + re.search(r"gw_session=([^;]+)", r.headers["set-cookie"]).group(1)}

    def app_cookie(self):
        r = self.e.req("POST", "/v1/app/session", who=None, token=self.e.tokens["webadmin"], idem=False, validate=False)
        self.assertEqual(r.status_code, 200)
        return {"Cookie": "gw_session=" + re.search(r"gw_session=([^;]+)", r.headers["set-cookie"]).group(1)}

    def call(self, method, path, headers=None, **kw):
        return self.e.client.request(method, path, headers=headers or {}, follow_redirects=False, timeout=10, **kw)

    @staticmethod
    def door_404(r):
        return r.status_code == 404 and r.headers.get("content-type", "").startswith("text/plain") and r.text == "not found" and "x-powered" not in r.headers

    # ── the table against the real routes ──
    def test_every_real_route_is_classified_and_nothing_stale(self):
        real = set()
        for r in self.e.app.routes:
            for m in (r.methods or ()):
                if m != "HEAD":
                    real.add((m, r.path))
        table = door.templates()
        self.assertEqual(sorted(real - table), [], "routes with no door flag (they would be denied; classify them in gw/door.py)")
        self.assertEqual(sorted(table - real), [], "door entries for routes that do not exist")
        self.assertEqual(len(door.TABLE), len(table), "duplicate door entries")

    def test_default_deny_and_most_specific_first(self):
        self.assertEqual(door.decide("GET", "/v1/brand-new-thing")[0], "deny")
        self.assertEqual(door.decide("POST", "/v1/cameras")[0], "deny")                      # right path, wrong method
        self.assertEqual(door.decide("GET", "/app/download")[0], "deny")                    # the specific entry beats the generic /app/{name}
        self.assertEqual(door.decide("GET", "/app/icon-192.png")[0], "allow")
        self.assertEqual(door.decide("GET", "/app/releases")[0], "deny")
        self.assertEqual(door.decide("GET", "/app/releases/4/app.apk")[0], "allow")
        r = self.call("GET", "/v1/brand-new-thing", headers=DOOR)
        self.assertTrue(self.door_404(r))

    # ── through the door ──
    def test_denied_routes_are_404_for_every_credential(self):
        web, app = self.web_cookie(), self.app_cookie()
        bearer = {"Authorization": "Bearer " + self.e.tokens["webadmin"]}
        n = 0
        for m, tmpl, d, a, _r in door.TABLE:
            if d != "deny":
                continue
            path = concrete(tmpl)
            for creds in ({}, web, app, bearer):
                r = self.call(m, path, headers={**DOOR, **creds})
                self.assertTrue(self.door_404(r), (m, path, sorted(creds), r.status_code, r.text[:60]))
                n += 1
        self.assertGreater(n, 150)

    def test_the_door_refuses_a_web_session_on_every_app_route_and_answers_an_app_session(self):
        web, app = self.web_cookie(), self.app_cookie()
        n = 0
        for m, tmpl, d, a, _r in door.TABLE:
            if d != "allow" or a != "app" or tmpl == "/v1/events":
                continue
            path = concrete(tmpl)
            r = self.call(m, path, headers={**DOOR, **web, "X-CSRF-Token": "x"})
            self.assertIn(r.status_code, (403, 303), (m, path, r.status_code))                 # JSON: 403 door_requires_app_session; pages: back to /login (also closed at the door)
            if r.status_code == 403 and r.headers.get("content-type", "").startswith("application/json"):
                self.assertEqual(r.json()["error"]["code"], "door_requires_app_session", (m, path))
            r = self.call(m, path, headers={**DOOR})
            self.assertIn(r.status_code, (401, 303), (m, path, r.status_code))                 # no credential at all
            r = self.call(m, path, headers={**DOOR, **app, "X-CSRF-Token": "x"})
            self.assertFalse(self.door_404(r), (m, path))                                      # past the door: the route's own rules decide (403 csrf, 404 unknown id, 200 ...)
            self.assertNotEqual(r.json()["error"]["code"] if r.headers.get("content-type", "").startswith("application/json") and r.status_code >= 400 else "", "door_requires_app_session", (m, path))
            n += 1
        self.assertEqual(n, sum(1 for e in door.TABLE if e[2] == "allow" and e[3] == "app" and e[1] != "/v1/events"))

    def test_a_device_token_passes_the_door_and_the_tailnet_is_unchanged(self):
        bearer = {"Authorization": "Bearer " + self.e.tokens["webadmin"]}
        self.assertEqual(self.call("GET", "/v1/capabilities", headers={**DOOR, **bearer}).status_code, 200)
        web = self.web_cookie()
        self.assertEqual(self.call("GET", "/v1/capabilities", headers=web).status_code, 200)       # no door header = the tailnet: a web session works as before
        self.assertEqual(self.call("GET", "/v1/capabilities", headers={**DOOR, **web}).status_code, 403)
        self.assertEqual(self.call("GET", "/v1/people", headers={**bearer}).status_code, 200)       # an admin route works on the tailnet ...
        self.assertTrue(self.door_404(self.call("GET", "/v1/people", headers={**DOOR, **bearer})))  # ... and is invisible through the door

    def test_the_open_paths_of_the_gate_work_without_a_credential(self):
        for path in ("/static/app.css", "/app/manifest.webmanifest", "/app/icon-192.png"):
            r = self.call("GET", path, headers=DOOR)
            self.assertEqual(r.status_code, 200, path)
        r = self.call("POST", "/v1/app/login", headers={**DOOR, "X-Forwarded-For": self.ip()}, json={"username": "nobody", "password": "x" * 14, "device": {"name": "Pixel", "model": "Pixel 8", "app_version": "1.0.0"}})
        self.assertEqual(r.status_code, 401)                                                           # reached the handler: the same refusal as ever
        self.assertEqual(self.call("GET", "/app/update.json", headers=DOOR).status_code, 401)           # the update path answers its own 401 to an anonymous caller

    def test_camera_calls_from_outside_are_counted_per_person_and_device(self):
        e = self.e
        app = self.app_cookie()
        keep = dict(door.DOOR_LIMITS)
        door.DOOR_LIMITS["camera_live"] = (2, 600)
        door.DOOR_LIMITS["camera_snapshot"] = (3, 600)
        e.o["app"].door_hits.clear()
        try:
            codes = [self.call("GET", "/v1/cameras/cam0/live?codec=h265", headers={**DOOR, **app}).status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
            codes = [self.call("GET", "/v1/cameras/cam0/snapshot", headers={**DOOR, **app}).status_code for _ in range(4)]
            self.assertEqual(codes, [200, 200, 200, 429])
            r = self.call("GET", "/v1/cameras/cam0/live?codec=h265", headers={"Authorization": "Bearer " + e.tokens["webadmin"]})          # the tailnet is not counted
            self.assertEqual(r.status_code, 200)
            r = self.call("GET", "/v1/cameras/cam0/live?codec=h265", headers={**DOOR, **app})
            self.assertEqual(r.json()["error"]["code"], "door_rate_limited")
        finally:
            door.DOOR_LIMITS.clear()
            door.DOOR_LIMITS.update(keep)
            e.o["app"].door_hits.clear()

    def test_the_full_quality_picture_is_tailnet_only(self):
        e = self.e
        e.o["app"].door_hits.clear()
        app = self.app_cookie()
        r = self.call("GET", "/v1/cameras/cam0/live?quality=hd", headers={**DOOR, **app})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "hd_tailnet_only"))
        self.assertEqual(self.call("GET", "/v1/cameras/cam0/live?quality=hd&codec=h265", headers={"Authorization": "Bearer " + e.tokens["webadmin"]}).status_code, 200)
        self.assertEqual(self.call("GET", "/v1/cameras/cam0/live?codec=h265", headers={**DOOR, **app}).status_code, 200)               # the sub stream is what the door carries
        self.assertFalse(self.call("GET", "/v1/cameras", headers={**DOOR, **app}).json()["hd_available"])                                # the page hides the button through the door
        e.o["app"].door_hits.clear()

    def test_the_first_camera_call_from_outside_is_recorded_once_and_reported_in_health(self):
        e = self.e
        e.db.x("DELETE FROM camera_outside")
        tok = {"Authorization": "Bearer " + e.tokens["webadmin"]}
        self.call("GET", "/v1/cameras", headers=tok)                                         # the tailnet: not an outside call
        self.assertEqual(e.req("GET", "/v1/health", who="webadmin").json()["camera_outside_new"], [])
        app = self.app_cookie()
        for _ in range(3):
            self.assertEqual(self.call("GET", "/v1/cameras", headers={**DOOR, **app}).status_code, 200)
        rows = e.req("GET", "/v1/health", who="webadmin").json()["camera_outside_new"]
        self.assertEqual(len(rows), 1)                                                       # once per device, however many calls
        self.assertEqual(rows[0]["person"], "Webadmin")
        self.assertTrue(rows[0]["id"].startswith("p-") or rows[0]["id"])

    def test_caddy_prefixes_cover_every_allowed_route_and_no_denied_one(self):
        pre = door.caddy_prefixes()
        self.assertIn("/v1/cameras", pre)
        self.assertIn("/v1/cameras/*", pre)
        self.assertIn("/v1/app/login", pre)
        for m, tmpl, d, a, _r in door.TABLE:
            path = concrete(tmpl)
            covered = any(path == p or (p.endswith("/*") and path.startswith(p[:-1])) or (p.endswith("*") and not p.endswith("/*") and path.startswith(p[:-1])) for p in pre)
            if d == "allow":
                self.assertTrue(covered, ("an allowed route is not forwarded by Caddy", m, tmpl))
        for bad in ("/login", "/v1/people", "/v1/audit", "/v1/auth/login", "/app/download", "/v1/health"):
            self.assertFalse(any(bad == p for p in pre), bad)


if __name__ == "__main__":
    unittest.main()
