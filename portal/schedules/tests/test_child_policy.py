"""Task 29 Part 2, the service side: the children's windows under guardrails, time-boxed exceptions, per-child domain lists, the invariants, the snapshot
the host job executes from, the import/migration and the cutover guard. Real service, contract-checked responses, a frozen clock (Mon 2026-10-19 12:00)."""
import copy
import datetime as dt
import json
import os
import unittest

from sched import model, child_invariants
from sched.child_invariants import REQUIRED_ALWAYS
from tests.helpers import Env, SYSTEM

MON_SAT = [1, 2, 3, 4, 5, 6]
RULES = [{"id": "yt_roblox", "days": MON_SAT, "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}},
         {"id": "maps", "days": MON_SAT, "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}}]
PAYLOAD = {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "20:36:d0:14:8d:5a", "rules": RULES},
                       {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": RULES}],
           "lists": {"allow_always": sorted(REQUIRED_ALWAYS), "allow_weekdays": ["maps.example.com"], "allow_extra": [], "deny_extra": []},
           "always_services": ["telegram"]}
TZ = model.TZ


def snap_file(env):
    return json.load(open(os.path.join(env.store.out, "child-policy.json")))


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.assertEqual(self.e.req("PUT", "/v1/child-policy/import", json=PAYLOAD).status_code, 200)

    def req(self, *a, **k):
        return self.e.req(*a, **k)

    def now(self):
        return dt.datetime.fromisoformat(self.e.store.clock())

    def set_clock(self, h, m=0, day=19):
        t = [dt.datetime(2026, 10, day, h, m, 0, tzinfo=TZ)]

        def clock():
            t[0] += dt.timedelta(seconds=1)
            return t[0].isoformat(timespec="seconds")
        self.e.store.clock = clock


class ImportAndSnapshot(Base):
    def test_import_gives_proposed_guardrails_flags_and_per_child_lists(self):
        d = self.req("GET", "/v1/child/child_a").json()
        self.assertEqual(d["guardrails"], {"allowed_from": "08:00", "allowed_to": "21:00", "max_open_minutes_per_day": 240, "max_exception_minutes": 360,
                                           "exception_latest_end": "22:00", "all_latest_end": "21:00"})
        self.assertEqual(d["flags"], {"household_editable": True, "guardrails_proposed": True})
        self.assertEqual((d["ip"], d["domains"], d["exceptions"]), ("192.168.31.60", {"allow": [], "deny": []}, []))
        self.assertEqual([x["id"] for x in self.req("GET", "/v1/child").json()["children"]], ["child_a", "child_b"])

    def test_the_snapshot_file_is_published_atomically_with_a_checksum_and_passes_the_invariants(self):
        s = snap_file(self.e)
        self.assertEqual((s["schema"], len(s["devices"]), s["always_services"]), (1, 2, ["telegram"]))
        self.assertEqual(len(s["checksum"]), 64)
        self.assertEqual(child_invariants.verify_snapshot(s), [])
        self.assertEqual(self.req("GET", "/v1/child/snapshot").json()["checksum"], s["checksum"])
        self.assertEqual(self.req("GET", "/v1/health").json().get("status"), "ok")
        h = self.req("GET", "/v1/health", role="admin").json()
        self.assertEqual((h["child_policy"]["checksum"], h["child_policy"]["devices"], h["child_policy"]["cutover"]), (s["checksum"], 2, None))

    def test_the_checksum_covers_the_policy_not_the_moment(self):
        before = snap_file(self.e)["checksum"]
        self.e.store.create_token(SYSTEM, "x-unrelated", "readonly")             # an unrelated audit row
        self.e.store.publish()
        self.assertEqual(snap_file(self.e)["checksum"], before)
        g = self.req("GET", "/v1/schedules/child_a")
        self.req("PUT", "/v1/schedules/child_a", headers={"If-Match": g.headers["ETag"]}, json={"rules": RULES[:1]})
        self.assertNotEqual(snap_file(self.e)["checksum"], before)

    def test_import_dry_run_reports_and_stores_nothing(self):
        p = copy.deepcopy(PAYLOAD)
        p["devices"] = p["devices"][:1]
        r = self.req("PUT", "/v1/child-policy/import?dry_run=1", json=p)
        self.assertEqual((r.status_code, r.json()["dry_run"], r.json()["devices"]), (200, True, ["child_a"]))
        self.assertEqual(len(self.req("GET", "/v1/child").json()["children"]), 2)

    def test_import_invariants_the_required_always_on_hosts_and_telegram_must_stay(self):
        p = copy.deepcopy(PAYLOAD)
        p["lists"]["allow_always"] = [h for h in p["lists"]["allow_always"] if h != "www.google.com"]
        r = self.req("PUT", "/v1/child-policy/import", json=p)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "invariant")
        p = copy.deepcopy(PAYLOAD)
        p["always_services"] = []
        self.assertEqual(self.req("PUT", "/v1/child-policy/import", json=p).status_code, 422)
        p = copy.deepcopy(PAYLOAD)
        p["devices"][0]["deny_extra"] = ["google.com"]
        self.assertEqual(self.req("PUT", "/v1/child-policy/import", json=p).status_code, 422)

    def test_migration_of_the_old_extra_lists_and_only_the_still_active_exceptions(self):
        p = copy.deepcopy(PAYLOAD)
        p["lists"]["allow_extra"], p["lists"]["deny_extra"] = ["ok.example.org"], ["bad.example.org"]
        now = self.now()
        p["exceptions"] = [{"client": "192.168.31.60", "target": "all", "created": "2026-10-17T13:55:04+02:00", "expires": "2026-10-17T23:59:59+02:00"},      # dead
                           {"client": "192.168.31.61", "target": "youtube", "expires": (now + dt.timedelta(minutes=45)).isoformat(timespec="seconds")}]
        r = self.req("PUT", "/v1/child-policy/import", json=p)
        self.assertEqual((r.status_code, r.json()["exceptions_migrated"]), (200, 1))
        a, b = self.req("GET", "/v1/child/child_a").json(), self.req("GET", "/v1/child/child_b").json()
        self.assertEqual((a["domains"], a["exceptions"]), ({"allow": ["ok.example.org"], "deny": ["bad.example.org"]}, []))
        self.assertEqual([x["target"] for x in b["exceptions"]], ["youtube"])
        self.assertEqual(self.req("GET", "/v1/child-policy").json()["lists"]["deny_extra"], [])
        bad = copy.deepcopy(p)
        bad["exceptions"] = [{"client": "10.9.9.9", "target": "youtube", "expires": (now + dt.timedelta(minutes=5)).isoformat()}]
        self.assertEqual(self.req("PUT", "/v1/child-policy/import", json=bad).status_code, 422)

    def test_the_cutover_switch_blocks_a_reimport_and_can_be_rolled_back(self):
        self.assertEqual(self.req("PUT", "/v1/child/cutover", role="household", json={"done": True}).status_code, 403)
        r = self.req("PUT", "/v1/child/cutover", json={"done": True, "note": "test"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["cutover"]["note"], "test")
        self.assertEqual(self.req("PUT", "/v1/child-policy/import", json=PAYLOAD).status_code, 403)
        self.assertEqual(self.req("PUT", "/v1/child-policy/import?dry_run=1", json=PAYLOAD).status_code, 200)
        self.assertEqual(self.req("PUT", "/v1/child/cutover", json={"done": False}).json()["cutover"], None)
        self.assertEqual(self.req("PUT", "/v1/child-policy/import", json=PAYLOAD).status_code, 200)
        self.assertEqual(self.req("PUT", "/v1/child/cutover", json={"done": "yes"}).status_code, 400)


class Windows(Base):
    def put(self, rules, role="household", cid="child_a"):
        g = self.req("GET", "/v1/schedules/" + cid)
        return self.req("PUT", "/v1/schedules/" + cid, role=role, headers={"If-Match": g.headers["ETag"]}, json={"rules": rules})

    def yt(self, start, end, days=MON_SAT, opens="youtube,roblox", rid="yt_roblox"):
        return [{"id": rid, "days": days, "start": start, "end": end, "variables": {"opens": opens}}, RULES[1]]

    def test_a_parent_may_edit_within_the_guardrails(self):
        r = self.put(self.yt("15:00", "18:00"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.put(self.yt("10:00", "12:00", days=[7], rid="sunday")).status_code, 200)      # a Sunday window is allowed (a parental decision)

    def test_guardrails_reject_with_reasons_never_clamp(self):
        r = self.put(self.yt("07:00", "09:00"))
        self.assertEqual((r.status_code, r.json()["error"]["problems"][0]["code"]), (422, "allowed_range"))
        r = self.put(self.yt("16:00", "22:00"))
        self.assertIn("allowed_range", [p["code"] for p in r.json()["error"]["problems"]])
        r = self.put(self.yt("14:00", "19:30"))                                    # 330 min > 240
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "max_open")
        two = self.yt("09:00", "12:00") + [{"id": "evening", "days": MON_SAT, "start": "15:00", "end": "17:00", "variables": {"opens": "roblox"}}]
        two = [two[0], two[2], RULES[1]]
        self.assertEqual(self.put(two).json()["error"]["problems"][0]["code"], "max_open")      # 180 + 120 > 240 on the same day
        self.assertEqual([r["start"] for r in self.req("GET", "/v1/schedules/child_a").json()["rules"] if r["id"] == "yt_roblox"], ["16:00"])   # nothing changed

    def test_maps_windows_are_not_bounded_by_the_open_window_guardrails(self):
        self.assertEqual(self.put([RULES[0], {**RULES[1], "days": [1, 2, 3, 4, 5, 6, 7]}]).status_code, 200)

    def test_roles(self):
        self.assertEqual(self.put(self.yt("15:00", "18:00"), role="readonly").status_code, 403)
        self.assertEqual(self.put(self.yt("15:00", "18:00"), role="admin").status_code, 200)
        self.assertEqual(self.req("PATCH", "/v1/entities/child_a", json={"flags": {"household_editable": False}}).status_code, 200)
        self.assertEqual(self.put(self.yt("15:30", "18:00")).status_code, 403)                  # the flag is admin data
        self.assertEqual(self.put(self.yt("15:30", "18:00"), role="admin").status_code, 200)

    def test_only_the_admin_changes_the_guardrails_and_the_windows_are_rechecked(self):
        self.assertEqual(self.req("PUT", "/v1/entities/child_a/guardrails", role="household", json={"max_open_minutes_per_day": 600}).status_code, 403)
        g = {"allowed_from": "08:00", "allowed_to": "21:00", "max_open_minutes_per_day": 120, "max_exception_minutes": 360, "exception_latest_end": "22:00", "all_latest_end": "21:00"}
        r = self.req("PUT", "/v1/entities/child_a/guardrails", json=g)
        self.assertEqual(r.status_code, 422)                                                        # today's 180 min window would violate 120
        self.assertEqual(self.req("PUT", "/v1/entities/child_a/guardrails", json={**g, "max_open_minutes_per_day": 300}).status_code, 200)
        self.assertEqual(self.put(self.yt("14:00", "19:00")).status_code, 200)                      # 300 now allowed

    def test_preview_shows_what_is_open_and_every_problem(self):
        r = self.req("POST", "/v1/schedules/child_a/preview", json={"rules": self.yt("15:00", "18:00")})
        j = r.json()
        self.assertTrue(j["valid"])
        self.assertTrue(j["open_next_7_days"])
        self.assertTrue(any(x["opens"] == ["roblox", "youtube"] and x["from"][11:16] == "15:00" for x in j["open_next_7_days"]))
        bad = self.req("POST", "/v1/schedules/child_a/preview", json={"rules": self.yt("07:00", "22:00")}).json()
        self.assertFalse(bad["valid"])
        self.assertGreaterEqual(len(bad["problems"]), 2)                                            # every problem, not just the first

    def test_undo_reverts_the_last_window_change_as_a_new_change(self):
        self.put(self.yt("15:00", "18:00"))
        r = self.req("POST", "/v1/schedules/child_a/undo", role="household")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["start"] for x in r.json()["rules"] if x["id"] == "yt_roblox"], ["16:00"])


class Exceptions(Base):
    def setUp(self):
        super().setUp()
        self.set_clock(12, 0)

    def grant(self, target, **body):
        return self.req("POST", "/v1/child/child_a/exceptions", role="household", json={"target": target, **body})

    def test_grant_list_replace_and_cancel(self):
        r = self.grant("youtube", minutes=30)
        self.assertEqual(r.status_code, 201, r.text)
        x = r.json()
        self.assertEqual((x["target"], x["created_by"] != "", x["remaining_s"] > 1700), ("youtube", True, True))
        self.assertEqual(self.req("GET", "/v1/child/child_a").json()["exceptions"][0]["id"], x["id"])
        y = self.grant("youtube", minutes=60).json()                                    # replaces
        self.assertEqual([e["id"] for e in self.req("GET", "/v1/child/child_a").json()["exceptions"]], [y["id"]])
        z = self.grant("maps", minutes=30).json()
        self.assertEqual(len(self.req("GET", "/v1/child/child_a").json()["exceptions"]), 2)
        self.assertEqual(self.req("DELETE", "/v1/child/child_a/exceptions/%s" % y["id"], role="household").status_code, 204)
        self.assertEqual([e["id"] for e in self.req("GET", "/v1/child/child_a").json()["exceptions"]], [z["id"]])
        self.assertEqual(self.req("DELETE", "/v1/child/child_a/exceptions/%s" % y["id"], role="household").status_code, 404)

    def test_guardrails_on_exceptions_length_latest_end_and_all(self):
        r = self.grant("youtube", minutes=400)
        self.assertEqual(r.status_code, 422)
        self.assertEqual([p["code"] for p in r.json()["error"]["problems"]], ["max_exception"])
        self.set_clock(20, 0)
        r = self.grant("youtube", minutes=130)                                           # ends 22:10 > 22:00
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "latest_end")
        self.assertEqual(self.grant("youtube", minutes=100).status_code, 201)            # 21:40 fine
        r = self.grant("all", minutes=70)                                                # 'all' must end by 21:00
        self.assertEqual(r.json()["error"]["problems"][0]["code"], "latest_end")
        self.assertEqual(self.grant("all", minutes=50).status_code, 201)

    def test_until_end_of_day_means_the_latest_end_of_that_target(self):
        r = self.grant("youtube", until_end_of_day=True).json()
        self.assertTrue(r["expires_at"].startswith("2026-10-19T22:00:00"))
        r = self.grant("all", until_end_of_day=True).json()
        self.assertTrue(r["expires_at"].startswith("2026-10-19T21:00:00"))
        self.set_clock(21, 30)
        r = self.grant("all", until_end_of_day=True)
        self.assertEqual((r.status_code, r.json()["error"]["problems"][0]["code"]), (422, "too_late"))

    def test_validation_and_roles(self):
        self.assertEqual(self.grant("tiktok", minutes=30).status_code, 422)
        self.assertEqual(self.grant("youtube").status_code, 422)
        self.assertEqual(self.grant("youtube", minutes=30, until_end_of_day=True).status_code, 422)
        self.assertEqual(self.grant("youtube", minutes=0).status_code, 422)
        self.assertEqual(self.grant("youtube", minutes=True).status_code, 422)
        self.assertEqual(self.req("POST", "/v1/child/child_a/exceptions", role="household", json={"target": "youtube", "minutes": 30, "x": 1}).status_code, 400)
        self.assertEqual(self.req("POST", "/v1/child/child_a/exceptions", role="readonly", json={"target": "youtube", "minutes": 30}).status_code, 403)
        self.assertEqual(self.req("POST", "/v1/child/nothing/exceptions", role="household", json={"target": "youtube", "minutes": 30}).status_code, 404)
        self.assertEqual(self.req("POST", "/v1/child/plant_rack1/exceptions", role="household", json={"target": "youtube", "minutes": 30}).status_code, 404)

    def test_status_follows_windows_and_exceptions(self):
        st = self.req("GET", "/v1/child/child_a").json()["status"]
        self.assertEqual((st["state"], st["open_services"], st["opens_at"][11:16]), ("closed", ["maps"], "16:00"))
        self.set_clock(17, 0)
        st = self.req("GET", "/v1/child/child_a").json()["status"]
        self.assertEqual((st["state"], st["open_until"][11:16]), ("open", "19:00"))
        self.set_clock(12, 0)
        self.grant("youtube", minutes=30)
        st = self.req("GET", "/v1/child/child_a").json()["status"]
        self.assertEqual((st["state"], "youtube" in st["open_services"], st["open_until"][11:16]), ("restricted", True, "12:30"))
        self.grant("all", minutes=30)
        st = self.req("GET", "/v1/child/child_a").json()["status"]
        self.assertEqual(st["state"], "open")

    def test_expiry_by_the_clock_and_the_loop(self):
        x = self.grant("youtube", minutes=30).json()
        self.set_clock(12, 31)
        self.assertEqual(self.req("GET", "/v1/child/child_a").json()["exceptions"], [])
        self.assertEqual(self.e.store.expire_child_exceptions(), 0)                       # still inside the grace
        self.set_clock(12, 40)
        self.assertEqual(self.e.store.expire_child_exceptions(), 1)
        self.assertIn("child_exception_expire", [a["action"] for a in self.req("GET", "/v1/audit?entity=child_a&limit=5").json()["audit"]])
        self.assertEqual(snap_file(self.e)["devices"][0]["exceptions"], [])

    def test_the_snapshot_carries_active_exceptions_with_absolute_expiry(self):
        x = self.grant("youtube", minutes=30).json()
        e = snap_file(self.e)["devices"][0]["exceptions"]
        self.assertEqual([(i["id"], i["target"]) for i in e], [(x["id"], "youtube")])
        self.assertEqual(e[0]["expires_ts"], int(dt.datetime.fromisoformat(x["expires_at"]).timestamp()))
        self.req("DELETE", "/v1/child/child_a/exceptions/%s" % x["id"], role="household")
        self.assertEqual(snap_file(self.e)["devices"][0]["exceptions"], [])

    def test_the_audit_names_the_acting_person(self):
        _i, secret = self.e.store.create_token(SYSTEM, "gw-test", "household", gateway=True)
        r = self.req("POST", "/v1/child/child_a/exceptions", token=secret, headers={"X-Acting-User": "person=dasha;phone=p-1"}, json={"target": "youtube", "minutes": 30})
        self.assertEqual(r.json()["created_by"], "dasha")
        a = self.req("GET", "/v1/audit?entity=child_a&limit=2").json()["audit"][0]
        self.assertEqual((a["action"], a["actor"]["acting"]["person"]), ("child_exception_add", "dasha"))


