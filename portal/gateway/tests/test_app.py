"""Task 30 (ADR 0009): the app's own sign-in (POST /v1/app/login: the SAME check as the web login), the device-token -> web-session exchange (POST /v1/app/session),
signing a device out (POST /v1/app/logout), the rule that admin-only actions stay behind the web sign-in, and the PWA files. Synthetic accounts only."""
import json
import logging
import os
import re
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import httpx  # noqa: E402
from helpers import Env, BaseCase, PASSWORD  # noqa: E402
from gw import auth  # noqa: E402

A = {"person_id": "test", "phone_id": None, "ip": "t"}
DEV = {"name": "Pixel Ivan", "model": "Pixel 8", "app_version": "1.0.0"}


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def setUpModule():
    e = BaseCase.ENV = Env()
    e.__dict__.setdefault("seeds", {})
    # admin with web credentials (password + TOTP), household parent WITHOUT an authenticator (the code is optional for them), a household person whose web login is off,
    # a household non-parent with web credentials and a throw-away account for the lockout test
    for name, role, parent, web, totp in (("Webadmin", "admin", True, True, True), ("Marta", "household", True, True, False), ("Nologin", "household", True, False, False),
                                          ("Sitter", "household", False, True, False), ("Lockme", "household", True, True, False)):
        e.people.create(A, name, role, parent, web_login=web)
        pid = name.lower()
        if web:
            raw, _ = e.people.create_setup_link(A, pid)
            _p, seed = e.people.setup_begin(raw)
            e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed) if totp else "", "1.1.1.1")
            if totp:
                e.seeds[pid] = seed


def tearDownModule():
    BaseCase.ENV.close()


