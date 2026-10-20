"""People, enrolment, phones and the audit through the HTTP API, plus the SIMULATED APP CLIENT that walks the whole flow
(code -> enrol -> capabilities -> devices -> switch -> revoke)."""
import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, Stream  # noqa: E402
from gw import auth, config  # noqa: E402


def setUpModule():
    BaseCase.ENV = Env()


def tearDownModule():
    BaseCase.ENV.close()


class People(BaseCase):
    def test_only_admin_manages_people(self):
        for who in ("dasha", "ola"):
            self.assertEqual(self.e.req("GET", "/v1/people", who=who).status_code, 403)
            self.assertEqual(self.e.req("POST", "/v1/people", who=who, json={"name": "X", "role": "readonly"}).status_code, 403)
            self.assertEqual(self.e.req("PATCH", "/v1/people/dasha", who=who, json={"role": "admin"}).status_code, 403)
            self.assertEqual(self.e.req("POST", "/v1/people/dasha/enrolment-codes", who=who).status_code, 403)
            self.assertEqual(self.e.req("POST", "/v1/people/dasha/setup-link", who=who).status_code, 403)

    def test_create_patch_and_the_last_admin(self):
        r = self.e.req("POST", "/v1/people", json={"name": "Guest One", "role": "readonly"})
        self.assertEqual((r.status_code, r.json()["id"], r.json()["parent"], r.json()["web_login_enabled"]), (201, "guest-one", False, False))
        self.assertEqual(self.e.req("POST", "/v1/people", json={"name": "Guest One", "role": "readonly"}).status_code, 409)
        self.assertEqual(self.e.req("POST", "/v1/people", json={"name": "X", "role": "root"}).status_code, 422)
        r = self.e.req("PATCH", "/v1/people/guest-one", json={"role": "household", "parent": True})
        self.assertEqual((r.json()["role"], r.json()["parent"]), ("household", True))
        r = self.e.req("PATCH", "/v1/people/ivan", json={"role": "household"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "last_admin"))
        self.assertEqual(self.e.req("PATCH", "/v1/people/ghost", json={"active": False}).status_code, 404)
        self.assertEqual(self.e.req("PATCH", "/v1/people/guest-one", json={"bogus": 1}).status_code, 422)
        names = [(a[0], a[1]) for a in self.audit_actions(10)]
        self.assertIn(("person_create", "ivan"), names)
        self.assertIn(("person_patch", "ivan"), names)

    def test_people_list_has_no_secret_fields(self):
        p = self.e.req("GET", "/v1/people").json()["people"]
        self.assertEqual({x["id"] for x in p} >= {"ivan", "dasha", "ola"}, True)
        for x in p:
            self.assertEqual(set(x), {"id", "name", "role", "parent", "active", "web_login_enabled", "has_web_credentials", "phones"})

    def test_deactivating_a_person_cuts_their_phones_off_at_once(self):
        self.e.req("POST", "/v1/people", json={"name": "Temp Person", "role": "household"})
        code = self.e.req("POST", "/v1/people/temp-person/enrolment-codes").json()["code"]
        tok = self.e.req("POST", "/v1/enrol", who=None, token="", json={"code": code, "device": {"label": "t"}}).json()["token"]
        self.assertEqual(self.e.req("GET", "/v1/capabilities", token=tok).status_code, 200)
        self.e.req("PATCH", "/v1/people/temp-person", json={"active": False})
        self.assertEqual(self.e.req("GET", "/v1/capabilities", token=tok).status_code, 401)