class Domains(Base):
    def add(self, kind, domain, role="household"):
        return self.req("POST", "/v1/child/child_a/domains", role=role, json={"kind": kind, "domain": domain})

    def test_allow_deny_move_and_snapshot(self):
        self.assertEqual(self.add("allow", "Example.COM.").json(), {"allow": ["example.com"], "deny": []})
        self.assertEqual(self.add("deny", "example.com").json(), {"allow": [], "deny": ["example.com"]})       # adding to one removes it from the other
        self.assertEqual(self.add("allow", "good.example.org").status_code, 200)
        s = snap_file(self.e)["devices"][0]
        self.assertEqual((s["allow_extra"], s["deny_extra"]), (["good.example.org"], ["example.com"]))
        self.assertEqual(snap_file(self.e)["devices"][1]["allow_extra"], [])                                     # per child
        self.assertEqual(self.add("allow", "good.example.org").json()["allow"], ["good.example.org"])            # idempotent

    def test_remove_and_undo_restore_the_lists_exactly(self):
        self.add("allow", "a.example.org")
        self.add("deny", "b.example.org")
        r = self.req("DELETE", "/v1/child/child_a/domains?kind=deny&domain=b.example.org", role="household")
        self.assertEqual(r.json(), {"allow": ["a.example.org"], "deny": []})
        r = self.req("POST", "/v1/child/child_a/domains/undo", role="household")
        self.assertEqual(r.json(), {"allow": ["a.example.org"], "deny": ["b.example.org"]})
        r = self.req("POST", "/v1/child/child_a/domains/undo", role="household")                               # undo of the undo's predecessor: the add of b
        self.assertEqual(r.json(), {"allow": ["a.example.org"], "deny": []})
        self.assertEqual(self.req("DELETE", "/v1/child/child_a/domains?kind=deny&domain=never.example.org", role="household").status_code, 404)

    def test_validation_invariants_and_roles(self):
        for bad in ("not a domain", "x$y.com", "a..b.com", "localhost", "", "http://a.com/x", "a.com^"):
            self.assertEqual(self.add("allow", bad).status_code, 422, bad)
        self.assertEqual(self.add("maybe", "a.example.org").status_code, 422)
        for baseline in ("google.com", "www.google.com", "googleapis.com", "t.me", "telegram.org", "api.telegram.org", "accuweather.com"):
            r = self.add("deny", baseline)
            self.assertEqual((r.status_code, r.json()["error"]["problems"][0]["code"]), (422, "invariant"), baseline)
        self.assertEqual(self.add("allow", "www.google.com").status_code, 200)                                    # allowing the baseline is harmless
        self.assertEqual(self.add("allow", "a.example.org", role="readonly").status_code, 403)
        self.assertEqual(self.req("POST", "/v1/child/child_a/domains", role="household", json={"kind": "allow", "domain": "a.example.org", "x": 1}).status_code, 400)
        self.assertEqual(self.req("DELETE", "/v1/child/child_a/domains?kind=allow", role="household").status_code, 400)

    def test_every_change_is_audited_with_the_lists_before_and_after(self):
        self.add("deny", "bad.example.org")
        a = self.req("GET", "/v1/audit?entity=child_a&limit=1").json()["audit"][0]
        self.assertEqual((a["action"], a["before"]["lists"], a["after"]["lists"]), ("child_domain_add", {"allow": [], "deny": []}, {"allow": [], "deny": ["bad.example.org"]}))
        ok, rows, bad = self.e.store.verify_audit()
        self.assertTrue(ok)


