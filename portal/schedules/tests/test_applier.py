import os
import sys
import json
import tempfile
import unittest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "applier"))
import portal_apply
from tests.helpers import make_store, SYSTEM

ALL = [1, 2, 3, 4, 5, 6, 7]


class FakeHA:
    """Simulates what Home Assistant does with generated/: on reload it parses the files on disk (exactly what the real one would load)."""
    def __init__(self, gen):
        self.gen, self.autos, self.scripts = gen, {}, {}
        self.reloads = 0

    def reload_now(self):
        self.reloads += 1
        self.autos, self.scripts = {}, {}
        for root, _d, names in os.walk(self.gen):
            for n in names:
                doc = yaml.safe_load(open(os.path.join(root, n)))
                if "automations" in root.replace(os.sep, "/"):
                    for a in doc:
                        self.autos[a["id"]] = a["alias"]
                else:
                    for k, v in doc.items():
                        self.scripts[k] = v["alias"]


class TestApplier(portal_apply.Applier):
    def __init__(self, cfg, ha):
        super().__init__(cfg, log=lambda *_a: None)
        self.ha, self.alerts, self.pushed, self.sleep = ha, [], [], lambda _s: None
        self.cc_result, self.reload_ok, self.push_ok = (True, ""), True, True

    def alert(self, key, text, clear=False):
        self.alerts.append((key, "clear" if clear else "alert", text))

    def check_config(self):
        return self.cc_result

    def reload(self):
        if not self.reload_ok:
            return False, "HA unreachable"
        self.ha.reload_now()
        return True, "HTTP 200"

    def loaded(self):
        return set(self.ha.autos.values()), dict(self.ha.scripts)

    def git_commit_push(self, message):
        self.pushed.append(message)
        return self.push_ok, ("pushed" if self.push_ok else "push failed")

    def verify_loaded(self, expected, timeout=None):
        return super().verify_loaded(expected, timeout=0 if timeout is None else timeout)