class Enrolment(BaseCase):
    def test_code_endpoint_shape_and_qr(self):
        r = self.e.req("POST", "/v1/people/dasha/enrolment-codes")
        j = r.json()
        self.assertEqual(r.status_code, 201)
        self.assertRegex(j["code"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(j["ttl_s"], 600)
        self.assertEqual(set(j["qr_payload"]), {"u", "c", "tailnet"})
        self.assertEqual(j["qr_payload"]["tailnet"], "")                   # reserved for Task 30
        self.assertEqual(j["qr_payload"]["c"], j["code"])
        self.assertTrue(j["qr_payload"]["u"].startswith("https://"))
        self.assertTrue(j["qr_svg"].lstrip().startswith("<svg"))
        for bad in ("<script", "style=", "onload", "href=", "xlink"):
            self.assertNotIn(bad, j["qr_svg"].lower())
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_enrol_returns_the_token_once_and_the_code_is_then_spent(self):
        code = self.e.req("POST", "/v1/people/ola/enrolment-codes").json()["code"]
        r = self.e.req("POST", "/v1/enrol", who=None, token="", json={"code": code.lower(), "device": {"label": "Ola phone", "model": "M1", "platform": "android", "app_version": "1.0"}})
        j = r.json()
        self.assertEqual((r.status_code, j["person"]["id"], j["person"]["role"]), (201, "ola", "readonly"))
        self.assertTrue(j["token"])
        r2 = self.e.req("POST", "/v1/enrol", who=None, token="", json={"code": code, "device": {"label": "again"}})
        self.assertEqual((r2.status_code, r2.json()["error"]["code"]), (403, "invalid_code"))
        ph = [p for p in self.e.req("GET", "/v1/phones", who="ivan").json()["phones"] if p["id"] == j["phone_id"]][0]
        self.assertEqual((ph["person_id"], ph["label"], ph["model"], ph["platform"], ph["revoked"]), ("ola", "Ola phone", "M1", "android", False))
        self.assertNotIn(j["token"], str(self.e.req("GET", "/v1/phones", who="ivan").json()))      # the token is never listed again
        self.e.req("DELETE", "/v1/phones/" + j["phone_id"], who="ivan")

    def test_wrong_codes_look_alike_and_are_rate_limited(self):
        h = {"X-Forwarded-For": "4.4.4.4"}
        seen = set()
        for i in range(config.MAX_PHONES_TOTAL if False else 10):
            r = self.e.req("POST", "/v1/enrol", who=None, token="", headers=h, json={"code": auth.fmt_code(auth.new_code()), "device": {"label": "g"}})
            seen.add((r.status_code, r.json()["error"]["code"]))
        self.assertEqual(seen, {(403, "invalid_code")})
        r = self.e.req("POST", "/v1/enrol", who=None, token="", headers=h, json={"code": auth.fmt_code(auth.new_code()), "device": {"label": "g"}})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "too_many_attempts"))
        good = self.e.req("POST", "/v1/people/ola/enrolment-codes").json()["code"]
        r = self.e.req("POST", "/v1/enrol", who=None, token="", headers=h, json={"code": good, "device": {"label": "g"}})
        self.assertEqual(r.status_code, 429)                                    # the blocked address stays blocked even with a right code
        r = self.e.req("POST", "/v1/enrol", who=None, token="", headers={"X-Forwarded-For": "4.4.4.5"}, json={"code": good, "device": {"label": "g"}})
        self.assertEqual(r.status_code, 201)
        self.e.req("DELETE", "/v1/phones/" + r.json()["phone_id"], who="ivan")

    def test_bad_enrol_bodies(self):
        for body in ({}, {"code": "x"}, {"device": {"label": "a"}}, {"code": "AAAA-AAAA-AAAA", "device": {"label": ""}}):
            r = self.e.req("POST", "/v1/enrol", who=None, token="", json=body)
            self.assertIn(r.status_code, (400, 403))

    def test_the_five_phones_per_person_limit(self):
        self.e.req("POST", "/v1/people", json={"name": "Limit Test", "role": "readonly"})
        ids = []
        for i in range(5):
            code = self.e.req("POST", "/v1/people/limit-test/enrolment-codes").json()["code"]
            ids.append(self.e.req("POST", "/v1/enrol", who=None, token="", json={"code": code, "device": {"label": "p%d" % i}}).json()["phone_id"])
        r = self.e.req("POST", "/v1/people/limit-test/enrolment-codes")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "device_limit"))
        self.e.req("DELETE", "/v1/phones/" + ids[0], who="ivan")
        self.assertEqual(self.e.req("POST", "/v1/people/limit-test/enrolment-codes").status_code, 201)
        for i in ids[1:]:
            self.e.req("DELETE", "/v1/phones/" + i, who="ivan")

    def test_setup_link_needs_web_login(self):
        r = self.e.req("POST", "/v1/people/dasha/setup-link")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "web_login_disabled"))
        r = self.e.req("POST", "/v1/people/ivan/setup-link")
        self.assertEqual(r.status_code, 201)
        self.assertRegex(r.json()["url"], r"^https://.+/setup/[A-Za-z0-9_-]{20,}$")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn(r.json()["url"].rsplit("/", 1)[1], str(self.e.db.audit_rows(50)))        # the audit never holds the link

    def test_phones_visibility_and_revocation_rights(self):
        mine = self.e.req("GET", "/v1/phones", who="dasha").json()["phones"]
        self.assertTrue(mine and all(p["person_id"] == "dasha" for p in mine))
        allp = self.e.req("GET", "/v1/phones", who="ivan").json()["phones"]
        self.assertTrue({"dasha", "ola", "ivan"} <= {p["person_id"] for p in allp})
        ola_phone = [p["id"] for p in allp if p["person_id"] == "ola"][0]
        r = self.e.req("DELETE", "/v1/phones/" + ola_phone, who="dasha")
        self.assertEqual(r.status_code, 403)                                    # not her phone
        self.assertEqual(self.e.req("DELETE", "/v1/phones/nope", who="ivan").status_code, 404)

    def test_audit_visibility(self):
        self.e.req("POST", "/v1/devices/plant_rack1/switch", who="dasha", json={"on": True})
        self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True})
        dasha = self.e.req("GET", "/v1/audit?limit=200", who="dasha").json()["audit"]
        self.assertTrue(dasha and all(a["person"] == "dasha" for a in dasha))
        ivan = self.e.req("GET", "/v1/audit?limit=200", who="ivan").json()["audit"]
        self.assertTrue({"dasha", "ivan"} <= {a["person"] for a in ivan})
        only = self.e.req("GET", "/v1/audit?action=switch&person=dasha", who="ivan").json()["audit"]
        self.assertTrue(only and all(a["action"] == "switch" and a["person"] == "dasha" for a in only))
        self.assertEqual(self.e.req("GET", "/v1/audit?limit=abc", who="ivan").status_code, 400)
        # a non-admin cannot widen the view with filters
        wide = self.e.req("GET", "/v1/audit?person=ivan", who="dasha").json()["audit"]
        self.assertTrue(all(a["person"] == "dasha" for a in wide))


