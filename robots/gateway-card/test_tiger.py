"""Task 32: the plan-less robot card (ETA Tiger) behind the feature-flag file; the H40 cards must be unaffected."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re
import time
from helpers import Env, PASSWORD  # noqa: E402
from gw import auth, config  # noqa: E402

A = {"person_id": "test", "phone_id": None, "ip": "t"}

TV = "vacuum.eta_tiger_robot_vacuum"


class FlagFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "robots.json")
        os.environ.pop("GW_ROBOTS_FILE", None)

    def seed(self, doc):
        with open(self.path, "w") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return config.vacuum_seed(self.d)

    def test_absent_or_broken_flag_changes_nothing(self):
        self.assertEqual(sorted(config.vacuum_seed(self.d)), ["bedroom", "livingroom"])
        for bad in ("not json", "[]", {"tiger": 1}, {"tiger": {"enabled": False, "vacuum": TV}}, {"tiger": {"enabled": "yes", "vacuum": TV}},
                    {"tiger": {"enabled": True}}, {"tiger": {"enabled": True, "vacuum": "switch.x"}}, {"tiger": {"enabled": True, "vacuum": "vacuum.A b"}}):
            self.assertEqual(sorted(self.seed(bad)), ["bedroom", "livingroom"], bad)

    def test_valid_flag_adds_only_the_planless_robot(self):
        s = self.seed({"tiger": {"enabled": True, "vacuum": TV}})
        self.assertEqual(sorted(s), ["bedroom", "livingroom", "tiger"])
        self.assertEqual((s["tiger"]["vacuum"], s["tiger"]["script"], s["tiger"]["schedule_id"]), (TV, None, None))
        self.assertEqual(config.VACUUM_SEED.keys(), {"bedroom": 0, "livingroom": 0}.keys())          # the seed table itself is untouched

    def test_env_override(self):
        os.environ["GW_ROBOTS_FILE"] = self.path
        try:
            self.seed({"tiger": {"enabled": True, "vacuum": TV}})
            self.assertIn("tiger", config.vacuum_seed("/nonexistent"))
        finally:
            os.environ.pop("GW_ROBOTS_FILE", None)


class TigerCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flag = os.path.join(tempfile.mkdtemp(), "robots.json")
        with open(cls.flag, "w") as f:
            json.dump({"tiger": {"enabled": True, "vacuum": TV}}, f)
        os.environ["GW_ROBOTS_FILE"] = cls.flag
        cls.e = Env()
        now = __import__("time").time()
        cls.e.ha.states[TV] = {"s": "docked", "a": {"battery_level": 77}, "lc": now, "lu": now}
        cls.e.ha.set(TV, "docked", {"battery_level": 77})
        cls.e.wait(lambda: cls.e.haclient.state(TV) == "docked")
        cls.e.people.create(A, "Webadmin", "admin", True, web_login=True)
        raw, _ = cls.e.people.create_setup_link(A, "webadmin")
        _p, cls.seed = cls.e.people.setup_begin(raw)
        cls.e.people.setup_complete(raw, PASSWORD, auth.totp_now(cls.seed), "1.1.1.1")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("GW_ROBOTS_FILE", None)
        cls.e.close() if hasattr(cls.e, "close") else None

    def setUp(self):
        self.e.ha.calls.clear()
        self.e.ha.set(TV, "docked")
        self.e.wait(lambda: self.e.haclient.state(TV) == "docked")
        self.e.ha.swallow.discard(TV)

    def vac(self, who="ivan"):
        return {x["id"]: x for x in self.e.req("GET", "/v1/vacuums", who=who).json()["vacuums"]}

    def test_list_shows_three_robots_and_h40_cards_are_unchanged(self):
        v = self.vac("dasha")
        self.assertEqual(sorted(v), ["bedroom", "livingroom", "tiger"])
        t = v["tiger"]
        self.assertEqual((t["state"], t["battery"], t["schedule_id"], t["can_start"], t["can_locate"]), ("docked", 77, None, True, True))
        self.assertEqual([s["id"] for s in t["scenarios"]], ["clean"])
        self.assertEqual((v["bedroom"]["schedule_id"], v["bedroom"]["can_locate"]), ("vacuum_bedroom", False))
        self.assertFalse(self.vac("ola")["tiger"]["can_start"])
        self.assertFalse(self.vac("ola")["tiger"]["can_locate"])

    def test_start_is_vacuum_start_by_a_person_and_confirmed(self):
        j = self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="dasha").json()
        self.assertEqual((j["outcome"], j["action"], j["scenario"]), ("confirmed", "start", "clean"))
        self.assertEqual(self.e.ha.calls, [("vacuum", "start", TV, {})])
        row = self.e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"], row["ok"]), ("vacuum_start", "dasha", "tiger/clean", True))

    def test_start_refusals(self):
        self.e.ha.set(TV, "paused")
        self.e.wait(lambda: self.e.haclient.state(TV) == "paused")
        r = self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_docked"))
        self.e.ha.set(TV, "unavailable")
        self.e.wait(lambda: self.e.haclient.state(TV) == "unavailable")
        r = self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "robot_unavailable"))
        self.assertEqual(self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="ola").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/vacuums/tiger/scenarios/nope/start", who="ivan").status_code, 404)
        self.assertEqual(self.e.ha.calls, [])

    def test_unconfirmed_start_is_reported_honestly(self):
        self.e.ha.swallow.add(TV)
        j = self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="ivan").json()
        self.assertEqual(j["outcome"], "unconfirmed")
        self.assertFalse(self.e.db.audit_rows(1)[0]["ok"])

    def test_pause_resume_dock_work_without_a_script(self):
        self.e.req("POST", "/v1/vacuums/tiger/scenarios/clean/start", who="ivan")
        self.e.wait(lambda: self.e.haclient.state(TV) == "cleaning")
        j = self.e.req("POST", "/v1/vacuums/tiger/pause", who="ivan").json()
        self.assertEqual((j["outcome"], j["action"]), ("confirmed", "pause"))
        j = self.e.req("POST", "/v1/vacuums/tiger/resume", who="ivan").json()
        self.assertEqual((j["outcome"], j["action"]), ("confirmed", "resume"))
        j = self.e.req("POST", "/v1/vacuums/tiger/dock", who="ivan").json()
        self.assertEqual((j["outcome"], j["action"]), ("confirmed", "dock"))
        self.assertEqual([c[:2] + (c[2],) for c in self.e.ha.calls][-1], ("vacuum", "return_to_base", TV))
        self.assertFalse([c for c in self.e.ha.calls if c[0] == "script"])

    def test_locate_is_sent_audited_and_never_claims_confirmation(self):
        j = self.e.req("POST", "/v1/vacuums/tiger/locate", who="dasha").json()
        self.assertEqual((j["outcome"], j["action"]), ("unconfirmed", "locate"))
        self.assertEqual(self.e.ha.calls, [("vacuum", "locate", TV, {})])
        row = self.e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"]), ("vacuum_locate", "dasha", "tiger"))
        self.assertEqual(self.e.req("POST", "/v1/vacuums/tiger/locate", who="ola").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/vacuums/bedroom/locate", who="ivan").status_code, 404)        # the H40s have no locate here
        self.e.ha.set(TV, "unavailable")
        self.e.wait(lambda: self.e.haclient.state(TV) == "unavailable")
        self.assertEqual(self.e.req("POST", "/v1/vacuums/tiger/locate", who="ivan").status_code, 409)

    def test_h40_start_still_uses_its_script(self):
        self.e.req("POST", "/v1/vacuums/livingroom/scenarios/full/start", who="ivan")
        self.assertEqual(self.e.ha.calls, [("script", "turn_on", "script.vacuum_livingroom_plan", {})])
        self.e.ha.set("script.vacuum_livingroom_plan", "off")

    def test_home_page_renders_the_locate_button_only_for_the_tiger(self):
        self.e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", ("webadmin",))
        r = self.e.client.post("/login", data={"username": "webadmin", "password": PASSWORD, "totp": auth.totp_now(self.seed)}, headers={"X-Forwarded-For": "5.5.5.5"}, follow_redirects=False)
        ck = {"Cookie": "gw_session=" + re.search(r"gw_session=([^;]+)", r.headers["set-cookie"]).group(1)}
        html = self.e.client.get("/home", headers=ck, follow_redirects=False).text
        self.assertEqual(html.count('data-action="vac-locate"'), 1)
        self.assertIn('data-vacuum="tiger"', html)
        self.assertEqual(html.count('data-vacuum="'), 3)


if __name__ == "__main__":
    unittest.main()
