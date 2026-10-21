"""The web UI (server-rendered, Russian): login with password + TOTP, sessions and CSRF, strict CSP, no inline script / external resource,
the setup page, the pages for each role. Synthetic accounts only."""
import os
import re
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, PASSWORD  # noqa: E402
from gw import auth  # noqa: E402

A = {"person_id": "test", "phone_id": None, "ip": "t"}


def setUpModule():
    e = BaseCase.ENV = Env()
    # a web admin ("Webadmin") and a web household account with the parent flag ("Marta"), credentials created the way the setup page does
    for name, role, parent in (("Webadmin", "admin", True), ("Marta", "household", True), ("Kid Sitter", "household", False)):
        e.people.create(A, name, role, parent, web_login=True)
        raw, _ = e.people.create_setup_link(A, e.people.get(name.lower().replace(" ", "-"))["id"])
        _p, seed = e.people.setup_begin(raw)
        e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed), "1.1.1.1")
        e.__dict__.setdefault("seeds", {})[name.lower().replace(" ", "-")] = seed


def tearDownModule():
    BaseCase.ENV.close()


class Web(BaseCase):
    _n = 0

    def code(self, who):
        Web._n += 1
        # a fresh TOTP step each time: the next 30 s window (the code of an already used step is refused)
        return auth.totp_now(self.e.seeds[who], time.time() + 30 * (Web._n + 2)), 30 * (Web._n + 2)

    def login(self, who="webadmin", password=PASSWORD, ip="5.5.5.5"):
        # the replay rule (a code works once) is tested in test_people; here we only need a valid code NOW, so the stored last step is reset
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", (who,))
        code = auth.totp_now(self.e.seeds[who])
        return self.e.client.post("/login", data={"username": who, "password": password, "totp": code}, headers={"X-Forwarded-For": ip}, follow_redirects=False)

    def cookie(self, r):
        m = re.search(r"gw_session=([^;]+)", r.headers.get("set-cookie", ""))
        return {"Cookie": "gw_session=" + m.group(1)} if m else None

    def get(self, path, ck, **kw):
        return self.e.client.get(path, headers=ck, follow_redirects=False, **kw)

    # ── login ──
    def test_login_page_and_the_headers_of_every_html_response(self):
        r = self.e.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Вход", r.text)
        csp = r.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["x-frame-options"], "DENY")

    def test_successful_login_sets_a_hardened_cookie_and_redirects(self):
        r = self.login()
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/home"))
        sc = r.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(flag, sc)
        self.assertNotIn("domain=", sc)

    def test_wrong_credentials_show_a_plain_russian_message_and_no_cookie(self):
        r = self.login(password="wrong password here 123", ip="5.5.5.6")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Неверное имя, пароль или код", r.text)
        self.assertNotIn("set-cookie", r.headers)

    def test_the_account_delay_applies_on_the_form(self):
        for _ in range(5):
            self.e.client.post("/login", data={"username": "marta", "password": "bad password 000000", "totp": "000000"}, headers={"X-Forwarded-For": "5.5.5.7"})
        r = self.e.client.post("/login", data={"username": "marta", "password": PASSWORD, "totp": "123456"}, headers={"X-Forwarded-For": "5.5.5.8"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Слишком много попыток", r.text)
        self.e.db.x("DELETE FROM login_attempts WHERE account='marta'")             # leave Marta usable for the other tests

    def test_cross_site_login_posts_are_refused(self):
        r = self.e.client.post("/login", data={"username": "webadmin", "password": PASSWORD, "totp": "000000"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)

    def test_pages_need_a_session(self):
        for p in ("/home", "/services", "/people", "/audit", "/parental", "/parental/filters", "/settings/plant_rack1"):
            r = self.e.client.get(p, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"), p)

    def test_a_device_token_is_not_a_web_session(self):
        r = self.e.client.get("/home", headers={"Authorization": "Bearer " + self.e.tokens["ivan"]}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)

    # ── the pages ──
    def test_home_for_the_admin(self):
        ck = self.cookie(self.login())
        r = self.get("/home", ck)
        self.assertEqual(r.status_code, 200)
        for text in ("Свет для растений — стеллаж 1", "Полотенцесушитель", "УФ-лампа для попугая", "Пылесос — спальня", "Пылесос — гостиная", "Включить", "Настройки"):
            self.assertIn(text, r.text)
        self.assertIn('href="/people"', r.text)
        self.assertIn('href="/parental"', r.text)
        self.assertEqual(len(re.findall(r'data-action="toggle"', r.text)), 4)                  # the dryer and the UV lamp are timed (Task 29): their On is a duration menu
        self.assertEqual(len(re.findall(r'data-action="run-menu"', r.text)), 2)
        self.assertIn('Включить на…', r.text)
        self.assertEqual(re.findall(r'data-action="run-start" data-min="([0-9]+)"', r.text), ['30', '60', '120', '60', '120', '240'])
        self.assertIn("/static/icons.svg#towel_dryer", r.text)

    def test_every_html_page_has_no_inline_script_no_inline_style_and_no_external_resource(self):
        ck = self.cookie(self.login())
        pages = ["/login", "/home", "/services", "/people", "/audit", "/parental", "/parental/filters", "/settings/plant_rack1", "/settings/vacuum_bedroom"]
        for p in pages:
            r = self.get(p, ck if p != "/login" else None)
            self.assertEqual(r.status_code, 200, p)
            html = r.text
            for m in re.finditer(r"<script\b([^>]*)>", html):
                attrs = m.group(1)
                self.assertTrue('src="/static/' in attrs or 'type="application/json"' in attrs, (p, attrs))
            self.assertNotRegex(html, r"\sstyle\s*=", p)
            self.assertNotRegex(html, r"\son[a-z]+\s*=", p)
            self.assertNotRegex(html, r"<link[^>]+href=\"https?://", p)
            self.assertNotRegex(html, r"<(script|img|iframe|link)[^>]+(src|href)=\"https?://", p)
            self.assertNotIn("fonts.googleapis", html)
            if p != "/services":                                         # the service tiles legitimately LINK to the tailnet URLs of the services
                self.assertNotRegex(html, r"https?://", p)

    def test_settings_page_embeds_the_schedule_as_inert_json(self):
        ck = self.cookie(self.login())
        r = self.get("/settings/plant_rack1", ck)
        m = re.search(r'<script type="application/json" id="doc">(.*?)</script>', r.text, re.S)
        self.assertIsNotNone(m)
        import json
        doc = json.loads(m.group(1))
        self.assertEqual((doc["id"], doc["kind"], doc["schedule"]["can_edit"]), ("plant_rack1", "plug", True))
        self.assertNotIn("<", m.group(1))                                # nothing in the data block can close the tag
        self.assertIn("Расписание: Свет для растений — стеллаж 1", r.text)

    def test_the_i18n_table_is_inert_json_too(self):
        r = self.e.client.get("/login")
        m = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', r.text, re.S)
        import json
        d = json.loads(m.group(1))
        self.assertEqual(d["S"]["turn_on"], "Включить")
        self.assertIn("validation_failed", d["E"])
        self.assertNotIn("<", m.group(1))

    def test_services_page_and_role_filtering(self):
        self.e.run(self.e.core.probe_all())
        r = self.get("/services", self.cookie(self.login()))
        self.assertIn("Fake web", r.text)
        self.assertIn("Admin only", r.text)
        self.assertIn("b-ok", r.text)
        ck = self.cookie(self.login("marta", ip="5.5.5.9"))
        r = self.get("/services", ck)
        self.assertIn("Fake web", r.text)
        self.assertNotIn("Admin only", r.text)

    def test_admin_only_pages_refuse_the_household(self):
        ck = self.cookie(self.login("marta", ip="5.5.5.10"))
        for p in ("/people", "/audit"):
            self.assertEqual(self.get(p, ck).status_code, 403, p)
        self.assertNotIn('href="/people"', self.get("/home", ck).text)
        self.assertEqual(self.get("/home", ck).status_code, 200)

    def test_parental_pages_only_for_parents_and_the_filter_page_only_for_the_admin(self):
        ck = self.cookie(self.login("marta", ip="5.5.5.11"))
        r = self.get("/parental", ck)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="par-root" data-mode="children" data-admin="no"', r.text)                 # household: no device controls
        self.assertIn('src="/static/parental.js"', r.text)
        self.assertIn("Mi WiFi", r.text)                                                   # bedtime lives in the router app: a one-line note, no integration
        self.assertNotIn('href="/parental/filters"', r.text)                               # household: no link to the admin page
        self.assertEqual(self.get("/parental/filters", ck).status_code, 403)
        ck2 = self.cookie(self.login("kid-sitter", ip="5.5.5.12"))
        self.assertEqual(self.get("/parental", ck2).status_code, 403)
        self.assertEqual(self.get("/parental/filters", ck2).status_code, 403)
        self.assertNotIn('href="/parental"', self.get("/home", ck2).text)
        ck3 = self.cookie(self.login("webadmin", ip="5.5.5.13"))
        r = self.get("/parental", ck3)
        self.assertIn('href="/parental/filters"', r.text)
        self.assertIn('data-mode="children" data-admin="yes"', r.text)                                  # the admin gets the device controls
        r = self.get("/parental/filters", ck3)
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="par-root" data-mode="filters"', r.text)

    def test_the_parental_script_is_served_and_builds_the_page_without_innerhtml(self):
        r = self.e.client.get("/static/parental.js")
        self.assertEqual((r.status_code, r.headers["content-type"].split(";")[0]), (200, "text/javascript"))
        self.assertNotIn("innerHTML", r.text)                                              # everything from the server goes in as text
        self.assertNotIn("eval(", r.text)
        from gw.strings import S
        used = set(re.findall(r'\bT\("([a-z0-9_]+)"', r.text)) | set(re.findall(r'\[[0-9]+, "(par_[a-z0-9_]+)"\]', r.text))
        used = {k for k in used if not k.endswith("_")}                                  # T("par_state_" + s): the dynamic ones are checked below
        self.assertGreater(len(used), 40)
        self.assertEqual(sorted(k for k in used if k not in S), [], "strings the page uses but the table lacks")
        dynamic = {"par_state_": ["open", "closed", "restricted"], "par_target_": ["youtube", "roblox", "maps", "all"],
                   "par_d_step_": ["create", "job", "verify", "rules", "parity", "revert", "internal", "remove", "client", "others"], "par_icon_": ["phone", "tablet", "laptop", "console", "tv", "other"],
                   "par_f_step_": ["validate", "apply", "probe", "replica", "parity", "revert", "probe_after_revert", "hold", "internal"],
                   "par_f_act_": ["add", "remove", "enable", "disable", "rename", "refresh", "revert"], "par_f_res_": ["applied", "reverted", "held", "failed", "running"]}
        for prefix, suffixes in dynamic.items():
            self.assertIn('T("%s" +' % prefix, r.text)
            for s in suffixes:
                self.assertIn(prefix + s, S)

    def test_people_page_lists_people_and_phones_for_the_admin(self):
        r = self.get("/people", self.cookie(self.login()))
        self.assertEqual(r.status_code, 200)
        for text in ("Ivan", "Dasha", "Создать код", "Телефоны"):
            self.assertIn(text, r.text)
        self.assertNotIn(self.e.tokens["dasha"], r.text)                        # no token anywhere on the page

    def test_audit_page(self):
        self.e.req("POST", "/v1/devices/plant_cacti/switch", who="ivan", json={"on": True})
        self.e.req("POST", "/v1/devices/plant_cacti/switch", who="ivan", json={"on": False}, headers={"Idempotency-Key": "web-audit-0001"})
        r = self.get("/audit?action=switch", self.cookie(self.login()))
        self.assertIn("switch: plant_cacti", r.text)
        self.assertNotIn("login:", r.text.replace("login_failed", ""))          # the action filter works

    # ── CSRF and the API with a cookie ──
    def test_state_changing_api_calls_with_a_cookie_need_the_csrf_header(self):
        r = self.login()
        ck = self.cookie(r)
        sess = self.e.client.get("/v1/auth/session", headers=ck).json()
        body = {"on": True}
        no = self.e.client.post("/v1/devices/plant_rack1/switch", json=body, headers={**ck, "Idempotency-Key": "csrf-test-001"})
        self.assertEqual((no.status_code, no.json()["error"]["code"]), (403, "csrf"))
        bad = self.e.client.post("/v1/devices/plant_rack1/switch", json=body, headers={**ck, "X-CSRF-Token": "wrong", "Idempotency-Key": "csrf-test-002"})
        self.assertEqual(bad.status_code, 403)
        ok = self.e.client.post("/v1/devices/plant_rack1/switch", json=body, headers={**ck, "X-CSRF-Token": sess["csrf"], "Idempotency-Key": "csrf-test-003"})
        self.assertEqual((ok.status_code, ok.json()["outcome"]), (200, "confirmed"))
        self.assertEqual(self.e.client.get("/v1/devices", headers=ck).status_code, 200)       # reads need no token
        self.e.client.post("/v1/devices/plant_rack1/switch", json={"on": False}, headers={**ck, "X-CSRF-Token": sess["csrf"], "Idempotency-Key": "csrf-test-004"})

    def test_logout_form_needs_csrf_and_ends_the_session(self):
        ck = self.cookie(self.login())
        sess = self.e.client.get("/v1/auth/session", headers=ck).json()
        r = self.e.client.post("/logout", data={"csrf": "wrong"}, headers=ck, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get("/home", ck).status_code, 200)
        r = self.e.client.post("/logout", data={"csrf": sess["csrf"]}, headers=ck, follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))
        self.assertEqual(self.get("/home", ck).status_code, 303)                # the cookie is dead server-side

    def test_json_login_api(self):
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id='marta'")
        r = self.e.req("POST", "/v1/auth/login", who=None, token="", json={"username": "marta", "password": PASSWORD, "totp": auth.totp_now(self.e.seeds["marta"])}, headers={"X-Forwarded-For": "5.5.5.20"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["person"]["id"], "marta")
        self.assertIn("httponly", r.headers["set-cookie"].lower())
        r = self.e.req("POST", "/v1/auth/login", who=None, token="", json={"username": "marta", "password": "bad bad bad bad bad"}, headers={"X-Forwarded-For": "5.5.5.21"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "invalid_credentials"))

    def test_json_session_and_logout(self):
        ck = self.cookie(self.login())
        r = self.e.req("GET", "/v1/auth/session", who=None, token="", headers=ck)
        self.assertEqual((r.status_code, r.json()["person"]["id"]), (200, "webadmin"))
        csrf = r.json()["csrf"]
        self.assertTrue(csrf)
        self.assertEqual(self.e.req("GET", "/v1/auth/session", who=None, token="").status_code, 401)
        r = self.e.req("POST", "/v1/auth/logout", who=None, token="", headers={**ck, "X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.e.req("GET", "/v1/auth/session", who=None, token="", headers=ck).status_code, 401)

    # ── the setup page ──
    def test_setup_page_flow(self):
        self.e.people.create(A, "Setup Test", "household", False, web_login=True)
        raw, _ = self.e.people.create_setup_link(A, "setup-test")
        r = self.e.client.get("/setup/" + raw)
        self.assertEqual(r.status_code, 200)
        self.assertIn("<svg", r.text)                                              # the QR
        self.assertNotRegex(r.text, r"<svg[^>]*\sstyle=")
        seed = re.search(r'<code class="seed">([A-Z2-7]+)</code>', r.text).group(1)
        bad = self.e.client.post("/setup/" + raw, data={"password": "short", "code": auth.totp_now(seed)})
        self.assertEqual(bad.status_code, 422)
        bad = self.e.client.post("/setup/" + raw, data={"password": PASSWORD, "code": "000000"})
        self.assertEqual(bad.status_code, 422)
        ok = self.e.client.post("/setup/" + raw, data={"password": PASSWORD, "code": auth.totp_now(seed)})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("Готово", ok.text)
        again = self.e.client.get("/setup/" + raw)
        self.assertEqual(again.status_code, 410)
        self.assertEqual(self.e.client.post("/setup/" + raw, data={"password": PASSWORD, "code": "123456"}).status_code, 410)
        self.assertEqual(self.e.client.get("/setup/not-a-real-token").status_code, 410)
        self.assertNotIn(PASSWORD, open(self.e.db.jsonl).read())

    # ── static files ──
    def test_static_files(self):
        for path, ctype in (("/static/app.css", "text/css"), ("/static/app.js", "text/javascript"), ("/static/icons.svg", "image/svg+xml")):
            r = self.e.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r.headers["content-type"].startswith(ctype), (path, r.headers["content-type"]))
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        for evil in ("/static/../db.py", "/static/..%2fdb.py", "/static/%2e%2e/%2e%2e/etc/passwd", "/static/nothing.js", "/static//etc/passwd"):
            self.assertEqual(self.e.client.get(evil).status_code, 404, evil)

    def test_the_sprite_contains_every_icon_the_data_can_ask_for(self):
        sprite = open(os.path.join(os.path.dirname(HERE), "gw", "static", "icons.svg")).read()
        ids = set(re.findall(r'<symbol id="([a-z_]+)"', sprite))
        from gw import config
        wanted = {icon for _k, icon in config.ICON_SEED} | {t[2] for t in config.SERVICE_SEED} | {"plug", "vacuum"}
        self.assertEqual(wanted - ids, set())
        self.assertNotRegex(sprite, r"https?://(?!www\.w3\.org)")             # no external reference in the sprite


if __name__ == "__main__":
    unittest.main()