class AppBase(BaseCase):
    _ip = 0

    def setUp(self):
        super().setUp()
        # isolation: devices made by earlier tests would hit the per-person limit, and wrong passwords of earlier tests would delay later correct ones
        self.e.db.x("UPDATE phones SET revoked=1 WHERE id NOT IN (%s)" % ",".join("'%s'" % i for i in self.e.people_ids.values()))
        self.e.db.x("DELETE FROM sessions")
        self.e.db.x("DELETE FROM login_attempts")

    def ip(self):
        AppBase._ip += 1
        return "7.7.%d.%d" % (AppBase._ip // 250, AppBase._ip % 250 + 1)

    def code(self, who):
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", (who,))
        return auth.totp_now(self.e.seeds[who])

    def app_login(self, who="webadmin", password=PASSWORD, totp="auto", device=DEV, ip=None, validate=True):
        body = {"username": who, "password": password, "device": device}
        if totp == "auto" and self.e.seeds.get(who):
            totp = self.code(who)
        if totp not in (None, "auto"):
            body["totp"] = totp
        return self.e.req("POST", "/v1/app/login", who=None, token="", json=body, headers={"X-Forwarded-For": ip or self.ip()}, idem=False, validate=validate)

    def signed_in(self, who="webadmin"):
        r = self.app_login(who)
        self.assertEqual(r.status_code, 201, r.text)
        self.e.tokens["app-" + who + "-" + r.json()["phone_id"]] = r.json()["token"]            # so assert_no_secrets scans for it too
        return r.json()

    def app_session(self, token, validate=True):
        r = self.e.req("POST", "/v1/app/session", who=None, token=token, idem=False, validate=validate)
        ck = None
        m = re.search(r"gw_session=([^;]+)", r.headers.get("set-cookie", ""))
        if m:
            ck = {"Cookie": "gw_session=" + m.group(1)}
        return r, ck

    def with_cookie(self, method, path, ck, csrf=None, **kw):
        h = dict(ck)
        if csrf:
            h["X-CSRF-Token"] = csrf
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            h["Idempotency-Key"] = "k-" + auth.token(8)
        r = self.e.client.request(method, path, headers=h, follow_redirects=False, **kw)
        return r


class AppLogin(AppBase):
    def test_admin_signs_in_with_name_password_and_code_and_gets_a_working_device_token(self):
        res = self.signed_in("webadmin")
        self.assertEqual((res["person"]["id"], res["person"]["role"], res["person"]["parent"]), ("webadmin", "admin", True))
        self.assertTrue(res["token"] and res["phone_id"].startswith("p-"))
        r = self.e.req("GET", "/v1/capabilities", who=None, token=res["token"])
        self.assertEqual(r.status_code, 200)
        row = self.e.db.q1("SELECT platform, model, app_version, label FROM phones WHERE id=?", (res["phone_id"],))
        self.assertEqual((row["platform"], row["model"], row["app_version"], row["label"]), ("android", "Pixel 8", "1.0.0", "Pixel Ivan"))

    def test_the_token_is_shown_once_and_only_its_hash_is_stored(self):
        res = self.signed_in("webadmin")
        row = self.e.db.q1("SELECT token_hash FROM phones WHERE id=?", (res["phone_id"],))
        self.assertNotEqual(row["token_hash"], res["token"])
        self.assertEqual(row["token_hash"], auth.sha(res["token"]))
        self.assertNotIn(res["token"], json.dumps([dict(r) for r in self.e.db.audit_rows(50)]))

    def test_admin_without_the_code_or_with_a_wrong_one_is_refused_exactly_like_a_wrong_password(self):
        bodies = []
        for kw in ({"totp": None}, {"totp": "000000"}, {"password": "wrong password wrong"}, {"who": "nobody-here"}):
            r = self.app_login(**({"who": "webadmin"} | kw))
            self.assertEqual(r.status_code, 401, kw)
            bodies.append(r.json())
        self.assertTrue(all(b == bodies[0] for b in bodies), "the answers differ: they would tell the attacker which part was wrong")
        self.assertEqual(bodies[0]["error"]["code"], "invalid_credentials")

    def test_a_household_person_signs_in_without_an_authenticator_code(self):
        r = self.app_login("marta", totp=None)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["person"]["role"], "household")
        self.e.tokens["app-marta"] = r.json()["token"]

    def test_a_person_whose_web_login_is_off_cannot_sign_in_until_an_admin_turns_it_on(self):
        self.e.db.x("INSERT OR REPLACE INTO credentials(person_id,pw_hash,totp_seed,totp_last_step,updated) VALUES ('nologin',?,NULL,0,0)", (auth.hash_password(PASSWORD),))
        r = self.app_login("nologin", totp=None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "invalid_credentials"))
        self.e.people.patch(A, "nologin", {"web_login_enabled": True})
        r = self.app_login("nologin", totp=None)
        self.assertEqual(r.status_code, 201)
        self.e.tokens["app-nologin"] = r.json()["token"]
        self.e.people.patch(A, "nologin", {"web_login_enabled": False})

    def test_an_account_locked_out_by_wrong_passwords_is_locked_out_for_the_app_too(self):
        for _ in range(5):
            self.app_login("lockme", password="bad password 000000", totp=None)
        r = self.app_login("lockme", totp=None)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "account_delay"))
        self.assertIn("Retry-After", r.headers)
        # the web login of the same account is delayed by the same counter: one check, not two
        w = self.e.client.post("/v1/auth/login", json={"username": "lockme", "password": PASSWORD}, headers={"X-Forwarded-For": self.ip()})
        self.assertEqual(w.status_code, 429)

    def test_too_many_failures_from_one_address_are_refused_whatever_the_account(self):
        ip = "9.9.9.9"
        for i in range(20):
            self.app_login("ghost%d" % i, password="bad password 000000", totp=None, ip=ip)
        r = self.app_login("webadmin", ip=ip)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "too_many_attempts"))

    def test_the_device_limit_stops_a_sixth_phone_and_says_so(self):
        for i in range(5):
            r = self.app_login("sitter", totp=None, device={"name": "phone %d" % i})
            self.assertEqual(r.status_code, 201, r.text)
            self.e.tokens["app-sitter-%d" % i] = r.json()["token"]
        r = self.app_login("sitter", totp=None, device={"name": "phone 6"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "device_limit"))
        for ph in self.e.people.phones("sitter"):
            self.e.people.revoke_phone({"role": "admin", "person_id": "webadmin"}, ph["id"])

    def test_the_device_name_is_required(self):
        r = self.e.req("POST", "/v1/app/login", who=None, token="", json={"username": "marta", "password": PASSWORD, "device": {}}, headers={"X-Forwarded-For": self.ip()}, idem=False)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_device"))
        r = self.e.req("POST", "/v1/app/login", who=None, token="", json={"username": "marta"}, headers={"X-Forwarded-For": self.ip()}, idem=False)
        self.assertEqual(r.status_code, 400)

    def test_the_audit_records_who_and_where_but_no_secret(self):
        self.signed_in("webadmin")
        self.app_login("webadmin", password="bad bad bad bad bad", totp=None)
        rows = [dict(r) for r in self.e.db.audit_rows(20)]
        ok = [r for r in rows if r["action"] == "app_login"]
        bad = [r for r in rows if r["action"] == "login_failed"]
        self.assertTrue(ok and ok[0]["person"] == "webadmin" and ok[0]["source_ip"] and ok[0]["device"])
        self.assertTrue(bad)
        text = json.dumps(rows)
        for secret in (PASSWORD, "bad bad bad bad bad", self.e.seeds["webadmin"]):
            self.assertNotIn(secret, text)

    def test_nothing_secret_reaches_the_log_lines_of_the_login_and_session_calls(self):
        h = Logs()
        root = logging.getLogger()
        h.addFilter(lambda rec: not rec.name.startswith(("httpx", "httpcore")))          # the gateway's own lines; the test client's debug output is not the gateway
        root.addHandler(h)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            res = self.signed_in("webadmin")
            r, ck = self.app_session(res["token"])
            cookie = ck["Cookie"].split("=", 1)[1]
            self.app_login("webadmin", password="bad bad bad bad bad", totp=None)
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        blob = "\n".join(h.lines)
        for secret in (res["token"], cookie, PASSWORD, "bad bad bad bad bad"):
            self.assertNotIn(secret, blob)

    def test_the_new_device_is_listed_for_the_watcher_once_it_exists(self):
        r = self.app_login("marta", totp=None, device={"name": "Marta phone", "model": "Galaxy A55"})
        self.assertEqual(r.status_code, 201)
        self.e.tokens["app-marta-health"] = r.json()["token"]
        h = self.e.req("GET", "/v1/health", who="ivan")
        nd = [d for d in h.json()["new_devices"] if d["id"] == r.json()["phone_id"]]
        self.assertEqual(len(nd), 1)
        self.assertEqual((nd[0]["person"], nd[0]["device"], nd[0]["model"]), ("Marta", "Marta phone", "Galaxy A55"))
        self.assertEqual(self.e.req("GET", "/v1/health", who=None, token="").json(), {"status": "ok"})          # nothing about people without credentials


