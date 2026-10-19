import re
import datetime as dt
import unittest
import yaml
from sched import render, model
from tests.helpers import make_store, SYSTEM


def snap_of(store, managed=()):
    s = store.snapshot()
    for e in s["entities"]:
        e["managed"] = e["id"] in managed or e["managed"]
    return s


ALL = [1, 2, 3, 4, 5, 6, 7]


class Render(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def r(self, managed):
        return render.render(snap_of(self.store, managed))

    def test_everything_parses_as_yaml_and_has_the_ha_shape(self):
        out = self.r({"bathroom_towels", "plant_rack1", "parrot_uv", "vacuum_bedroom", "vacuum_livingroom"})
        for path, text in out["files"].items():
            doc = yaml.safe_load(text)
            self.assertTrue(text.startswith("# GENERATED"), path)
            if path.startswith("automations/"):
                self.assertIsInstance(doc, list, path)
                for a in doc:
                    self.assertRegex(a["alias"], r"\[sch [0-9a-f]{8}\]$")
                    self.assertTrue(a["id"].startswith("sch_"))
            else:
                self.assertIsInstance(doc, dict, path)
                for k, v in doc.items():
                    self.assertTrue(k.startswith("sch_"))
                    self.assertIn("sequence", v)

    def test_deterministic(self):
        self.assertEqual(self.r({"bathroom_towels", "plant_rack1"}), self.r({"plant_rack1", "bathroom_towels"}))

    def test_nothing_managed_renders_only_inert_files(self):
        out = self.r(set())
        self.assertEqual(sorted(out["files"]), ["automations/sch_none.yaml", "scripts/sch_fingerprint.yaml", "scripts/sch_reconcile_all.yaml"])
        self.assertEqual(yaml.safe_load(out["files"]["automations/sch_none.yaml"]), [])

    def test_dryer_automations(self):
        doc = yaml.safe_load(self.r({"bathroom_towels"})["files"]["automations/sch_bathroom_towels.yaml"])
        by_id = {a["id"]: a for a in doc}
        self.assertEqual(sorted(by_id), ["sch_bathroom_towels_evening_off", "sch_bathroom_towels_evening_on", "sch_bathroom_towels_failsafe",
                                         "sch_bathroom_towels_morning_off", "sch_bathroom_towels_morning_on", "sch_bathroom_towels_reconnect"])
        self.assertEqual(by_id["sch_bathroom_towels_morning_on"]["trigger"][0]["at"], "06:30:00")
        self.assertEqual(by_id["sch_bathroom_towels_evening_off"]["trigger"][0]["at"], "20:00:00")
        fs = by_id["sch_bathroom_towels_failsafe"]
        self.assertEqual(fs["trigger"], [{"platform": "time_pattern", "minutes": "/1"}])
        self.assertEqual(fs["condition"][0], {"condition": "state", "entity_id": "switch.bathroom_towels", "state": "on"})
        self.assertIn("> 7200", fs["condition"][1]["value_template"])
        self.assertEqual(fs["action"][0], {"service": "switch.turn_off", "target": {"entity_id": "switch.bathroom_towels"}})
        self.assertEqual(by_id["sch_bathroom_towels_reconnect"]["trigger"][0]["from"], "unavailable")

    def test_failsafe_follows_the_guardrail(self):
        self.store.set_guardrails(SYSTEM, "plant_rack1", {"max_on_minutes": 800, "allowed_from": "05:00", "allowed_to": "22:00", "min_gap_minutes": 360})
        doc = yaml.safe_load(self.r({"plant_rack1"})["files"]["automations/sch_plant_rack1.yaml"])
        fs = [a for a in doc if a["id"].endswith("failsafe")][0]
        self.assertIn("> 48000", fs["condition"][1]["value_template"])      # 800 min

    def test_vacuum_triggers_carry_variables(self):
        doc = yaml.safe_load(self.r({"vacuum_bedroom"})["files"]["automations/sch_vacuum_bedroom.yaml"])
        m = [a for a in doc if a["id"] == "sch_vacuum_bedroom_morning"][0]
        self.assertEqual(m["action"], [{"action": "script.turn_on", "target": {"entity_id": "script.vacuum_bedroom_plan"},
                                        "data": {"variables": {"plan": "morning"}}}])
        self.assertEqual(m["trigger"][0]["at"], "09:00:00")
        live = yaml.safe_load(self.r({"vacuum_livingroom"})["files"]["automations/sch_vacuum_livingroom.yaml"])
        self.assertNotIn("data", live[0]["action"][0])

    def test_hash_changes_exactly_when_the_definition_changes(self):
        a = self.r({"plant_rack1", "plant_rack2"})
        sch = self.store.schedule("plant_rack1")
        self.store.put_schedule(SYSTEM, "plant_rack1", [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}], sch["version"])
        b = self.r({"plant_rack1", "plant_rack2"})
        self.assertNotEqual(a["set_hash"], b["set_hash"])
        self.assertNotEqual(a["files"]["automations/sch_plant_rack1.yaml"], b["files"]["automations/sch_plant_rack1.yaml"])
        self.assertEqual(a["files"]["automations/sch_plant_rack2.yaml"], b["files"]["automations/sch_plant_rack2.yaml"])
        self.assertEqual(a["files"]["scripts/sch_reconcile_plant_rack2.yaml"], b["files"]["scripts/sch_reconcile_plant_rack2.yaml"])

    def test_weekday_and_skip_conditions(self):
        sch = self.store.schedule("plant_rack1")
        self.store.put_schedule(SYSTEM, "plant_rack1", [{"id": "day", "days": [1, 2, 3, 4, 5], "start": "07:00", "end": "20:00"}], sch["version"])
        self.store.add_exception(SYSTEM, "plant_rack1", {"kind": "skip_date", "date": "2026-10-21"}, today=dt.date(2026, 10, 19))
        doc = yaml.safe_load(self.r({"plant_rack1"})["files"]["automations/sch_plant_rack1.yaml"])
        on = [a for a in doc if a["id"].endswith("_on")][0]
        off = [a for a in doc if a["id"].endswith("_off")][0]
        self.assertEqual(on["condition"][0], {"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]})
        self.assertIn("2026-10-21", on["condition"][1]["value_template"])
        self.assertEqual(len(off["condition"]), 1)             # the OFF edge ignores skipped dates on purpose

    def test_reconcile_template_equals_the_model_for_every_minute(self):
        """The generated Jinja window test must give the same answer as model.target_state for every minute of every weekday."""
        sch = self.store.schedule("bathroom_towels")
        self.store.put_schedule(SYSTEM, "bathroom_towels", [
            {"id": "morning", "days": [1, 2, 3, 4, 5], "start": "06:30", "end": "08:00"},
            {"id": "evening", "days": ALL, "start": "18:30", "end": "20:00"}], sch["version"])
        out = self.r({"bathroom_towels"})
        doc = yaml.safe_load(out["files"]["scripts/sch_reconcile_bathroom_towels.yaml"])
        tmpl = doc["sch_reconcile_bathroom_towels"]["sequence"][0]["variables"]["target"]
        expr = re.match(r"^\{\{ (.*) \}\}$", tmpl).group(1)
        snap = snap_of(self.store, {"bathroom_towels"})
        ent = [e for e in snap["entities"] if e["id"] == "bathroom_towels"][0]
        for dow in range(1, 8):
            day = dt.date(2026, 10, 18 + dow)          # 2026-10-19 is a Monday
            self.assertEqual(day.isoweekday(), dow)
            for t in range(1440):
                at = dt.datetime(day.year, day.month, day.day, t // 60, t % 60, tzinfo=model.TZ)
                got = eval(expr.replace(" not in ", " not in ").replace("'on' if", "'on' if"), {"__builtins__": {}},
                           {"t": t, "dow": dow, "today": day.isoformat()})
                self.assertEqual(got, model.target_state("plug", ent["rules"], at), (dow, t))


if __name__ == "__main__":
    unittest.main()
