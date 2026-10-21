"""Adding a smart plug as an entity (the citrus plug, 2026-10-21): admin only, copies windows/guardrails of an existing plug, starts unmanaged, validated, audited."""
import json
import os
import unittest

from tests.helpers import Env


class AddPlug(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def add(self, body, role="admin"):
        return self.e.req("POST", "/v1/entities", role=role, json=body)

    def test_add_copies_the_plant_light_and_starts_unmanaged(self):
        r = self.add({"id": "plant_citrus", "name": "Plant light - citrus (2nd floor)", "ha_target": "switch.citrus_2nd_floor", "like": "plant_rack1"})
        self.assertEqual(r.status_code, 201, r.text)
        ent = r.json()
        self.assertEqual((ent["id"], ent["kind"], ent["ha_target"], ent["managed"]), ("plant_citrus", "plug", "switch.citrus_2nd_floor", False))
        like = next(x for x in self.e.req("GET", "/v1/entities").json()["entities"] if x["id"] == "plant_rack1")
        self.assertEqual((ent["guardrails"], ent["flags"]), (like["guardrails"], like["flags"]))
        s = self.e.req("GET", "/v1/schedules/plant_citrus").json()
        self.assertEqual([(x["start"], x["end"]) for x in s["rules"]], [("07:00", "20:00")])
        self.assertEqual(s["version"], 1)
        # unmanaged: nothing of it is rendered into Home Assistant
        meta = json.load(open(os.path.join(self.e.store.out, "render.json")))
        self.assertFalse([f for f in meta["files"] if "citrus" in f])
        row = next(x for x in self.e.req("GET", "/v1/audit?limit=5").json()["audit"] if x["action"] == "entity_add")
        self.assertEqual((row["entity"], row["after"]["like"]), ("plant_citrus", "plant_rack1"))

    def test_the_window_is_editable_and_the_plug_can_be_managed_afterwards(self):
        self.add({"id": "plant_citrus", "name": "Citrus", "ha_target": "switch.citrus_2nd_floor", "like": "plant_rack1"})
        g = self.e.req("GET", "/v1/schedules/plant_citrus")
        rules = [{**g.json()["rules"][0], "start": "08:00", "end": "19:00"}]
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_citrus", headers={"If-Match": g.headers["ETag"]}, json={"rules": rules}, role="household").status_code, 200)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_citrus", json={"managed": True}).status_code, 200)

    def test_refusals_and_roles(self):
        ok = {"id": "plant_citrus", "name": "Citrus", "ha_target": "switch.citrus_2nd_floor", "like": "plant_rack1"}
        for role in ("household", "readonly"):
            self.assertEqual(self.add(ok, role).status_code, 403, role)
        for bad, code in (({**ok, "id": "Plant Citrus"}, "format"), ({**ok, "id": "plant_rack1"}, "duplicate"), ({**ok, "ha_target": "switch.living_room_rack"}, "duplicate"),
                          ({**ok, "ha_target": "light.citrus"}, "format"), ({**ok, "ha_target": "switch.Citrus!"}, "format"), ({**ok, "like": "nope"}, "like"),
                          ({**ok, "like": "vacuum_bedroom"}, "like"), ({**ok, "name": ""}, "format")):
            r = self.add(bad)
            self.assertEqual(r.status_code, 422, bad)
            self.assertIn(code, [p["code"] for p in r.json()["error"]["problems"]], bad)
        for bad in ({}, {"id": "x"}, {**ok, "extra": "1"}, {**ok, "like": 3}):
            self.assertEqual(self.add(bad).status_code, 400, bad)
        self.assertEqual(self.add(ok).status_code, 201)
        self.assertEqual(self.add(ok).status_code, 422)                                         # not twice



    def test_the_ha_switch_of_a_plug_can_be_renamed(self):
        self.add({"id": "plant_citrus", "name": "Citrus", "ha_target": "switch.smartplug_citrus_2ndfloor", "like": "plant_rack1"})
        r = self.e.req("PATCH", "/v1/entities/plant_citrus", json={"ha_target": "switch.citrus_2nd_floor"})
        self.assertEqual((r.status_code, r.json()["ha_target"]), (200, "switch.citrus_2nd_floor"))
        for bad in ("light.x", "switch.Bad!", "switch.living_room_rack", 5):
            self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_citrus", json={"ha_target": bad}).status_code, 422, bad)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_citrus", role="household", json={"ha_target": "switch.x1"}).status_code, 403)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/vacuum_bedroom", json={"ha_target": "switch.x1"}).status_code, 422)         # not a plug
        row = next(x for x in self.e.req("GET", "/v1/audit?limit=3").json()["audit"] if x["action"] == "entity")
        self.assertEqual((row["before"]["ha_target"], row["after"]["ha_target"]), ("switch.smartplug_citrus_2ndfloor", "switch.citrus_2nd_floor"))


if __name__ == "__main__":
    unittest.main()
