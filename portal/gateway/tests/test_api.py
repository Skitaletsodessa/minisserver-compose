"""API tests against the OpenAPI contract, with a fake Home Assistant and the REAL schedule service. Synthetic people: ivan (admin, parent),
dasha (household, parent), ola (readonly). Authentication is by device token (Bearer) unless a test says otherwise."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase  # noqa: E402

def setUpModule():
    BaseCase.ENV = Env()


def tearDownModule():
    BaseCase.ENV.close()


Base = BaseCase


class Basics(Base):
    def test_health_is_liveness_only_without_credentials(self):
        r = self.e.req("GET", "/v1/health", who=None, token="")
        self.assertEqual(r.json(), {"status": "ok"})

    def test_health_details_with_credentials(self):
        h = self.e.req("GET", "/v1/health", who="ivan").json()
        self.assertEqual((h["status"], h["ha"]["link"], h["schedule_service"]["link"]), ("ok", "ws", "ok"))
        self.assertTrue(h["audit_chain"]["ok"])
        self.assertEqual(len(h["data_checksum"]), 64)

    def test_unauthenticated_calls_get_nothing_but_liveness(self):
        for method, path in (("GET", "/v1/devices"), ("GET", "/v1/vacuums"), ("GET", "/v1/services"), ("GET", "/v1/audit"), ("GET", "/v1/people"),
                             ("GET", "/v1/capabilities"), ("GET", "/v1/phones"), ("GET", "/v1/schedules/plant_rack1"), ("POST", "/v1/devices/plant_rack1/switch"),
                             ("GET", "/v1/events")):
            r = self.e.req(method, path, who=None, token="", json={"on": True} if method == "POST" else None)
            self.assertEqual(r.status_code, 401, path)
            self.assertEqual(r.json()["error"]["code"], "unauthorized")

    def test_invalid_token_is_401_and_repeated_failures_are_rate_limited(self):
        for _ in range(30):
            self.assertEqual(self.e.req("GET", "/v1/devices", token="nonsense", headers={"X-Forwarded-For": "8.8.4.4"}).status_code, 401)
        r = self.e.req("GET", "/v1/devices", token="nonsense", headers={"X-Forwarded-For": "8.8.4.4"})
        self.assertEqual(r.status_code, 429)
        # another address and a valid token are unaffected
        self.assertEqual(self.e.req("GET", "/v1/devices", who="ivan", headers={"X-Forwarded-For": "8.8.8.8"}).status_code, 200)

    def test_capabilities_per_person(self):
        iv = self.e.req("GET", "/v1/capabilities", who="ivan").json()
        da = self.e.req("GET", "/v1/capabilities", who="dasha").json()
        ol = self.e.req("GET", "/v1/capabilities", who="ola").json()
        self.assertEqual(iv["tabs"], ["home", "parental"])
        self.assertEqual(da["tabs"], ["home", "parental"])
        self.assertEqual(ol["tabs"], ["home"])                              # no parent flag: no parental tab
        self.assertEqual(iv["modules"], {"parental": False, "cameras": False})
        self.assertEqual((iv["limits"]["max_devices_total"], iv["limits"]["max_devices_per_person"]), (20, 5))
        self.assertTrue(iv["can"]["manage_people"])
        self.assertFalse(da["can"]["manage_people"])
        self.assertFalse(ol["can"]["switch"])
        self.assertEqual(da["person"], {"id": "dasha", "name": "Dasha", "role": "household", "parent": True})

    def test_the_parental_namespace_is_reserved_and_not_implemented(self):
        for who in ("ivan", "dasha"):
            r = self.e.req("GET", "/v1/parental/devices", who=who)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_implemented"))

    def test_unknown_endpoint_and_bad_json_have_the_error_shape(self):
        r = self.e.req("GET", "/v1/nothing", who="ivan", validate=False)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "not_found"))
        r = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="ivan", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "bad_json"))
        r = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="ivan", json=[1, 2])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()["error"]), {"code", "message", "details"})


class Devices(Base):
    def test_cards(self):
        d = self.e.req("GET", "/v1/devices", who="dasha").json()
        self.assertEqual(d["ha_link"], "ws")
        cards = {x["id"]: x for x in d["devices"]}
        self.assertEqual(sorted(cards), ["bathroom_towels", "parrot_uv", "plant_cacti", "plant_orchids", "plant_rack1", "plant_rack2"])
        icons = {k: v["icon"] for k, v in cards.items()}
        self.assertEqual(icons, {"bathroom_towels": "towel_dryer", "plant_rack1": "plant_rack", "plant_rack2": "plant_rack", "plant_orchids": "orchid",
                                 "plant_cacti": "cactus", "parrot_uv": "parrot_lamp"})
        c = cards["plant_rack1"]
        self.assertEqual((c["state"], c["stale"], c["can_switch"], c["schedule_id"], c["kind"]), ("off", False, True, "plant_rack1", "lamp"))
        self.assertEqual(cards["bathroom_towels"]["kind"], "dryer")
        self.assertIsNotNone(c["next_edge"])
        self.assertIn(c["source"], ("schedule", "manual"))
        self.assertIsNone(cards["plant_orchids"]["power_w"])               # fake meter: never shown

    def test_source_is_manual_when_the_state_differs_from_the_schedule(self):
        eff = self.e.req("GET", "/v1/devices/plant_rack1", who="ivan").json()
        # force a known mismatch whatever the time of day: ask the schedule service what it expects and set the opposite
        import urllib.request
        req = urllib.request.Request(self.e.sched.url + "/v1/effective?entity=plant_rack1", headers={"Authorization": "Bearer " + self.e.sched.admin_secret})
        expected = json.load(urllib.request.urlopen(req))["state"]
        self.e.ha.set("switch.living_room_rack", "off" if expected == "on" else "on")
        self.e.core._eff.clear()
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state("switch.living_room_rack") != expected))
        c = self.e.req("GET", "/v1/devices/plant_rack1", who="ivan").json()
        self.assertEqual(c["source"], "manual")
        self.assertIsNotNone(c["manual_until"])
        self.e.ha.set("switch.living_room_rack", expected)
        self.e.core._eff.clear()
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state("switch.living_room_rack") == expected))
        self.assertEqual(self.e.req("GET", "/v1/devices/plant_rack1", who="ivan").json()["source"], "schedule")

    def test_readonly_sees_but_cannot_switch(self):
        c = self.e.req("GET", "/v1/devices/plant_rack1", who="ola").json()
        self.assertFalse(c["can_switch"])
        self.assertFalse(c["can_edit_schedule"])
        r = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="ola", json={"on": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.e.ha.calls, [])

    def test_can_edit_schedule_follows_household_editable(self):
        dasha = {c["id"]: c for c in self.e.req("GET", "/v1/devices", who="dasha").json()["devices"]}
        ivan = {c["id"]: c for c in self.e.req("GET", "/v1/devices", who="ivan").json()["devices"]}
        self.assertTrue(dasha["plant_rack1"]["can_edit_schedule"])
        self.assertFalse(dasha["bathroom_towels"]["can_edit_schedule"])      # household_editable = false in the schedule service
        self.assertFalse(dasha["parrot_uv"]["can_edit_schedule"])
        self.assertTrue(ivan["bathroom_towels"]["can_edit_schedule"])

    def test_unknown_device(self):
        self.assertEqual(self.e.req("GET", "/v1/devices/nope", who="ivan").status_code, 404)
        self.assertEqual(self.e.req("POST", "/v1/devices/nope/switch", who="ivan", json={"on": True}).status_code, 404)


class Switching(Base):
    def test_switch_on_is_confirmed_by_read_back_and_audited(self):
        r = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="dasha", json={"on": True})
        j = r.json()
        self.assertEqual((r.status_code, j["outcome"], j["observed"], j["changed"]), (200, "confirmed", "on", True))
        self.assertEqual(self.e.ha.calls, [("switch", "turn_on", "switch.living_room_rack", {})])
        row = self.e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"], row["ok"]), ("switch", "dasha", "plant_rack1", True))
        self.assertEqual(row["device"], self.e.people_ids["dasha"])
        self.assertEqual(row["after"]["observed"], "on")
        self.assertEqual(self.e.req("GET", "/v1/devices/plant_rack1", who="ivan").json()["state"], "on")

    def test_already_in_the_requested_state_is_idempotent_and_makes_no_call(self):
        j = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="ivan", json={"on": False}).json()
        self.assertEqual((j["outcome"], j["changed"]), ("confirmed", False))
        self.assertEqual(self.e.ha.calls, [])

    def test_idempotency_key_replays_the_first_result(self):
        h = {"Idempotency-Key": "same-key-0001"}
        a = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True}, headers=h).json()
        self.e.core._last_switch.clear()
        b = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True}, headers=h).json()
        self.assertEqual(len(self.e.ha.calls), 1)
        self.assertTrue(b["replayed"])
        self.assertEqual((a["outcome"], b["outcome"]), ("confirmed", "confirmed"))
        r = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": False}, headers=h)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "idempotency_conflict"))

    def test_idempotency_key_is_required_and_scoped_to_the_person(self):
        r = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True}, idem=False)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "idempotency_key_required"))
        r = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True}, headers={"Idempotency-Key": "short"})
        self.assertEqual(r.status_code, 400)
        h = {"Idempotency-Key": "shared-key-01"}
        self.e.req("POST", "/v1/devices/plant_rack2/switch", who="ivan", json={"on": True}, headers=h)
        self.e.core._last_switch.clear()
        j = self.e.req("POST", "/v1/devices/plant_rack2/switch", who="dasha", json={"on": False}, headers=h).json()    # another person: not a replay
        self.assertNotIn("replayed", j)
        self.assertEqual(j["observed"], "off")

    def test_a_service_call_that_changes_nothing_is_reported_as_unconfirmed(self):
        self.e.ha.swallow.add("switch.kitchen_orchids")
        t0 = time.time()
        j = self.e.req("POST", "/v1/devices/plant_orchids/switch", who="ivan", json={"on": True}).json()
        self.assertEqual((j["outcome"], j["observed"], j["requested"]), ("unconfirmed", "off", "on"))
        self.assertGreaterEqual(time.time() - t0, 5.5)                        # it waited the bounded 6 s
        self.assertEqual(self.e.db.audit_rows(1)[0]["ok"], False)

    def test_debounce(self):
        self.e.req("POST", "/v1/devices/plant_cacti/switch", who="ivan", json={"on": True})
        r = self.e.req("POST", "/v1/devices/plant_cacti/switch", who="ivan", json={"on": False})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "too_fast"))
        self.assertEqual(r.headers.get("Retry-After"), "2")

    def test_unavailable_device_is_refused_and_audited(self):
        self.e.ha.set("switch.bedroom_parrot", "unavailable")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state("switch.bedroom_parrot") == "unavailable"))
        r = self.e.req("POST", "/v1/devices/parrot_uv/switch", who="ivan", json={"on": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "device_unavailable"))
        self.assertEqual(self.e.ha.calls, [])
        card = self.e.req("GET", "/v1/devices/parrot_uv", who="ivan").json()
        self.assertEqual((card["state"], card["can_switch"]), ("unavailable", False))

    def test_the_dryer_and_the_uv_lamp_are_switchable_by_the_household(self):
        for did in ("bathroom_towels", "parrot_uv"):
            self.assertEqual(self.e.req("POST", "/v1/devices/%s/switch" % did, who="dasha", json={"on": True}).json()["outcome"], "confirmed")

    def test_household_switchable_flag_is_honoured(self):
        self.e.db.x("UPDATE device_meta SET household_switchable=0 WHERE device_id='parrot_uv'")
        self.e.core.devices["parrot_uv"]["switchable"] = False
        try:
            r = self.e.req("POST", "/v1/devices/parrot_uv/switch", who="dasha", json={"on": True})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "not_switchable"))
            self.assertFalse(self.e.req("GET", "/v1/devices/parrot_uv", who="dasha").json()["can_switch"])
            self.assertEqual(self.e.req("POST", "/v1/devices/parrot_uv/switch", who="ivan", json={"on": True}).json()["outcome"], "confirmed")     # admin always may
        finally:
            self.e.db.x("UPDATE device_meta SET household_switchable=1 WHERE device_id='parrot_uv'")
            self.e.core.devices["parrot_uv"]["switchable"] = True

    def test_the_allow_list_refuses_anything_else(self):
        import asyncio
        from gw.auth import GwError
        loop = asyncio.new_event_loop()
        try:
            for dom, svc, eid in (("switch", "turn_on", "switch.not_ours"), ("light", "turn_on", "switch.living_room_rack"),
                                  ("homeassistant", "restart", "switch.living_room_rack"), ("automation", "reload", "automation.sch_x"),
                                  ("script", "turn_on", "script.some_other_script"), ("switch", "toggle", "switch.living_room_rack")):
                with self.assertRaises(GwError) as c:
                    loop.run_until_complete(self.e.haclient.call(dom, svc, eid))
                self.assertEqual((c.exception.status, c.exception.code), (403, "not_allowed"), (dom, svc, eid))
        finally:
            loop.close()
        self.assertEqual(self.e.ha.calls, [])

    def test_ha_down_the_api_stays_up_and_refuses_honestly(self):
        self.e.ha.down = True
        self.assertTrue(self.e.wait(lambda: self.e.haclient.link in ("poll", "down"), 12))
        self.assertTrue(self.e.wait(lambda: self.e.haclient.link == "down", 20))
        d = self.e.req("GET", "/v1/devices", who="ivan").json()
        self.assertEqual(d["ha_link"], "down")
        self.assertTrue(all(c["stale"] for c in d["devices"]))
        self.assertTrue(all(not c["can_switch"] for c in d["devices"]))
        r = self.e.req("POST", "/v1/devices/plant_rack1/switch", who="ivan", json={"on": True})
        self.assertIn(r.status_code, (503, 409))
        self.assertEqual(self.e.req("GET", "/v1/health", who="ivan").json()["status"], "degraded")
        self.e.ha.down = False
        self.assertTrue(self.e.wait(lambda: self.e.haclient.link == "ws", 40))
        d = self.e.req("GET", "/v1/devices", who="ivan").json()
        self.assertTrue(all(not c["stale"] for c in d["devices"]))


class Vacuums(Base):
    B = "bedroom"
    LV = "vacuum.xiaomi_ov51gl_e160_robot_cleaner"
    LS = "script.vacuum_bedroom_plan"

    def test_list(self):
        v = {x["id"]: x for x in self.e.req("GET", "/v1/vacuums", who="dasha").json()["vacuums"]}
        self.assertEqual(sorted(v), ["bedroom", "livingroom"])
        self.assertEqual([s["id"] for s in v["bedroom"]["scenarios"]], ["morning", "evening"])
        self.assertEqual([s["id"] for s in v["livingroom"]["scenarios"]], ["full"])
        self.assertEqual((v["bedroom"]["state"], v["bedroom"]["battery"], v["bedroom"]["can_start"], v["bedroom"]["schedule_id"]), ("docked", 100, True, "vacuum_bedroom"))
        self.assertFalse(self.e.req("GET", "/v1/vacuums", who="ola").json()["vacuums"][0]["can_start"])

    def test_start_runs_the_existing_script_with_its_variables_and_is_confirmed(self):
        j = self.e.req("POST", "/v1/vacuums/bedroom/scenarios/evening/start", who="dasha").json()
        self.assertEqual((j["outcome"], j["action"], j["scenario"]), ("confirmed", "start", "evening"))
        self.assertEqual(self.e.ha.calls, [("script", "turn_on", "script.vacuum_bedroom_plan", {"variables": {"plan": "evening"}})])
        row = self.e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"], row["ok"]), ("vacuum_start", "dasha", "bedroom/evening", True))
        v = {x["id"]: x for x in self.e.req("GET", "/v1/vacuums", who="ivan").json()["vacuums"]}["bedroom"]
        self.assertEqual(v["running_scenario"], "evening")
        self.assertFalse(v["can_start"])

    def test_livingroom_scenario_has_no_variables(self):
        self.e.req("POST", "/v1/vacuums/livingroom/scenarios/full/start", who="ivan")
        self.assertEqual(self.e.ha.calls, [("script", "turn_on", "script.vacuum_livingroom_plan", {})])

    def test_a_second_start_while_a_plan_runs_is_refused_with_the_reason(self):
        self.e.req("POST", "/v1/vacuums/bedroom/scenarios/morning/start", who="ivan")
        n = len(self.e.ha.calls)
        r = self.e.req("POST", "/v1/vacuums/bedroom/scenarios/evening/start", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "busy"))
        self.assertEqual(len(self.e.ha.calls), n)

    def test_start_is_refused_when_unavailable_or_not_docked(self):
        self.e.ha.set(self.LV, "unavailable")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.LV) == "unavailable"))
        r = self.e.req("POST", "/v1/vacuums/bedroom/scenarios/morning/start", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "robot_unavailable"))
        self.e.ha.set(self.LV, "idle")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.LV) == "idle"))
        r = self.e.req("POST", "/v1/vacuums/bedroom/scenarios/morning/start", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_docked"))
        self.assertEqual(self.e.ha.calls, [])
        self.assertFalse(self.e.db.audit_rows(1)[0]["ok"])

    def test_readonly_cannot_and_unknown_things_are_404(self):
        self.assertEqual(self.e.req("POST", "/v1/vacuums/bedroom/scenarios/morning/start", who="ola").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/vacuums/bedroom/pause", who="ola").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/vacuums/attic/scenarios/x/start", who="ivan").status_code, 404)
        self.assertEqual(self.e.req("POST", "/v1/vacuums/bedroom/scenarios/nope/start", who="ivan").status_code, 404)

    def test_pause_needs_a_cleaning_robot_and_is_confirmed(self):
        r = self.e.req("POST", "/v1/vacuums/bedroom/pause", who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "not_running"))
        self.e.ha.set(self.LV, "cleaning")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.LV) == "cleaning"))
        j = self.e.req("POST", "/v1/vacuums/bedroom/pause", who="ivan").json()
        self.assertEqual((j["outcome"], j["observed"]["vacuum"]), ("confirmed", "paused"))

    def test_dock_stops_the_plan_and_sends_the_robot_home(self):
        self.e.req("POST", "/v1/vacuums/bedroom/scenarios/evening/start", who="ivan")
        self.e.ha.set(self.LV, "cleaning")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.LV) == "cleaning"))
        self.e.ha.calls.clear()
        j = self.e.req("POST", "/v1/vacuums/bedroom/dock", who="ivan").json()
        self.assertEqual(j["outcome"], "confirmed")
        self.assertEqual([c[:3] for c in self.e.ha.calls], [("script", "turn_off", "script.vacuum_bedroom_plan"), ("vacuum", "return_to_base", self.LV)])
        self.assertEqual(self.e.haclient.state(self.LS), "off")
        v = {x["id"]: x for x in self.e.req("GET", "/v1/vacuums", who="ivan").json()["vacuums"]}["bedroom"]
        self.assertTrue(v["last_result"].startswith("stopped"))

    def test_dock_when_already_docked_is_idempotent(self):
        j = self.e.req("POST", "/v1/vacuums/bedroom/dock", who="ivan").json()
        self.assertEqual(j["outcome"], "confirmed")
        self.assertEqual(self.e.ha.calls, [])


if __name__ == "__main__":
    unittest.main()