class AppSession(AppBase):
    def test_a_valid_token_gives_a_session_for_that_person_with_a_cookie_and_a_csrf_token(self):
        res = self.signed_in("webadmin")
        r, ck = self.app_session(res["token"])
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ck)
        sc = r.headers["set-cookie"].lower()
        self.assertIn("httponly", sc)
        self.assertIn("secure", sc)
        self.assertIn("samesite=strict", sc)
        self.assertEqual(r.json()["person"]["id"], "webadmin")
        self.assertTrue(r.json()["csrf"] and r.json()["app"] is True)
        page = self.with_cookie("GET", "/home", ck)
        self.assertEqual(page.status_code, 200)
        cap = self.with_cookie("GET", "/v1/capabilities", ck)
        self.assertEqual((cap.status_code, cap.json()["app_session"], cap.json()["can"]["manage_people"]), (200, True, False))

    def test_household_without_web_login_still_gets_an_app_session(self):
        # Dasha's case: the person may have no web session at all; the device token is what counts
        self.e.people.patch(A, "marta", {"web_login_enabled": True})
        res = self.signed_in("marta")
        self.e.people.patch(A, "marta", {"web_login_enabled": False})
        r, ck = self.app_session(res["token"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 200)
        self.e.people.patch(A, "marta", {"web_login_enabled": True})

    def test_no_token_a_wrong_token_or_a_session_cookie_instead_of_the_token_is_refused(self):
        self.assertEqual(self.e.req("POST", "/v1/app/session", who=None, token="", idem=False).status_code, 401)
        self.assertEqual(self.e.req("POST", "/v1/app/session", who=None, token="not-a-token", idem=False, headers={"X-Forwarded-For": self.ip()}).status_code, 401)
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        r = self.e.client.post("/v1/app/session", headers=ck)
        self.assertEqual(r.status_code, 401)

    def test_an_expired_token_gets_no_session(self):
        res = self.signed_in("webadmin")
        self.e.db.x("UPDATE phones SET expires=? WHERE id=?", (time.time() - 5, res["phone_id"]))
        r, ck = self.app_session(res["token"])
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(ck)

    def test_a_revoked_token_gets_no_session(self):
        res = self.signed_in("webadmin")
        self.e.people.revoke_phone({"role": "admin", "person_id": "webadmin"}, res["phone_id"])
        self.assertEqual(self.app_session(res["token"])[0].status_code, 401)

    def test_a_session_belongs_to_the_person_of_the_token_and_to_nobody_else(self):
        a, b = self.signed_in("webadmin"), self.signed_in("marta")
        ra, cka = self.app_session(a["token"])
        rb, ckb = self.app_session(b["token"])
        self.assertEqual((ra.json()["person"]["id"], rb.json()["person"]["id"]), ("webadmin", "marta"))
        self.assertNotEqual(cka, ckb)
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ckb).json()["person"]["id"], "marta")

    def test_revoking_the_device_ends_the_open_session_on_its_next_request(self):
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 200)
        self.e.people.revoke_phone({"role": "admin", "person_id": "webadmin"}, res["phone_id"])
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 401)
        page = self.with_cookie("GET", "/home", ck)
        self.assertEqual((page.status_code, page.headers["location"]), (303, "/login"))

    def test_revoking_the_device_closes_an_open_event_stream(self):
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        done, status = threading.Event(), []

        def run():
            try:
                with httpx.Client(base_url=self.e.base, timeout=httpx.Timeout(30, read=30)) as c:
                    with c.stream("GET", "/v1/events", headers=ck) as s:
                        status.append(s.status_code)
                        for _line in s.iter_lines():
                            pass
            except Exception:                                  # noqa: BLE001
                pass
            finally:
                done.set()
        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(self.e.wait(lambda: status == [200], 5))
        self.e.people.revoke_phone({"role": "admin", "person_id": "webadmin"}, res["phone_id"])
        self.assertTrue(done.wait(12), "the stream stayed open after the device was revoked")

    def test_a_deactivated_person_loses_the_app_session_too(self):
        self.e.people.create(A, "Temp Person", "household", False, web_login=True)
        self.e.db.x("INSERT OR REPLACE INTO credentials(person_id,pw_hash,totp_seed,totp_last_step,updated) VALUES ('temp-person',?,NULL,0,0)", (auth.hash_password(PASSWORD),))
        res = self.signed_in("temp-person")
        _r, ck = self.app_session(res["token"])
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 200)
        self.e.people.patch(A, "temp-person", {"active": False})
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 401)

    def test_the_csrf_header_is_required_for_state_changes_from_an_app_session(self):
        res = self.signed_in("webadmin")
        r, ck = self.app_session(res["token"])
        no = self.with_cookie("POST", "/v1/devices/plant_rack1/switch", ck, json={"on": False})
        self.assertEqual((no.status_code, no.json()["error"]["code"]), (403, "csrf"))
        yes = self.with_cookie("POST", "/v1/devices/plant_rack1/switch", ck, csrf=r.json()["csrf"], json={"on": False})
        self.assertEqual(yes.status_code, 200, yes.text)

    def test_the_household_role_without_the_parent_flag_cannot_reach_the_parental_pages_from_the_app(self):
        res = self.signed_in("sitter")
        _r, ck = self.app_session(res["token"])
        self.assertEqual(self.with_cookie("GET", "/parental", ck).status_code, 403)
        self.assertEqual(self.with_cookie("GET", "/v1/parental/children", ck).status_code, 403)
        self.assertNotIn("/parental", self.with_cookie("GET", "/home", ck).text.split('class="bottomnav"')[1])


