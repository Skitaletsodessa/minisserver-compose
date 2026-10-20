"""Timed runs (Task 29 Part 1): model rules, the store, the API (contract-checked), the rendered Home Assistant config."""
import datetime as dt
import json
import os
import unittest
import yaml

from sched import model, render
from tests.helpers import Env, SYSTEM

TZ = model.TZ


def at(h, m=0, s=0, day=19):
    return dt.datetime(2026, 10, day, h, m, s, tzinfo=TZ)


DRYER = {"id": "bathroom_towels", "kind": "plug", "managed": True,
         "guardrails": {"max_on_minutes": 120, "allowed_from": "06:00", "allowed_to": "21:00", "min_gap_minutes": 30}}
WINDOWS = [{"id": "morning", "days": [1, 2, 3, 4, 5, 6, 7], "start": "06:30", "end": "08:00"},
           {"id": "evening", "days": [1, 2, 3, 4, 5, 6, 7], "start": "18:30", "end": "20:00"}]


def codes(probs):
    return sorted(p["code"] for p in probs)


class RunModel(unittest.TestCase):
    def v(self, minutes, start, prev=None, ent=DRYER, rules=WINDOWS, skips=()):
        return model.validate_run(ent, rules, set(skips), minutes, start, prev)

    def test_a_legal_run_ends_on_a_whole_minute(self):
        ends, probs = self.v(30, at(12, 0, 20))
        self.assertEqual(probs, [])
        self.assertEqual(ends, at(12, 31, 0))                      # 12:00:20 + 30 min = 12:30:20 -> the next whole minute
        ends, _ = self.v(30, at(12, 0, 0))
        self.assertEqual(ends, at(12, 30, 0))

    def test_max_on_is_counted_from_the_real_start_and_rejected_not_clamped(self):
        self.assertEqual(self.v(120, at(12, 0))[1], [])
        ends, probs = self.v(121, at(12, 0))
        self.assertIsNone(ends)
        self.assertEqual(codes(probs), ["max_on"])
        _e, probs = self.v(120, at(12, 0, 5))                       # 120 min + the seconds rounded up to the minute = 121 min
        self.assertEqual(codes(probs), ["max_on"])

    def test_allowed_range(self):
        self.assertEqual(codes(self.v(30, at(5, 40))[1]), ["allowed_range"])                 # before 06:00
        self.assertEqual(codes(self.v(60, at(20, 30))[1]), ["allowed_range"])                 # would end 21:30, after 21:00
        ends, probs = self.v(30, at(20, 30))
        self.assertEqual(probs, [])
        self.assertEqual(ends, at(21, 0))
        _e, probs = self.v(31, at(20, 30))
        self.assertEqual(codes(probs), ["allowed_range"])

    def test_a_run_never_crosses_midnight_or_the_dst_hours(self):
        ent = {**DRYER, "guardrails": {"max_on_minutes": 600, "allowed_from": "00:00", "allowed_to": "23:59"}}
        self.assertIn("allowed_range", codes(self.v(120, at(23, 0), ent=ent)[1]))
        self.assertIn("dst_ambiguous", codes(self.v(60, at(1, 30), ent=ent)[1]))

    def test_min_gap_since_the_previous_run(self):
        self.assertEqual(codes(self.v(30, at(12, 0), prev=at(11, 45))[1]), ["min_gap"])
        self.assertEqual(self.v(30, at(12, 0), prev=at(11, 30))[1], [])

    def test_overlap_with_a_scheduled_window_is_rejected_unless_that_date_is_skipped(self):
        self.assertEqual(codes(self.v(60, at(17, 45))[1]), ["window_overlap"])
        self.assertEqual(codes(self.v(30, at(18, 0))[1]), [])                 # ends 18:30 exactly: touching is fine
        self.assertEqual(self.v(60, at(17, 45), skips=["2026-10-19"])[1], [])

    def test_not_managed_and_bad_minutes_and_kind(self):
        self.assertEqual(codes(self.v(30, at(12), ent={**DRYER, "managed": False})[1]), ["not_managed"])
        for bad in (0, -5, True, "30", 1.5, None):
            self.assertEqual(codes(self.v(bad, at(12))[1]), ["range"], repr(bad))
        self.assertEqual(codes(model.validate_run({**DRYER, "kind": "vacuum_plan"}, [], set(), 10, at(12))[1]), ["kind"])

    def test_extension_counts_from_the_real_start(self):
        start = at(12, 0, 0)
        old_end = at(12, 30)
        ends, probs = model.validate_run(DRYER, WINDOWS, set(), 60, old_end, None, real_start=start)
        self.assertEqual((probs, ends), ([], at(13, 30)))
        _e, probs = model.validate_run(DRYER, WINDOWS, set(), 91, old_end, None, real_start=start)      # 30 + 91 = 121 min in total
        self.assertEqual(codes(probs), ["max_on"])