class SimulatedApp(BaseCase):
    """What the Android app will do (Task 30), as a script: the whole life of one phone."""
    def test_whole_flow(self):
        e = self.e
        # 1. the admin shows a code (web UI); the QR payload carries it
        code = e.req("POST", "/v1/people/dasha/enrolment-codes", who="ivan").json()
        # 2. the app enrols with the code (no other credential)
        res = e.req("POST", "/v1/enrol", who=None, token="", json={"code": code["qr_payload"]["c"], "device": {"label": "Dasha Pixel", "model": "Pixel 9", "platform": "android", "app_version": "0.1"}}).json()
        tok = res["token"]
        # 3. capabilities decide the tabs
        caps = e.req("GET", "/v1/capabilities", token=tok).json()
        self.assertEqual((caps["tabs"], caps["person"]["id"]), (["home", "parental"], "dasha"))
        # 4. cards, vacuums, tiles
        devs = e.req("GET", "/v1/devices", token=tok).json()["devices"]
        self.assertEqual(len(devs), 6)
        self.assertEqual(len(e.req("GET", "/v1/vacuums", token=tok).json()["vacuums"]), 2)
        self.assertEqual([t["id"] for t in e.req("GET", "/v1/services", token=tok).json()["services"]], ["fakeweb"])
        # 5. an event stream, then a switch from this phone is seen on it
        st = Stream(e, tok)
        self.assertTrue(e.wait(lambda: st.status == 200))
        r = e.req("POST", "/v1/devices/plant_orchids/switch", token=tok, json={"on": True})
        self.assertEqual(r.json()["outcome"], "confirmed")
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "device" and ev[2]["id"] == "plant_orchids" and ev[2]["state"] == "on"))
        # 6. the audit knows the person AND the phone
        row = e.db.audit_rows(5)
        sw = [a for a in row if a["action"] == "switch"][0]
        self.assertEqual((sw["person"], sw["device"], sw["target"]), ("dasha", res["phone_id"], "plant_orchids"))
        # 7. the admin revokes the phone: the stream closes, the next request is refused
        self.assertEqual(e.req("DELETE", "/v1/phones/" + res["phone_id"], who="ivan").status_code, 204)
        self.assertTrue(st.done.wait(5), "the open event stream was not closed")
        r = e.req("GET", "/v1/capabilities", token=tok)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "unauthorized"))
        r = e.req("POST", "/v1/devices/plant_orchids/switch", token=tok, json={"on": False})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(e.haclient.state("switch.kitchen_orchids"), "on")      # the refused call did nothing
        e.req("POST", "/v1/devices/plant_orchids/switch", who="ivan", json={"on": False})


