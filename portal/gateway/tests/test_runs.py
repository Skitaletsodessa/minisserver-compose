"""Timed runs through the gateway (Task 29 Part 1): the REAL schedule service (in-process), a fake HA, and a fake applier that "loads" the render into HA.
Order proven here: the service accepts the run -> HA loads its end (armed) -> only then the plug is switched on and read back; a run that cannot be armed
switches nothing on; guardrails reject in the service; OFF closes the run only after a confirmed OFF."""
import datetime as dt
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase  # noqa: E402
from gw import config  # noqa: E402

TZ_OFFSET = None


class FakeApplier(threading.Thread):
    """Stands for portal-apply + HA: while `enabled`, it reports every change of the service as applied and loaded (apply-status.json)."""
    def __init__(self, sched):
        super().__init__(daemon=True)
        self.store, self.path = sched.store, os.path.join(sched.dir, "apply-status.json")
        self.enabled, self.stop = True, False

    def run(self):
        while not self.stop:
            if self.enabled:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump({"seq": self.store.audit_seq(), "result": "applied", "loaded_ok": True, "set_hash": "h", "at": "t"}, f)
                os.replace(tmp, self.path)
            time.sleep(0.15)


def setUpModule():
    e = BaseCase.ENV = Env()
    # frozen, plausible wall clock for the service AND the gateway: today 12:00 (the dryer's gap between its windows, inside every allowed range)
    from zoneinfo import ZoneInfo
    base = dt.datetime.now(ZoneInfo("Europe/Prague")).replace(hour=12, minute=0, second=0, microsecond=0)
    e.now = [base]
    e.sched.store.clock = lambda: e.now[0].isoformat(timespec="seconds")
    e.core.clock = lambda: e.now[0].timestamp()
    e.core.sched.clock = lambda: e.now[0].timestamp()
    for eid in ("bathroom_towels", "parrot_uv", "plant_rack1", "plant_orchids"):
        e.sched.store.patch_entity(e.sched.sys, eid, {"managed": True})
    e.applier = FakeApplier(e.sched)
    e.applier.start()
    config.RUN_ARM_WAIT_S, config.RUN_ARM_POLL_S = 1.5, 0.1


def tearDownModule():
    BaseCase.ENV.applier.stop = True
    BaseCase.ENV.close()


