#!/usr/bin/env python3
"""Task 35b-1: unit tests of ha_guard.py (synthetic snapshot and states; nothing is read from the house). Run: python3 test_ha_guard.py"""
import importlib.util
import os
import unittest
from datetime import datetime

spec = importlib.util.spec_from_file_location("hg", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_guard.py"))
hg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hg)

ALL = [1, 2, 3, 4, 5, 6, 7]
SNAP = {"schema": 1, "entities": [
    {"id": "dryer", "kind": "plug", "ha_target": "switch.dryer", "rules": [{"days": ALL, "start": "06:30", "end": "08:00"}, {"days": ALL, "start": "18:30", "end": "20:00"}]},
    {"id": "rack1", "kind": "plug", "ha_target": "switch.rack1", "rules": [{"days": ALL, "start": "07:00", "end": "20:00"}]},
    {"id": "uv", "kind": "plug", "ha_target": "switch.uv", "rules": [{"days": ALL, "start": "11:00", "end": "18:00"}]},
    {"id": "vac_lr", "kind": "vacuum_plan", "ha_target": "script.vac_lr", "rules": [{"days": ALL, "start": "10:00", "end": None}]},
    {"id": "vac_ev", "kind": "vacuum_plan", "ha_target": "script.vac_ev", "rules": [{"days": ALL, "start": "20:00", "end": None}], "skip_dates": ["2026-11-15"]},
    {"id": "child", "kind": "child_window", "ha_target": None, "rules": [{"days": ALL, "start": "16:00", "end": "19:00"}]},
]}
SUNDAY = "2026-11-15"


def at(hhmm, date=SUNDAY):
    return datetime.fromisoformat("%sT%s:00" % (date, hhmm))


def idle(**kw):
    st = {"switch.dryer": "off", "switch.rack1": "on", "switch.uv": "off", "script.vac_lr": "off", "script.vac_ev": "off"}
    st.update(kw)
    return st


class Guard(unittest.TestCase):
    def test_quiet_afternoon_is_go(self):
        self.assertEqual(hg.blockers(at("13:00"), SNAP, idle()), [])

    def test_plant_light_on_inside_its_window_does_not_block(self):
        self.assertEqual(hg.blockers(at("14:00"), SNAP, idle(**{"switch.rack1": "on"})), [])

    def test_vacuum_plan_script_running_blocks(self):
        b = hg.blockers(at("10:30"), SNAP, idle(**{"script.vac_lr": "on"}))
        self.assertTrue(any("vacuum plan running (vac_lr)" in x for x in b), b)

    def test_edge_window_ten_minutes_around_a_plan_start(self):
        for t, blocked in (("09:49", False), ("09:50", True), ("10:00", True), ("10:10", True), ("10:11", False)):
            b = hg.blockers(at(t), SNAP, idle())
            self.assertEqual(any("vac_lr plan starts" in x for x in b), blocked, (t, b))

    def test_plug_window_edges_block_too(self):
        self.assertTrue(any("uv starts 11:00" in x for x in hg.blockers(at("11:05"), SNAP, idle())))
        self.assertTrue(any("uv ends 18:00" in x for x in hg.blockers(at("17:55"), SNAP, idle(**{"switch.uv": "on"}))))

    def test_hand_started_plug_outside_its_window_blocks(self):
        b = hg.blockers(at("13:00"), SNAP, idle(**{"switch.dryer": "on"}))
        self.assertTrue(any("plug ON outside its schedule window (dryer)" in x for x in b), b)

    def test_uv_on_before_its_window_is_a_hand_run(self):
        b = hg.blockers(at("09:00"), SNAP, idle(**{"switch.uv": "on"}))
        self.assertTrue(any("(uv)" in x for x in b), b)

    def test_skip_date_removes_the_edge(self):
        self.assertFalse(any("vac_ev" in x for x in hg.blockers(at("20:00", SUNDAY), SNAP, idle())))
        self.assertTrue(any("vac_ev" in x for x in hg.blockers(at("20:00", "2026-11-16"), SNAP, idle())))

    def test_weekday_rules_are_honoured(self):
        snap = {"schema": 1, "entities": [{"id": "x", "kind": "plug", "ha_target": "switch.x", "rules": [{"days": [6], "start": "11:45", "end": "12:00"}]}]}
        self.assertEqual(hg.blockers(at("11:50", SUNDAY), snap, {"switch.x": "off"}), [])                    # Sunday: no rule
        self.assertTrue(any("x starts" in b or "x ends" in b for b in hg.blockers(at("11:50", "2026-11-14"), snap, {"switch.x": "off"})))

    def test_active_timed_run_blocks(self):
        ts = at("13:00").timestamp()
        snap = {"schema": 1, "entities": [{"id": "t", "kind": "plug", "ha_target": "switch.t", "rules": [], "runs": [{"start_ts": ts - 600, "end_ts": ts + 3000}]}]}
        self.assertTrue(any("timed run active (t)" in b for b in hg.blockers(at("13:00"), snap, {"switch.t": "on"})))

    def test_a_timed_run_ending_in_a_few_minutes_blocks_one_long_ended_does_not(self):
        ts = at("13:00").timestamp()
        snap = {"schema": 1, "entities": [{"id": "t", "kind": "plug", "ha_target": "switch.t", "rules": [], "runs": [{"start_ts": ts - 3000, "end_ts": ts + 300}]}]}
        self.assertTrue(hg.blockers(at("13:00"), snap, {"switch.t": "on"}))
        snap["entities"][0]["runs"] = [{"start_ts": ts - 7200, "end_ts": ts - 3600}]
        self.assertEqual(hg.blockers(at("13:00"), snap, {"switch.t": "off"}), [])

    def test_unreadable_states_block(self):
        self.assertTrue(hg.blockers(at("13:00"), SNAP, None))

    def test_read_states_failure_is_none_never_idle(self):
        def boom(req):
            raise OSError("down")
        self.assertIsNone(hg.read_states(SNAP, url="http://x", token_file=__file__, opener=boom))
        self.assertIsNone(hg.read_states(SNAP, url="http://x", token_file="/nonexistent"))

    def test_read_states_uses_the_bearer_token_and_the_state_field(self):
        import io
        import json
        seen = []

        def op(req):
            seen.append((req.full_url, req.get_header("Authorization")))
            return io.StringIO(json.dumps({"state": "on"}))
        tf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tok.tmp")
        open(tf, "w").write("sekret\n")
        try:
            st = hg.read_states(SNAP, url="http://ha", token_file=tf, opener=op)
        finally:
            os.remove(tf)
        self.assertEqual(st["switch.dryer"], "on")
        self.assertTrue(all(a == "Bearer sekret" for _, a in seen))
        self.assertEqual(len(seen), 5)                                    # 3 plugs + 2 plan scripts, no child window


if __name__ == "__main__":
    unittest.main()