class Applier(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        for e in ("plant_rack1", "bathroom_towels"):
            self.store.patch_entity(SYSTEM, e, {"managed": True})
        self.tmp = tempfile.mkdtemp(prefix="applier-test-")
        self.gen = os.path.join(self.tmp, "generated")
        os.makedirs(self.gen)
        self.ha = FakeHA(self.gen)
        cfg = {"out": self.store.out, "gen": self.gen, "status": os.path.join(self.tmp, "apply-status.json"), "state": os.path.join(self.tmp, "state"),
               "webhook_file": os.path.join(self.tmp, "wh")}
        self.ap = TestApplier(cfg, self.ha)
        self.inode = os.stat(self.gen).st_ino

    def edit(self, start="07:30"):
        v = self.store.schedule("plant_rack1")["version"]
        self.store.put_schedule(SYSTEM, "plant_rack1", [{"id": "day", "days": ALL, "start": start, "end": "20:00"}], v)

    def status(self):
        return json.load(open(os.path.join(self.tmp, "apply-status.json")))

    def test_success_path(self):
        self.assertEqual(self.ap.apply(), "applied")
        st = self.status()
        self.assertEqual((st["result"], st["loaded_ok"]), ("applied", True))
        self.assertIn("sch_plant_rack1_day_on", self.ha.autos)
        self.assertIn("sch_reconcile_bathroom_towels", self.ha.scripts)
        self.assertEqual(len(self.ap.pushed), 1)
        self.assertEqual(os.stat(self.gen).st_ino, self.inode, "generated/ must never be replaced: it is a bind mount")
        self.assertEqual(self.ap.apply(), "verified")             # nothing new: only re-verification, no reload, no commit
        self.assertEqual((self.ha.reloads, len(self.ap.pushed)), (1, 1))

    def test_a_change_reaches_ha_and_unchanged_files_are_not_rewritten(self):
        self.ap.apply()
        before = {p: os.stat(os.path.join(self.gen, p)).st_mtime_ns for p in ("automations/sch_bathroom_towels.yaml", "automations/sch_plant_rack1.yaml")}
        self.edit("07:45")
        self.assertEqual(self.ap.apply(), "applied")
        self.assertIn("07:45", yaml.safe_load(open(os.path.join(self.gen, "automations/sch_plant_rack1.yaml")))[0]["alias"])
        self.assertEqual(os.stat(os.path.join(self.gen, "automations/sch_bathroom_towels.yaml")).st_mtime_ns, before["automations/sch_bathroom_towels.yaml"])
        self.assertEqual(self.ha.autos["sch_plant_rack1_day_on"], yaml.safe_load(open(os.path.join(self.gen, "automations/sch_plant_rack1.yaml")))[0]["alias"])

    def test_check_config_failure_restores_the_previous_files_and_does_not_reload(self):
        self.ap.apply()
        good = self.ap.read_gen()
        reloads = self.ha.reloads
        self.edit("07:45")
        self.ap.cc_result = (False, "Invalid config for automation")
        self.assertEqual(self.ap.apply(), "failed")
        self.assertEqual(self.ap.read_gen(), good)
        self.assertEqual(self.ha.reloads, reloads)
        st = self.status()
        self.assertEqual(st["result"], "failed")
        self.assertIn("check_config", st["detail"])
        self.assertTrue([a for a in self.ap.alerts if a[0] == "portal-apply" and a[1] == "alert"])

    def test_ha_loading_something_else_is_a_failure_and_is_rolled_back(self):
        self.ap.apply()
        good_files, good_autos = self.ap.read_gen(), dict(self.ha.autos)
        self.edit("07:45")
        real_reload, calls = self.ap.reload, []

        def flaky():                    # the first reload (of the new render) silently loses one automation; the rollback reload is clean
            ok, msg = real_reload()
            if not calls:
                calls.append(1)
                self.ha.autos.pop("sch_plant_rack1_day_on")
            return ok, msg
        self.ap.reload = flaky
        self.assertEqual(self.ap.apply(), "failed")
        self.assertEqual(self.ap.read_gen(), good_files)
        self.assertEqual(self.ha.autos, good_autos, "after the rollback HA must run the previous generation again")
        self.assertIn("did not load what was rendered", self.status()["detail"])

    def test_ha_down_keeps_the_files_and_stays_pending(self):
        self.ap.reload_ok = False
        self.assertEqual(self.ap.apply(), "pending")
        self.assertEqual(self.status()["result"], "pending")
        self.assertTrue(self.ap.read_gen())                       # files stay: HA picks them up at its next start
        self.ap.reload_ok = True
        self.assertEqual(self.ap.apply(), "applied")

    def test_the_same_failed_render_is_not_retried_in_a_loop(self):
        self.ap.cc_result = (False, "bad")
        self.assertEqual(self.ap.apply(), "failed")
        self.assertEqual(self.ap.apply(), "failed-held")
        self.assertEqual(self.ap.apply(force=True), "failed")
        self.ap.cc_result = (True, "")
        self.assertEqual(self.ap.apply(force=True), "applied")

    def test_incomplete_render_changes_nothing(self):
        open(os.path.join(self.store.out, "READY"), "w").write("999 deadbeef\n")
        self.assertEqual(self.ap.apply(), "no-render")
        self.assertEqual(self.ap.read_gen(), {})
        self.store.publish()
        # a rendered file whose hash does not match
        meta = json.load(open(os.path.join(self.store.out, "render.json")))
        victim = os.path.join(self.store.out, "render", sorted(meta["files"])[0])
        open(victim, "a").write("# tampered\n")
        self.assertEqual(self.ap.apply(), "no-render")
        self.assertEqual(self.ap.read_gen(), {})

    def test_mismatch_is_alerted_only_when_it_repeats(self):
        self.ap.apply()
        self.ha.autos.pop("sch_bathroom_towels_morning_on")        # e.g. HA restarted and lost an automation
        self.assertEqual(self.ap.apply(), "mismatch")
        self.assertFalse([a for a in self.ap.alerts if a[0] == "portal-mismatch" and a[1] == "alert"])
        self.assertEqual(self.ap.apply(), "mismatch")
        self.assertTrue([a for a in self.ap.alerts if a[0] == "portal-mismatch" and a[1] == "alert"])
        self.ha.reload_now()
        self.assertEqual(self.ap.apply(), "verified")
        self.assertEqual(self.ap.alerts[-1][:2], ("portal-mismatch", "clear"))

    def test_compare_finds_extras_and_wrong_hashes(self):
        exp = {"automations": {"sch_a": "Sch a [sch 11111111]"}, "scripts": {"sch_s": "Sch s [sch 22222222]"}}
        c = portal_apply.Applier.compare
        self.assertEqual(c(exp, {"Sch a [sch 11111111]", "Schedule: hand written"}, {"sch_s": "Sch s [sch 22222222]", "reconcile_x": "y"}), [])
        d = c(exp, {"Sch a [sch 99999999]", "Sch x [sch 12345678]"}, {})
        self.assertEqual(len(d), 4)       # expected one missing, its script missing, two generated-shaped extras

    def test_unmanaging_an_entity_removes_its_files_and_automations(self):
        self.ap.apply()
        self.store.patch_entity(SYSTEM, "plant_rack1", {"managed": False})
        self.assertEqual(self.ap.apply(), "applied")
        self.assertFalse(os.path.exists(os.path.join(self.gen, "automations/sch_plant_rack1.yaml")))
        self.assertFalse([k for k in self.ha.autos.values() if "plant_rack1" in k])

    def test_push_failure_is_reported_but_the_apply_stands(self):
        self.ap.push_ok = False
        self.assertEqual(self.ap.apply(), "applied")
        self.assertTrue([a for a in self.ap.alerts if a[0] == "portal-push" and a[1] == "alert"])


class CheckConfigJudge(unittest.TestCase):
    ESC = chr(27)

    def test_clean_run(self):
        out = self.ESC + "[1mTesting configuration at /config" + self.ESC + "[0m" + chr(10)
        self.assertEqual(portal_apply.judge_check_config(0, out), (True, ""))

    def test_an_error_line_fails_even_with_exit_code_0(self):
        out = ("Testing configuration at /config" + chr(10) + "ERROR:homeassistant.components.automation:Automation with alias 'Sch x' "
               "failed to setup triggers and has been disabled: Invalid trigger" + chr(10))
        ok, why = portal_apply.judge_check_config(0, out)
        self.assertFalse(ok)
        self.assertIn("has been disabled", why)

    def test_failed_config_and_nonzero_exit(self):
        self.assertFalse(portal_apply.judge_check_config(0, "Failed config" + chr(10) + "  automation: bad" + chr(10))[0])
        self.assertFalse(portal_apply.judge_check_config(1, "Testing configuration at /config" + chr(10))[0])

    def test_warnings_alone_do_not_fail(self):
        self.assertTrue(portal_apply.judge_check_config(0, "Testing configuration at /config" + chr(10) + "WARNING:x.y:old thing" + chr(10))[0])


if __name__ == "__main__":
    unittest.main()