class AppAdminOnWeb(AppBase):
    def setUp(self):
        super().setUp()
        res = self.signed_in("webadmin")
        r, self.ck = self.app_session(res["token"])
        self.csrf, self.phone = r.json()["csrf"], res["phone_id"]

    def refused(self, r):
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "web_login_required")

    def test_people_codes_setup_links_filters_and_device_management_answer_web_login_required(self):
        c, k = self.csrf, self.ck
        self.refused(self.with_cookie("GET", "/v1/people", k))
        self.refused(self.with_cookie("POST", "/v1/people", k, csrf=c, json={"name": "X Y", "role": "readonly"}))
        self.refused(self.with_cookie("PATCH", "/v1/people/marta", k, csrf=c, json={"role": "admin"}))
        self.refused(self.with_cookie("POST", "/v1/people/marta/enrolment-codes", k, csrf=c))
        self.refused(self.with_cookie("POST", "/v1/people/marta/setup-link", k, csrf=c))
        self.refused(self.with_cookie("GET", "/v1/parental/filter-lists", k))
        self.refused(self.with_cookie("GET", "/v1/parental/filter-lists/catalogue", k))
        self.refused(self.with_cookie("POST", "/v1/parental/filter-lists", k, csrf=c, json={"url": "https://x.example/l.txt"}))
        self.refused(self.with_cookie("GET", "/v1/parental/candidates", k))
        self.refused(self.with_cookie("POST", "/v1/parental/children", k, csrf=c, json={"name": "K", "ip": "192.168.31.150", "template": "closed"}))
        self.refused(self.with_cookie("DELETE", "/v1/parental/children/child_a", k, csrf=c, json={"confirm": "a"}))
        self.refused(self.with_cookie("GET", "/v1/parental/removed-children", k))

    def test_revoking_another_device_needs_the_web_sign_in_but_the_device_may_sign_itself_out(self):
        other = self.signed_in("marta")
        self.refused(self.with_cookie("DELETE", "/v1/phones/" + other["phone_id"], self.ck, csrf=self.csrf))
        self.assertEqual(self.e.req("GET", "/v1/capabilities", who=None, token=other["token"]).status_code, 200)     # the other device was NOT revoked
        own = self.with_cookie("DELETE", "/v1/phones/" + self.phone, self.ck, csrf=self.csrf)
        self.assertEqual(own.status_code, 204)

    def test_the_admin_pages_are_refused_and_their_links_and_buttons_are_not_shown(self):
        for path in ("/people", "/parental/filters"):
            r = self.with_cookie("GET", path, self.ck)
            self.assertEqual(r.status_code, 403, path)
            self.assertIn("паролем и кодом", r.text)
        home = self.with_cookie("GET", "/home", self.ck).text
        self.assertNotIn('href="/people"', home)
        self.assertIn('data-app="yes"', home)
        par = self.with_cookie("GET", "/parental", self.ck).text
        self.assertIn('data-admin="no"', par)
        self.assertNotIn("/parental/filters", par)

    def test_the_same_admin_on_the_web_still_has_all_of_it(self):
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id='webadmin'")
        r = self.e.client.post("/v1/auth/login", json={"username": "webadmin", "password": PASSWORD, "totp": auth.totp_now(self.e.seeds["webadmin"])}, headers={"X-Forwarded-For": self.ip()})
        self.assertEqual(r.status_code, 200, r.text)
        ck = {"Cookie": "gw_session=" + re.search(r"gw_session=([^;]+)", r.headers["set-cookie"]).group(1)}
        self.assertEqual(self.with_cookie("GET", "/v1/people", ck).status_code, 200)
        self.assertEqual(self.with_cookie("GET", "/v1/parental/filter-lists", ck).status_code, 200)
        self.assertIn('data-admin="yes"', self.with_cookie("GET", "/parental", ck).text)
        self.assertNotIn("data-app", self.with_cookie("GET", "/home", ck).text)