class Events(BaseCase):
    def test_device_vacuum_link_and_reserved_release_events(self):
        st = Stream(self.e, self.e.tokens["dasha"])
        self.assertTrue(self.e.wait(lambda: st.status == 200))
        self.e.ha.set("switch.minisserver", "on")
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "device" and ev[2] == {"id": "plant_cacti", "state": "on", "stale": False}))
        self.e.ha.set("vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "cleaning")
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "vacuum" and ev[2]["id"] == "livingroom" and ev[2]["state"] == "cleaning"))
        self.e.hub.publish("release", {"version": "9.9", "note": "reserved for Task 30"})
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "release"))
        self.e.hub.publish("service", {"id": "x", "badge": "down"})
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "service"))
        ids = [ev[0] for ev in st.events]
        self.assertEqual(ids, sorted(ids))
        self.e.ha.set("vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "docked")

    def test_link_event_when_ha_goes_away_and_cards_turn_stale_then_recover(self):
        st = Stream(self.e, self.e.tokens["ivan"])
        self.assertTrue(self.e.wait(lambda: st.status == 200))
        self.e.ha.down = True
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "link" and ev[2]["ha"] == "down", 40))
        d = self.e.req("GET", "/v1/devices", who="ivan").json()
        self.assertTrue(all(c["stale"] for c in d["devices"]))
        self.e.ha.down = False
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "link" and ev[2]["ha"] == "ws", 40))
        self.e.ha.set("switch.bedroom_parrot", "on")
        self.assertIsNotNone(st.wait_event(lambda ev: ev[1] == "device" and ev[2]["id"] == "parrot_uv" and ev[2]["state"] == "on" and ev[2]["stale"] is False, 10))
        self.assertTrue(all(not c["stale"] for c in self.e.req("GET", "/v1/devices", who="ivan").json()["devices"]))

    def test_last_event_id_resumes(self):
        st = Stream(self.e, self.e.tokens["ivan"])
        self.assertTrue(self.e.wait(lambda: st.status == 200))
        self.e.hub.publish("service", {"id": "a", "badge": "ok"})
        first = st.wait_event(lambda ev: ev[1] == "service" and ev[2]["id"] == "a")
        self.assertIsNotNone(first)
        self.e.hub.publish("service", {"id": "b", "badge": "ok"})
        self.e.hub.publish("service", {"id": "c", "badge": "ok"})
        st2 = Stream(self.e, self.e.tokens["ivan"], last_id=first[0])
        got = st2.wait_event(lambda ev: ev[1] == "service" and ev[2]["id"] == "c")
        self.assertIsNotNone(got)
        self.assertEqual([ev[2]["id"] for ev in st2.events if ev[1] == "service"], ["b", "c"])         # resumed AFTER the given id, nothing before it

    def test_heartbeat_comment(self):
        old = config.SSE_HEARTBEAT_S
        config.SSE_HEARTBEAT_S = 0.4
        try:
            st = Stream(self.e, self.e.tokens["ivan"])
            self.assertTrue(self.e.wait(lambda: any(l == ": hb" for l in st.lines), 5))
        finally:
            config.SSE_HEARTBEAT_S = old

    def test_a_stream_needs_valid_credentials(self):
        st = Stream(self.e, "wrong")
        self.assertTrue(st.done.wait(5))
        self.assertEqual(st.status, 401)


if __name__ == "__main__":
    unittest.main()
