import os
import sys
import shutil
import tempfile
import unittest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import hand_edit
import coverage_check
from sched import render
from tests.helpers import make_store, SYSTEM

BASELINE = os.path.join(ROOT, "baseline")
ORDER = ["plant_cacti", "plant_rack1", "plant_rack2", "plant_orchids", "parrot_uv", "vacuum_livingroom", "vacuum_bedroom", "bathroom_towels"]


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="migr-")
        self.hand = os.path.join(self.tmp, "hand")
        self.gen = os.path.join(self.tmp, "generated")
        os.makedirs(self.hand)
        for n in ("automations.yaml", "scripts.yaml"):
            shutil.copy(os.path.join(BASELINE, n), self.hand)
        self.store = make_store()
        self.sync_generated()

    def sync_generated(self):
        shutil.rmtree(self.gen, ignore_errors=True)
        for rel, text in render.render(self.store.snapshot())["files"].items():
            p = os.path.join(self.gen, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)

    def cov(self):
        return coverage_check.check(BASELINE, self.hand, self.gen)

    def cutover(self, entity):
        a = open(os.path.join(self.hand, "automations.yaml")).read()
        s = open(os.path.join(self.hand, "scripts.yaml")).read()
        na, ns = hand_edit.edit(entity, a, s)
        hand_edit.write_in_place(os.path.join(self.hand, "automations.yaml"), na)
        hand_edit.write_in_place(os.path.join(self.hand, "scripts.yaml"), ns)
        self.store.patch_entity(SYSTEM, entity, {"managed": True})
        self.sync_generated()

    def test_baseline_is_covered_by_the_hand_written_files_alone(self):
        r = self.cov()
        self.assertEqual(r["problems"], [])
        self.assertEqual(r["edges_baseline"], 238)

    def test_init_adds_the_webhook_and_the_reconcile_call_and_changes_no_edge(self):
        a = open(os.path.join(self.hand, "automations.yaml")).read()
        s = open(os.path.join(self.hand, "scripts.yaml")).read()
        na, ns = hand_edit.init(a, s)
        self.assertIn("portal_apply_reload", na)
        self.assertEqual(ns.count("script.sch_reconcile_all"), 1)
        for t in (na, ns):
            yaml.safe_load(t.replace("!secret portal_reload_webhook", "x"))
        with self.assertRaises(ValueError):
            hand_edit.init(na, ns)

    def test_every_step_of_the_migration_keeps_each_edge_exactly_once(self):
        a0 = open(os.path.join(self.hand, "automations.yaml")).read()
        s0 = open(os.path.join(self.hand, "scripts.yaml")).read()
        na, ns = hand_edit.init(a0, s0)
        hand_edit.write_in_place(os.path.join(self.hand, "automations.yaml"), na)
        hand_edit.write_in_place(os.path.join(self.hand, "scripts.yaml"), ns)
        for e in ORDER:
            before = self.cov()
            self.assertEqual(before["problems"], [], "before cutting over %s" % e)
            self.cutover(e)
            r = self.cov()
            self.assertEqual(r["problems"], [], "after cutting over %s: %s" % (e, r["problems"][:3]))
            self.assertEqual(r["edges_now"], 238)
            if self.store.entity(e)["kind"] == "plug":
                self.assertIn(self.store.entity(e)["ha_target"], r["generated_entities"])
        txt = open(os.path.join(self.hand, "automations.yaml")).read().replace("!secret portal_reload_webhook", "x")
        self.assertEqual({x["id"] for x in yaml.safe_load(txt)}, {"reconcile_all_on_start", "portal_apply_reload"},
                         "only the start-up reconcile and the reload webhook are left in the hand-written automations")
        sc = yaml.safe_load(open(os.path.join(self.hand, "scripts.yaml")))
        self.assertEqual(sorted(sc), ["reconcile_all_plugs", "vacuum_bedroom_plan", "vacuum_livingroom_plan"])
        self.assertEqual(sc["reconcile_all_plugs"]["sequence"], [{"service": "script.sch_reconcile_all"}])

    def test_double_coverage_and_gaps_are_detected(self):
        # rendered but hand-written copy still there: every edge twice
        self.store.patch_entity(SYSTEM, "plant_rack1", {"managed": True})
        self.sync_generated()
        r = self.cov()
        self.assertTrue([p for p in r["problems"] if "baseline 1, now 2" in p])
        self.assertTrue([p for p in r["problems"] if "reconcile for switch.living_room_rack" in p])
        # hand-written removed but nothing rendered: edges missing
        self.store.patch_entity(SYSTEM, "plant_rack1", {"managed": False})
        self.sync_generated()
        a = open(os.path.join(self.hand, "automations.yaml")).read()
        s = open(os.path.join(self.hand, "scripts.yaml")).read()
        na, ns = hand_edit.edit("plant_rack1", a, s)
        hand_edit.write_in_place(os.path.join(self.hand, "automations.yaml"), na)
        hand_edit.write_in_place(os.path.join(self.hand, "scripts.yaml"), ns)
        r = self.cov()
        self.assertTrue([p for p in r["problems"] if "baseline 1, now 0" in p])

    def test_rollback_restores_the_exact_previous_files(self):
        orig = {n: open(os.path.join(self.hand, n)).read() for n in ("automations.yaml", "scripts.yaml")}
        self.cutover("parrot_uv")
        self.assertNotEqual(open(os.path.join(self.hand, "automations.yaml")).read(), orig["automations.yaml"])
        for n, t in orig.items():                          # rollback = put the previous hand-written files back + un-manage
            hand_edit.write_in_place(os.path.join(self.hand, n), t)
        self.store.patch_entity(SYSTEM, "parrot_uv", {"managed": False})
        self.sync_generated()
        self.assertEqual(self.cov()["problems"], [])

    def test_in_place_write_keeps_the_inode(self):
        p = os.path.join(self.hand, "automations.yaml")
        ino = os.stat(p).st_ino
        self.cutover("vacuum_livingroom")
        self.assertEqual(os.stat(p).st_ino, ino)

    def test_the_plant_light_blocks_shrink_one_plug_at_a_time(self):
        self.cutover("plant_rack1")
        by_id = {x["id"]: x for x in yaml.safe_load(open(os.path.join(self.hand, "automations.yaml")))}
        self.assertEqual(by_id["sched_plant_lights_on"]["action"][0]["target"]["entity_id"],
                         ["switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver"])
        self.assertNotIn("reconcile_rack1_reconnect", by_id)
        s = open(os.path.join(self.hand, "scripts.yaml")).read()
        self.assertEqual(s.count("- switch.living_room_rack"), 0)
        self.assertEqual(s.count("- switch.livingroom_rack_big"), 1)


if __name__ == "__main__":
    unittest.main()