class AppLogout(AppBase):
    def test_logout_revokes_only_the_calling_device_and_its_sessions(self):
        a, b = self.signed_in("webadmin"), self.signed_in("webadmin")
        _r, cka = self.app_session(a["token"])
        _r, ckb = self.app_session(b["token"])
        r = self.e.req("POST", "/v1/app/logout", who=None, token=a["token"], idem=False)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.e.req("GET", "/v1/capabilities", who=None, token=a["token"], headers={"X-Forwarded-For": self.ip()}).status_code, 401)
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", cka).status_code, 401)
        self.assertEqual(self.e.req("GET", "/v1/capabilities", who=None, token=b["token"]).status_code, 200)
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ckb).status_code, 200)
        self.assertEqual(self.e.db.q1("SELECT revoked FROM phones WHERE id=?", (b["phone_id"],))["revoked"], 0)
        self.assertTrue([r for r in self.e.db.audit_rows(30) if r["action"] == "app_logout" and r["device"] == a["phone_id"]])

    def test_logout_needs_the_device_token_and_nothing_else(self):
        self.assertEqual(self.e.req("POST", "/v1/app/logout", who=None, token="", idem=False).status_code, 401)
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        r = self.e.client.post("/v1/app/logout", headers=ck)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.with_cookie("GET", "/v1/capabilities", ck).status_code, 200)