class RunApi(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        for eid in ("bathroom_towels", "parrot_uv", "plant_rack1"):
            self.assertEqual(self.e.req("PATCH", "/v1/entities/" + eid, json={"managed": True}).status_code, 200)
        self.status_path = self.e.app.state.core.apply_status_path

    def apply(self, seq, result="applied", loaded=True):
        with open(self.status_path, "w") as f:
            json.dump({"seq": seq, "result": result, "loaded_ok": loaded, "set_hash": "x", "at": "now"}, f)

    def skip_today(self, eid):
        self.e.store.add_exception(SYSTEM, eid, {"kind": "skip_date", "date": "2026-10-19"}, today=dt.date(2026, 10, 19))

    def start(self, eid="bathroom_towels", minutes=30, role="household", **kw):
        return self.e.req("POST", "/v1/runs", role=role, json={"entity_id": eid, "minutes": minutes}, **kw)

    def test_start_returns_the_run_and_is_audited_with_the_actor(self):
        r = self.start()
        self.assertEqual(r.status_code, 201, r.text)
        run = r.json()
        self.assertEqual((run["entity"], run["status"], run["armed"], run["started_by"]), ("bathroom_towels", "active", False, "t-household"))
        self.assertTrue(run["ends_at"].endswith(":00+02:00"))
        a = self.e.req("GET", "/v1/audit?entity=bathroom_towels&limit=3").json()["audit"]
        self.assertEqual((a[0]["action"], a[0]["after"]["id"]), ("run_start", run["id"]))

    def test_acting_person_of_a_gateway_token_is_recorded(self):
        self.skip_today("parrot_uv")
        _i, secret = self.e.store.create_token(SYSTEM, "gw-test", "household", gateway=True)
        r = self.e.req("POST", "/v1/runs", token=secret, headers={"X-Acting-User": "person=dasha;phone=p-1"}, json={"entity_id": "parrot_uv", "minutes": 30})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["started_by"], "dasha")

    def test_roles(self):
        self.assertEqual(self.start(role="readonly").status_code, 403)
        self.assertEqual(self.e.req("GET", "/v1/runs", role="readonly").status_code, 200)
        self.assertEqual(self.start(token="").status_code, 401)
        self.assertEqual(self.start(role="admin").status_code, 201)

    def test_only_plugs_and_known_entities(self):
        self.assertEqual(self.start("vacuum_bedroom").status_code, 403)
        self.assertEqual(self.start("nothing").status_code, 404)
        self.assertEqual(self.e.req("POST", "/v1/runs", json={"entity_id": "parrot_uv"}).status_code, 422)
        self.assertEqual(self.e.req("POST", "/v1/runs", json={"entity_id": "parrot_uv", "minutes": 10, "x": 1}).status_code, 400)

    def test_guardrails_reject_with_reasons_and_change_nothing(self):
        r = self.start(minutes=121)
        self.assertEqual(r.status_code, 422)
        self.assertEqual([p["code"] for p in r.json()["error"]["problems"]], ["max_on"])
        self.assertEqual(self.e.req("GET", "/v1/runs?active=1").json()["runs"], [])

    def test_unmanaged_plug_is_refused(self):
        r = self.start("plant_rack2")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "not_managed")

    def test_second_run_is_a_conflict_and_a_new_one_respects_the_gap(self):
        first = self.start().json()
        r = self.start()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "run_active"))
        self.assertEqual(self.e.req("DELETE", "/v1/runs/" + first["id"]).status_code, 200)
        r = self.start()                                                    # the cancelled run ended seconds ago: the 30 min gap applies
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "min_gap")

    def test_a_run_past_its_end_is_ended_for_everybody_and_does_not_block_a_new_one(self):
        first = self.start(minutes=10).json()
        end = dt.datetime.fromisoformat(first["ends_at"])
        self.e.store.clock = lambda: (end + dt.timedelta(minutes=31)).isoformat(timespec="seconds")      # past the end and the 30 min gap
        v = self.e.req("GET", "/v1/runs/" + first["id"]).json()
        self.assertEqual((v["status"], v["remaining_s"], v["ended_at"]), ("ended", 0, first["ends_at"]))
        self.assertEqual(self.e.req("DELETE", "/v1/runs/" + first["id"]).status_code, 409)
        second = self.start(minutes=10)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertIn("run_expire", [a["action"] for a in self.e.req("GET", "/v1/audit?entity=bathroom_towels&limit=6").json()["audit"]])

    def test_armed_follows_the_applier(self):
        run = self.start().json()
        self.assertFalse(self.e.req("GET", "/v1/runs/" + run["id"]).json()["armed"])
        self.apply(0)                                                       # an older render than the run
        self.assertFalse(self.e.req("GET", "/v1/runs/" + run["id"]).json()["armed"])
        seq = self.e.store.run(run["id"])["rev_seq"]
        self.apply(seq, loaded=False)
        self.assertFalse(self.e.req("GET", "/v1/runs/" + run["id"]).json()["armed"])
        self.apply(seq, result="failed")
        self.assertFalse(self.e.req("GET", "/v1/runs/" + run["id"]).json()["armed"])
        self.apply(seq)
        self.assertTrue(self.e.req("GET", "/v1/runs/" + run["id"]).json()["armed"])

    def test_extend_adds_minutes_within_the_guardrail_and_needs_time_left(self):
        run = self.start(minutes=60).json()
        ok = self.e.req("POST", "/v1/runs/%s/extend" % run["id"], role="household", json={"minutes": 30})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertGreaterEqual(ok.json()["minutes_total"], 90)
        self.assertGreater(ok.json()["ends_at"], run["ends_at"])
        too_much = self.e.req("POST", "/v1/runs/%s/extend" % run["id"], json={"minutes": 40})
        self.assertEqual(too_much.status_code, 422)
        self.assertEqual(too_much.json()["error"]["problems"][0]["code"], "max_on")
        self.assertEqual(self.e.req("POST", "/v1/runs/%s/extend" % run["id"], role="readonly", json={"minutes": 5}).status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/runs/rnone/extend", json={"minutes": 5}).status_code, 404)

    def test_extend_too_late_is_rejected(self):
        run = self.start(minutes=1).json()                                  # ends within ~2 min of the fake clock: less than 90 s left soon
        later = dt.datetime.fromisoformat(run["ends_at"]) - dt.timedelta(seconds=30)
        self.e.store.clock = lambda: later.isoformat(timespec="seconds")
        r = self.e.req("POST", "/v1/runs/%s/extend" % run["id"], json={"minutes": 5})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "too_late")

    def test_cancel_and_double_cancel(self):
        run = self.start().json()
        r = self.e.req("DELETE", "/v1/runs/" + run["id"])
        self.assertEqual((r.status_code, r.json()["status"]), (200, "cancelled"))
        r = self.e.req("DELETE", "/v1/runs/" + run["id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "run_not_active"))
        self.assertEqual(self.e.req("DELETE", "/v1/runs/" + run["id"], role="readonly").status_code, 403)

    def test_expiry_drops_the_run_from_the_render_after_the_grace(self):
        run = self.start().json()
        files = self.e.store.snapshot()
        self.assertEqual([r["id"] for x in files["entities"] if x["id"] == "bathroom_towels" for r in x["runs"]], [run["id"]])
        end = dt.datetime.fromisoformat(run["ends_at"])
        self.e.store.clock = lambda: (end + dt.timedelta(seconds=model.RUN_GRACE_S - 5)).isoformat(timespec="seconds")
        self.assertEqual(self.e.store.expire_runs(), 0)
        self.e.store.clock = lambda: (end + dt.timedelta(seconds=model.RUN_GRACE_S + 5)).isoformat(timespec="seconds")
        self.assertEqual(self.e.store.expire_runs(), 1)
        self.assertEqual(self.e.req("GET", "/v1/runs/" + run["id"]).json()["status"], "ended")
        snap = self.e.store.snapshot()
        ent = [x for x in snap["entities"] if x["id"] == "bathroom_towels"][0]
        self.assertEqual((ent["runs"], [r["id"] for r in ent["recent_runs"]]), ([], [run["id"]]))
        self.assertIn("run_expire", [a["action"] for a in self.e.req("GET", "/v1/audit?entity=bathroom_towels&limit=5").json()["audit"]])

    def test_runs_list_and_contract(self):
        a = self.start().json()
        self.skip_today("parrot_uv")
        self.assertEqual(self.start("parrot_uv", 20).status_code, 201)
        allr = self.e.req("GET", "/v1/runs").json()["runs"]
        self.assertEqual(len(allr), 2)
        self.assertEqual([r["id"] for r in self.e.req("GET", "/v1/runs?entity=bathroom_towels").json()["runs"]], [a["id"]])
        self.assertEqual(len(self.e.req("GET", "/v1/runs?active=1").json()["runs"]), 2)

    def test_audit_chain_stays_intact(self):
        run = self.start().json()
        self.e.req("POST", "/v1/runs/%s/extend" % run["id"], json={"minutes": 10})
        self.e.req("DELETE", "/v1/runs/" + run["id"])
        ok, rows, bad = self.e.store.verify_audit()
        self.assertTrue(ok, bad)


class RunRender(unittest.TestCase):
    def setUp(self):
        self.e = Env(check_contract=False)
        self.e.req("PATCH", "/v1/entities/bathroom_towels", json={"managed": True})

    def files(self):
        return render.render(self.e.store.snapshot())["files"]

    def mkrun(self):
        r = self.e.req("POST", "/v1/runs", role="household", json={"entity_id": "bathroom_towels", "minutes": 30})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_nothing_changes_in_the_bytes_without_a_run(self):
        before = self.files()
        run = self.mkrun()
        during = self.files()
        self.e.req("DELETE", "/v1/runs/" + run["id"])
        after = self.files()
        self.assertEqual(before, after)                                  # cancelled: back to the exact previous bytes
        self.assertNotEqual(before["automations/sch_bathroom_towels.yaml"], during["automations/sch_bathroom_towels.yaml"])
        self.assertNotEqual(before["scripts/sch_reconcile_bathroom_towels.yaml"], during["scripts/sch_reconcile_bathroom_towels.yaml"])
        self.assertEqual({k: v for k, v in before.items() if "bathroom_towels" not in k and "fingerprint" not in k},
                         {k: v for k, v in during.items() if "bathroom_towels" not in k and "fingerprint" not in k})

    def test_the_end_of_the_run_is_generated_into_ha(self):
        run = self.mkrun()
        doc = yaml.safe_load(self.files()["automations/sch_bathroom_towels.yaml"])
        by_id = {a["id"]: a for a in doc}
        a = by_id["sch_bathroom_towels_runend_" + run["id"]]
        end = int(dt.datetime.fromisoformat(run["ends_at"]).timestamp())
        self.assertEqual(a["trigger"], [{"platform": "time_pattern", "minutes": "/1"}])
        self.assertEqual(a["condition"][0]["value_template"], "{{ %d <= now().timestamp() < %d }}" % (end, end + model.RUN_GRACE_S))
        self.assertEqual(a["condition"][1], {"condition": "state", "entity_id": "switch.bathroom_towels", "state": "on"})
        self.assertIn("last_changed.timestamp() <= %d" % end, a["condition"][2]["value_template"])
        self.assertEqual(a["action"][0], {"service": "switch.turn_off", "target": {"entity_id": "switch.bathroom_towels"}})
        self.assertRegex(a["alias"], r"\[sch [0-9a-f]{8}\]$")
        self.assertEqual(sum(1 for x in by_id if "_failsafe" in x), 1)    # the fail-safe OFF is still there, untouched

    def test_window_off_edges_do_not_cut_a_run_and_the_failsafe_is_unchanged(self):
        before = {a["id"]: a for a in yaml.safe_load(self.files()["automations/sch_bathroom_towels.yaml"])}
        run = self.mkrun()
        during = {a["id"]: a for a in yaml.safe_load(self.files()["automations/sch_bathroom_towels.yaml"])}
        off = during["sch_bathroom_towels_morning_off"]["condition"]
        self.assertIn("not (", off[-1]["value_template"])
        self.assertEqual(before["sch_bathroom_towels_morning_on"]["condition"], during["sch_bathroom_towels_morning_on"]["condition"])   # ON edges untouched
        fs_b, fs_d = before["sch_bathroom_towels_failsafe"], during["sch_bathroom_towels_failsafe"]
        self.assertEqual({k: v for k, v in fs_b.items() if k != "alias"}, {k: v for k, v in fs_d.items() if k != "alias"})
        self.assertIn("> 7200", fs_d["condition"][1]["value_template"])

    def test_reconcile_never_turns_a_plug_on_because_of_a_run(self):
        run = self.mkrun()
        script = yaml.safe_load(self.files()["scripts/sch_reconcile_bathroom_towels.yaml"])["sch_reconcile_bathroom_towels"]
        seq = script["sequence"]
        self.assertIn("in_run", seq[0]["variables"])
        on_cond = seq[1]["if"][0]["value_template"]
        self.assertIn("target == 'on'", on_cond)
        self.assertNotIn("in_run", on_cond)                                # the ON branch looks at the windows only
        self.assertNotIn("in_run", seq[0]["variables"]["target"])
        self.assertNotIn("%d" % int(dt.datetime.fromisoformat(run["ends_at"]).timestamp()), seq[0]["variables"]["target"])
        self.assertIn("not in_run", seq[2]["if"][0]["value_template"])      # during the run: no OFF; after the end: the normal window logic

    def test_render_is_deterministic_with_a_run(self):
        self.mkrun()
        self.assertEqual(render.render(self.e.store.snapshot()), render.render(self.e.store.snapshot()))

    def test_ha_alerts_gets_the_runs_in_the_snapshot_file(self):
        run = self.mkrun()
        snap = json.load(open(os.path.join(self.e.store.out, "schedules.json")))
        ent = [x for x in snap["entities"] if x["id"] == "bathroom_towels"][0]
        self.assertEqual([r["id"] for r in ent["runs"]], [run["id"]])
        self.assertEqual(ent["runs"][0]["end_ts"], int(dt.datetime.fromisoformat(run["ends_at"]).timestamp()))


if __name__ == "__main__":
    unittest.main()
