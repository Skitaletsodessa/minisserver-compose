import unittest
from tests.helpers import Env, SYSTEM

ALL = [1, 2, 3, 4, 5, 6, 7]
RULES = [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}]


class ActingUser(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        _tid, self.gw_admin = self.e.store.create_token(SYSTEM, "gw-admin", "admin", gateway=True)
        _tid, self.gw_house = self.e.store.create_token(SYSTEM, "gw-household", "household", gateway=True)

    def put(self, token, headers=None):
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        h = {"If-Match": g.headers["ETag"], **(headers or {})}
        return self.e.req("PUT", "/v1/schedules/plant_rack1", token=token, headers=h, json={"rules": RULES})

    def last_actor(self):
        return self.e.req("GET", "/v1/audit?entity=plant_rack1&limit=1").json()["audit"][0]["actor"]

    def test_a_gateway_token_records_who_it_acts_for(self):
        r = self.put(self.gw_house, {"X-Acting-User": "person=dasha;phone=p-7f3a"})
        self.assertEqual(r.status_code, 200)
        a = self.last_actor()
        self.assertEqual(a["acting"], {"person": "dasha", "phone": "p-7f3a"})
        self.assertEqual((a["name"], a["role"], a["gateway"]), ("gw-household", "household", True))

    def test_the_web_ui_may_send_a_dash_for_no_phone(self):
        self.assertEqual(self.put(self.gw_admin, {"X-Acting-User": "person=ivan;phone=-"}).status_code, 200)
        self.assertEqual(self.last_actor()["acting"], {"person": "ivan", "phone": "-"})

    def test_a_plain_token_cannot_claim_to_be_somebody_else(self):
        r = self.put(self.e.tokens["admin"], {"X-Acting-User": "person=ivan;phone=p-1"})
        self.assertEqual(r.status_code, 200)                 # not an error: the header is simply ignored
        a = self.last_actor()
        self.assertNotIn("acting", a)
        self.assertEqual(a["name"], "t-admin")

    def test_a_gateway_token_without_the_header_has_no_acting_field(self):
        self.assertEqual(self.put(self.gw_admin).status_code, 200)
        self.assertNotIn("acting", self.last_actor())

    def test_a_malformed_header_from_a_gateway_token_is_refused(self):
        for bad in ("ivan", "person=IVAN;phone=x", "person=ivan;phone=", "person=ivan;phone=a b", "person=;phone=x", "person=a;phone=b;x=y"):
            r = self.e.req("GET", "/v1/entities", token=self.gw_admin, headers={"X-Acting-User": bad})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "bad_acting_user"), bad)

    def test_gateway_flag_is_visible_and_audited_and_default_is_off(self):
        toks = {t["name"]: t for t in self.e.req("GET", "/v1/tokens").json()["tokens"]}
        self.assertTrue(toks["gw-admin"]["gateway"])
        self.assertFalse(toks["t-admin"]["gateway"])
        rows = [a for a in self.e.req("GET", "/v1/audit?limit=20").json()["audit"] if a["action"] == "token_create" and a["after"].get("name") == "gw-admin"]
        self.assertTrue(rows[0]["after"]["gateway"])

    def test_the_role_check_is_unchanged(self):
        r = self.e.req("PUT", "/v1/entities/bathroom_towels/guardrails", token=self.gw_house, headers={"X-Acting-User": "person=ivan;phone=-"},
                       json={"max_on_minutes": 100, "allowed_from": "06:00", "allowed_to": "21:00", "min_gap_minutes": 30})
        self.assertEqual(r.status_code, 403)             # claiming to be the admin does not raise a household token's rights

    def test_the_audit_chain_still_verifies(self):
        self.put(self.gw_house, {"X-Acting-User": "person=dasha;phone=p-1"})
        self.assertTrue(self.e.store.verify_audit()[0])


if __name__ == "__main__":
    unittest.main()