class Setup(AppBase):
    def link(self, pid):
        raw, _ = self.e.people.create_setup_link(A, pid)
        _p, seed = self.e.people.setup_begin(raw)
        return raw, seed

    def test_household_may_skip_the_authenticator_in_the_setup_page_the_admin_may_not(self):
        self.e.people.patch(A, "nologin", {"web_login_enabled": True})
        raw, _seed = self.link("nologin")
        page = self.e.client.get("/setup/" + raw)
        self.assertIn("по желанию", page.text)
        self.assertNotIn(" required autocomplete=\"one-time-code\"", page.text)
        r = self.e.client.post("/setup/" + raw, data={"password": PASSWORD, "code": ""}, headers={"X-Forwarded-For": self.ip()})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.e.db.q1("SELECT totp_seed FROM credentials WHERE person_id='nologin'")["totp_seed"])
        self.e.people.patch(A, "nologin", {"web_login_enabled": False})
        raw, _seed = self.link("webadmin")
        page = self.e.client.get("/setup/" + raw)
        self.assertIn("required autocomplete=\"one-time-code\"", page.text)
        r = self.e.client.post("/setup/" + raw, data={"password": PASSWORD, "code": ""}, headers={"X-Forwarded-For": self.ip()})
        self.assertEqual(r.status_code, 422)
        self.assertIsNotNone(self.e.db.q1("SELECT totp_seed FROM credentials WHERE person_id='webadmin'")["totp_seed"])      # the old authenticator is still there


class Pwa(AppBase):
    def test_the_manifest_is_a_standalone_app_that_starts_at_home_and_has_maskable_icons(self):
        r = self.e.client.get("/app/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertIn("manifest+json", r.headers["content-type"])
        m = r.json()
        self.assertEqual((m["display"], m["start_url"], m["lang"]), ("standalone", "/home", "ru"))
        self.assertEqual(m["theme_color"], "#1b6ef3")
        sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        self.assertIn(("192x192", "any"), sizes)
        self.assertIn(("512x512", "any"), sizes)
        self.assertIn(("512x512", "maskable"), sizes)
        for i in m["icons"]:
            ir = self.e.client.get(i["src"])
            self.assertEqual((ir.status_code, ir.headers["content-type"]), (200, "image/png"), i["src"])
            self.assertTrue(ir.content.startswith(b"\x89PNG"))

    def test_the_pages_link_the_manifest_and_set_a_theme_colour_and_the_csp_allows_it(self):
        r = self.e.client.get("/login")
        self.assertIn('rel="manifest" href="/app/manifest.webmanifest"', r.text)
        self.assertIn('name="theme-color"', r.text)
        self.assertIn("viewport-fit=cover", r.text)
        self.assertIn("manifest-src 'self'", r.headers["content-security-policy"])

    def test_there_is_no_service_worker_and_pages_with_family_data_are_never_cacheable(self):
        self.assertEqual(self.e.client.get("/sw.js").status_code, 404)
        self.assertEqual(self.e.client.get("/app/sw.js").status_code, 404)
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        for path in ("/home", "/parental", "/v1/devices"):
            self.assertIn("no-store", self.with_cookie("GET", path, ck).headers.get("cache-control", ""), path)

    def test_unknown_app_files_are_404(self):
        for p in ("/app/nothing.png", "/app/icon-1.png"):
            self.assertEqual(self.e.client.get(p).status_code, 404, p)

    def test_the_pages_carry_the_offline_banner_and_a_refresh_button(self):
        res = self.signed_in("webadmin")
        _r, ck = self.app_session(res["token"])
        html = self.with_cookie("GET", "/home", ck).text
        self.assertIn('id="net-banner"', html)
        self.assertIn('id="refresh"', html)
        self.assertIn("Нет связи с домом", html)


if __name__ == "__main__":
    unittest.main()