class Invariants(unittest.TestCase):
    def snap(self):
        e = Env()
        e.req("PUT", "/v1/child-policy/import", json=PAYLOAD)
        return copy.deepcopy(snap_file(e))

    def test_a_good_snapshot_passes(self):
        self.assertEqual(child_invariants.verify_snapshot(self.snap()), [])

    def test_the_job_refuses_a_snapshot_that_breaks_an_invariant(self):
        s = self.snap()
        s["lists"]["allow_always"].remove("kidsmanagement-pa.googleapis.com")
        self.assertEqual(child_invariants.verify_snapshot(s)[0]["code"], "always_on")
        s = self.snap()
        s["always_services"] = []
        self.assertEqual(child_invariants.verify_snapshot(s)[0]["code"], "always_services")
        s = self.snap()
        s["devices"] = []
        self.assertEqual(child_invariants.verify_snapshot(s)[0]["code"], "devices")
        s = self.snap()
        s["devices"][0]["deny_extra"] = ["google.com"]
        self.assertIn("deny_baseline", [p["code"] for p in child_invariants.verify_snapshot(s)])
        s = self.snap()
        s["devices"][0]["ip"] = "300.1.1.1"
        self.assertIn("device", [p["code"] for p in child_invariants.verify_snapshot(s)])
        s = self.snap()
        s["devices"][1]["ip"] = s["devices"][0]["ip"]
        self.assertIn("duplicate", [p["code"] for p in child_invariants.verify_snapshot(s)])
        s = self.snap()
        [x for x in s["devices"][0]["rules"] if x["id"] == "yt_roblox"][0]["start"] = "07:00"                      # a window outside the guardrail travels in the file: refused by the job too
        self.assertIn("window", [p["code"] for p in child_invariants.verify_snapshot(s)])
        s = self.snap()
        s["devices"][0]["exceptions"] = [{"id": "x", "target": "tiktok", "expires_ts": 1}]
        self.assertIn("exception", [p["code"] for p in child_invariants.verify_snapshot(s)])
        s = self.snap()
        s["devices"][0]["allow_extra"] = ["a b$c"]
        self.assertIn("domain", [p["code"] for p in child_invariants.verify_snapshot(s)])
        self.assertEqual(child_invariants.verify_snapshot(None)[0]["code"], "schema")
        self.assertEqual(child_invariants.verify_snapshot({"schema": 2})[0]["code"], "schema")

    def test_the_service_does_not_publish_a_snapshot_that_breaks_an_invariant(self):
        e = Env()
        e.req("PUT", "/v1/child-policy/import", json=PAYLOAD)
        before = open(os.path.join(e.store.out, "child-policy.json")).read()
        with e.store._tx():                                                 # tampering below the validation layer (a corrupted database)
            e.store.db.execute("UPDATE meta SET value=? WHERE key='child_lists'", (json.dumps({"lists": {"allow_always": [], "allow_weekdays": [], "allow_extra": [], "deny_extra": []},
                                                                                              "always_services": ["telegram"]}),))
        e.store.publish()
        self.assertEqual(open(os.path.join(e.store.out, "child-policy.json")).read(), before, "the previous good snapshot stays")
        self.assertIn("always-on", e.store.child_publish_error)
        self.assertEqual(e.req("GET", "/v1/health", token="").json()["status"], "ok")             # unauthenticated: liveness only
        self.assertEqual(e.req("GET", "/v1/health", role="admin").json()["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