class Runs(BaseCase):
    DRYER, DV = "switch.bathroom_towels", "bathroom_towels"

    def setUp(self):
        super().setUp()
        e = self.e
        e.applier.enabled = True
        e.now[0] = e.now[0].replace(hour=12, minute=0, second=0)
        st = e.sched.store
        with st._tx():                                  # a clean slate: no runs from an earlier test
            st.db.execute("DELETE FROM runs")
        e.core.sched.drop_runs_cache()
        e.core._last_switch.clear()
        for did in ("bathroom_towels", "parrot_uv", "plant_rack1", "plant_orchids"):
            e.db.x("UPDATE device_meta SET switch_mode=? WHERE device_id=?", ("timed" if did in ("bathroom_towels", "parrot_uv") else "plain", did))
            e.core.devices[did]["switch_mode"] = "timed" if did in ("bathroom_towels", "parrot_uv") else "plain"
        e.ha.calls.clear()

    def tick(self, seconds):
        self.e.now[0] += dt.timedelta(seconds=seconds)
        self.e.core.sched.drop_runs_cache()

    def start(self, who="dasha", did="bathroom_towels", minutes=30, **kw):
        return self.e.req("POST", "/v1/devices/%s/run" % did, who=who, json={"minutes": minutes}, **kw)

    def service_runs(self, active=True):
        return self.e.sched.store.runs(active_only=active)

    # ── the cards ──
    def test_cards_carry_the_mode_presets_and_no_run(self):
        d = {x["id"]: x for x in self.e.req("GET", "/v1/devices", who="dasha").json()["devices"]}
        self.assertEqual((d["bathroom_towels"]["switch_mode"], d["bathroom_towels"]["run_presets"], d["bathroom_towels"]["run_max_minutes"]), ("timed", [30, 60, 120], 120))
        self.assertEqual((d["parrot_uv"]["switch_mode"], d["parrot_uv"]["run_presets"]), ("timed", [60, 120, 240]))
        self.assertEqual((d["plant_rack1"]["switch_mode"], d["plant_rack1"]["run_presets"]), ("plain", [30, 60, 120, 240]))
        self.assertTrue(all(x["run"] is None for x in d.values()))
        self.assertTrue(d["bathroom_towels"]["can_run"])
        self.assertFalse(self.e.req("GET", "/v1/devices/bathroom_towels", who="ola").json()["can_run"])

    # ── start ──
    def test_start_order_service_then_armed_then_switch_on_and_everything_is_audited(self):
        r = self.start()
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual((j["outcome"], j["action"], j["observed"]), ("confirmed", "start", "on"))
        self.assertTrue(j["run"]["armed"])
        self.assertEqual(j["run"]["started_by"], "dasha")
        self.assertEqual(self.e.ha.calls, [("switch", "turn_on", self.DRYER, {})])
        runs = self.service_runs()
        self.assertEqual([x["entity"] for x in runs], ["bathroom_towels"])
        a = [x for x in self.e.sched.store.audit(entity="bathroom_towels", limit=5) if x["action"] == "run_start"][0]
        self.assertEqual(a["actor"]["acting"]["person"], "dasha")                       # the schedule audit names the real person
        row = self.e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"], row["ok"]), ("run_start", "dasha", "bathroom_towels", True))
        self.assertEqual(row["after"]["run"], runs[0]["id"])
        d = self.e.req("GET", "/v1/devices/bathroom_towels", who="ivan").json()
        self.assertEqual((d["state"], d["source"], d["run"]["id"], d["can_run"]), ("on", "run", runs[0]["id"], False))
        self.assertAlmostEqual(d["run"]["remaining_s"], 30 * 60, delta=61)

    def test_a_started_run_ends_on_a_whole_minute_and_the_remaining_time_counts_down(self):
        j = self.start(minutes=30).json()
        self.assertTrue(j["run"]["ends_at"].endswith(":00+02:00") or j["run"]["ends_at"].endswith(":00+01:00"))
        self.tick(600)
        d = self.e.req("GET", "/v1/devices/bathroom_towels", who="ivan").json()
        self.assertAlmostEqual(d["run"]["remaining_s"], 20 * 60, delta=61)
        self.tick(25 * 60)                                                              # past the end: no run on the card any more
        d = self.e.req("GET", "/v1/devices/bathroom_towels", who="ivan").json()
        self.assertIsNone(d["run"])

    def test_guardrails_reject_in_the_service_nothing_is_switched_on(self):
        r = self.start(minutes=121)
        self.assertEqual(r.status_code, 422)
        self.assertEqual([p["code"] for p in r.json()["error"]["details"]["problems"]], ["max_on"])
        self.assertEqual(self.e.ha.calls, [])
        self.assertEqual(self.service_runs(), [])
        self.assertEqual(self.e.db.audit_rows(1)[0]["ok"], False)

    def test_a_run_overlapping_a_window_or_outside_the_range_is_rejected(self):
        self.e.now[0] = self.e.now[0].replace(hour=18, minute=0)                        # 18:00 + 60 min runs into the 18:30 window
        r = self.start(minutes=60)
        self.assertEqual((r.status_code, r.json()["error"]["details"]["problems"][0]["code"]), (422, "window_overlap"))
        self.e.now[0] = self.e.now[0].replace(hour=5, minute=0)
        r = self.start(minutes=30)
        self.assertEqual((r.status_code, r.json()["error"]["details"]["problems"][0]["code"]), (422, "allowed_range"))
        self.assertEqual(self.e.ha.calls, [])

    def test_the_gap_between_runs(self):
        j = self.start(minutes=10).json()
        self.e.core._last_switch.clear()
        self.assertEqual(self.e.req("DELETE", "/v1/devices/bathroom_towels/run", who="dasha").status_code, 200)
        self.e.core._last_switch.clear()
        r = self.start(minutes=10)
        self.assertEqual((r.status_code, r.json()["error"]["details"]["problems"][0]["code"]), (422, "min_gap"))
        self.tick(31 * 60)
        self.e.core._last_switch.clear()
        self.assertEqual(self.start(minutes=10).status_code, 200)

    def test_already_on_and_unavailable_are_refused_before_the_service_is_asked(self):
        self.e.ha.set(self.DRYER, "on")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.DRYER) == "on"))
        r = self.start()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "already_on"))
        self.assertEqual(self.service_runs(), [])
        self.e.ha.set(self.DRYER, "unavailable")
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.DRYER) == "unavailable"))
        r = self.start()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "device_unavailable"))

    def test_a_run_that_cannot_be_armed_switches_nothing_on_and_is_withdrawn(self):
        self.e.applier.enabled = False                                                  # the applier does not report: HA never loaded the end
        with open(self.e.applier.path, "w") as f:
            json.dump({"seq": 0, "result": "failed", "loaded_ok": False}, f)
        r = self.start()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "run_not_armed"))
        self.assertEqual(self.e.ha.calls, [], "the plug must NOT be switched on when its end could not be armed")
        self.assertEqual(self.service_runs(), [], "the run is withdrawn")
        self.assertEqual(self.e.sched.store.runs()[-1]["status"], "withdrawn")
        self.assertEqual(self.e.db.audit_rows(1)[0]["after"]["outcome"], "not_armed")

    def test_ha_refusing_the_switch_after_arming_withdraws_the_run(self):
        self.e.ha.refuse = True
        r = self.start()
        self.assertGreaterEqual(r.status_code, 502)
        self.e.ha.refuse = False
        self.assertEqual(self.service_runs(), [])

    def test_unconfirmed_switch_on_keeps_the_run(self):
        self.e.ha.swallow.add(self.DRYER)
        r = self.start()
        self.assertEqual((r.status_code, r.json()["outcome"]), (200, "unconfirmed"))
        self.assertEqual(len(self.service_runs()), 1)                                    # HA ends it at its end if the plug does come on late
        self.e.ha.swallow.clear()

    def test_a_second_run_while_one_is_active_is_a_conflict(self):
        self.start()
        self.e.ha.set(self.DRYER, "off")                                                 # switched off by hand during the run
        self.assertTrue(self.e.wait(lambda: self.e.haclient.state(self.DRYER) == "off"))
        self.e.core._last_switch.clear()
        r = self.start()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "run_active"))

    # ── end ──
    def test_end_switches_off_first_and_then_closes_the_run(self):
        self.start()
        self.e.ha.calls.clear()
        self.e.core._last_switch.clear()
        r = self.e.req("DELETE", "/v1/devices/bathroom_towels/run", who="dasha")
        j = r.json()
        self.assertEqual((r.status_code, j["outcome"], j["action"], j["observed"], j["run"]), (200, "confirmed", "end", "off", None))
        self.assertEqual(self.e.ha.calls, [("switch", "turn_off", self.DRYER, {})])
        self.assertEqual(self.service_runs(), [])
        self.assertEqual(self.e.sched.store.runs()[0]["status"], "cancelled")
        d = self.e.req("GET", "/v1/devices/bathroom_towels", who="ivan").json()
        self.assertEqual((d["state"], d["run"]), ("off", None))

    def test_an_unconfirmed_off_leaves_the_run_in_place(self):
        self.start()
        self.e.core._last_switch.clear()
        self.e.ha.swallow.add(self.DRYER)
        j = self.e.req("DELETE", "/v1/devices/bathroom_towels/run", who="dasha").json()
        self.assertEqual(j["outcome"], "unconfirmed")
        self.assertEqual(len(self.service_runs()), 1, "HA still ends it at its end")
        self.e.ha.swallow.clear()

    def test_the_plain_off_endpoint_also_closes_the_run(self):
        self.start()
        self.e.core._last_switch.clear()
        j = self.e.req("POST", "/v1/devices/bathroom_towels/switch", who="dasha", json={"on": False}).json()
        self.assertEqual(j["outcome"], "confirmed")
        self.assertEqual(self.service_runs(), [])

    def test_end_without_a_run_is_404(self):
        r = self.e.req("DELETE", "/v1/devices/bathroom_towels/run", who="dasha")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "no_run"))

    # ── extend ──
    def test_extend_moves_the_end_and_is_armed_before_confirmed(self):
        j = self.start(minutes=60).json()
        r = self.e.req("POST", "/v1/devices/bathroom_towels/run/extend", who="dasha", json={"minutes": 30})
        self.assertEqual(r.status_code, 200, r.text)
        x = r.json()
        self.assertEqual((x["outcome"], x["action"]), ("confirmed", "extend"))
        self.assertEqual(x["run"]["id"], j["run"]["id"])
        self.assertGreater(x["run"]["ends_at"], j["run"]["ends_at"])
        self.assertAlmostEqual(x["run"]["minutes_total"], 90, delta=1)

    def test_extension_beyond_the_guardrail_is_rejected_by_the_service(self):
        self.start(minutes=100)
        r = self.e.req("POST", "/v1/devices/bathroom_towels/run/extend", who="dasha", json={"minutes": 30})      # 100 + 30 > 120 from the real start
        self.assertEqual((r.status_code, r.json()["error"]["details"]["problems"][0]["code"]), (422, "max_on"))
        d = self.e.req("GET", "/v1/devices/bathroom_towels", who="ivan").json()
        self.assertAlmostEqual(d["run"]["minutes_total"], 100, delta=1)

    def test_extend_without_a_run_and_too_late(self):
        self.assertEqual(self.e.req("POST", "/v1/devices/bathroom_towels/run/extend", who="dasha", json={"minutes": 10}).status_code, 404)
        self.start(minutes=5)
        self.tick(5 * 60 - 20)                                                          # 20 s left: too late to arm a new end
        r = self.e.req("POST", "/v1/devices/bathroom_towels/run/extend", who="dasha", json={"minutes": 10})
        self.assertEqual((r.status_code, r.json()["error"]["details"]["problems"][0]["code"]), (422, "too_late"))

    # ── roles, validation, idempotency, modes ──
    def test_roles_and_validation(self):
        self.assertEqual(self.start(who="ola").status_code, 403)
        self.assertEqual(self.e.req("DELETE", "/v1/devices/bathroom_towels/run", who="ola").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/devices/bathroom_towels/run/extend", who="ola", json={"minutes": 5}).status_code, 403)
        self.assertEqual(self.start(who=None, token="").status_code, 401)
        self.assertEqual(self.start(did="nothing").status_code, 404)
        for bad in ({}, {"minutes": "30"}, {"minutes": True}, {"minutes": 30, "x": 1}, {"minutes": 1.5}):
            self.assertEqual(self.e.req("POST", "/v1/devices/bathroom_towels/run", who="dasha", json=bad).status_code, 400, bad)
        self.e.db.x("UPDATE device_meta SET household_switchable=0 WHERE device_id='bathroom_towels'")
        self.e.core.devices["bathroom_towels"]["switchable"] = False
        try:
            self.assertEqual(self.start().status_code, 403)
            self.assertEqual(self.start(who="ivan").status_code, 200)                    # the admin always may
        finally:
            self.e.db.x("UPDATE device_meta SET household_switchable=1 WHERE device_id='bathroom_towels'")
            self.e.core.devices["bathroom_towels"]["switchable"] = True

    def test_the_same_idempotency_key_does_not_start_two_runs(self):
        h = {"Idempotency-Key": "run-key-0001"}
        a = self.e.req("POST", "/v1/devices/bathroom_towels/run", who="dasha", json={"minutes": 30}, headers=h)
        b = self.e.req("POST", "/v1/devices/bathroom_towels/run", who="dasha", json={"minutes": 30}, headers=h)
        self.assertEqual((a.status_code, b.status_code, b.json().get("replayed")), (200, 200, True))
        self.assertEqual(len(self.service_runs()), 1)
        self.assertEqual(len([c for c in self.e.ha.calls if c[1] == "turn_on"]), 1)

    def test_a_plain_device_can_run_too_and_modes_are_admin_editable(self):
        r = self.start(did="plant_orchids", minutes=30)                                  # lamps may use a run as well (optional)
        self.assertEqual(r.status_code, 422)                                             # its window 07:00-20:00 covers 12:00: runs are for the gaps
        self.assertEqual(r.json()["error"]["details"]["problems"][0]["code"], "window_overlap")
        self.assertEqual(self.e.req("PATCH", "/v1/devices/bathroom_towels", who="dasha", json={"switch_mode": "plain"}).status_code, 403)
        self.assertEqual(self.e.req("PATCH", "/v1/devices/bathroom_towels", who="ivan", json={"switch_mode": "sometimes"}).status_code, 422)
        self.assertEqual(self.e.req("PATCH", "/v1/devices/bathroom_towels", who="ivan", json={}).status_code, 400)
        self.assertEqual(self.e.req("PATCH", "/v1/devices/nothing", who="ivan", json={"switch_mode": "plain"}).status_code, 404)
        c = self.e.req("PATCH", "/v1/devices/bathroom_towels", who="ivan", json={"switch_mode": "plain"})
        self.assertEqual((c.status_code, c.json()["switch_mode"]), (200, "plain"))
        self.assertEqual(self.e.req("POST", "/v1/devices/bathroom_towels/switch", who="dasha", json={"on": True}).json()["outcome"], "confirmed")   # a plain ON again
        self.assertEqual(self.e.db.audit_rows(3)[1]["action"], "device_patch")
        c = self.e.req("PATCH", "/v1/devices/bathroom_towels", who="ivan", json={"switch_mode": "timed"})
        self.assertEqual(c.json()["switch_mode"], "timed")

    def test_no_secret_in_the_audit_of_a_run(self):
        self.start()
        self.e.assert_no_secrets()


if __name__ == "__main__":
    unittest.main()
