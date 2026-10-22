"""Task 31 Part A (Ivan, 2026-10-22): Maps moved to the house-wide always-open set. No window, no switch, no exception for it; open on all seven days.

What is proven here: (1) the old arrangement (a `maps` rule, a `maps` exception, the domains in allow_weekdays) is migrated once, audited, idempotently; (2) the
rendered block differs from the old one ONLY in the Maps lines becoming unconditional; (3) nothing accepts `maps` any more (clear refusal, not a silent accept);
(4) every child has the Maps domains on every weekday; (5) deny still beats allow; (6) the rollback restores the exact old snapshot.
"""
import copy
import datetime as dt
import json
import os
import unittest

from sched import child_invariants, child_render, model
from sched.child_store import ChildMixin
from tests.helpers import Env, SYSTEM

MON_SAT = [1, 2, 3, 4, 5, 6]
YT = {"id": "yt_roblox", "days": MON_SAT, "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}
OLD_MAPS = {"id": "maps", "days": MON_SAT, "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}}
MAPS = list(ChildMixin.MAPS_DOMAINS)
SD = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com", "ytimg.com"], "roblox": ["roblox.com"]}
# the old arrangement as it is on the live house today: the three domains in allow_weekdays, a maps rule per child
OLD_LISTS = {"allow_always": sorted(child_invariants.REQUIRED_ALWAYS), "allow_weekdays": sorted(MAPS), "allow_extra": [], "deny_extra": []}


def at(day, h, m=0):          # 2026-10-19 is a Monday
    return dt.datetime(2026, 10, 19 + day, h, m, tzinfo=model.TZ)


def old_world():
    """A store in the state BEFORE the migration, written below the validation layer (the validators refuse a maps rule now - that is the point)."""
    e = Env()
    payload = {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "", "rules": [YT]},
                           {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": [YT]}],
               "lists": {**OLD_LISTS, "allow_weekdays": [], "allow_always": sorted(child_invariants.REQUIRED_ALWAYS)}, "always_services": ["telegram"]}
    assert e.req("PUT", "/v1/child-policy/import", json=payload).status_code == 200
    st = e.store
    with st._tx():
        for eid in ("child_a", "child_b"):
            st.db.execute("UPDATE schedules SET rules=?, version=version+1 WHERE entity_id=?", (json.dumps([YT, OLD_MAPS]), eid))
        st.db.execute("UPDATE meta SET value=? WHERE key='child_lists'", (json.dumps({"lists": OLD_LISTS, "always_services": ["telegram"]}),))
        exp = (st._now() + dt.timedelta(minutes=45)).isoformat(timespec="seconds")
        for eid, tgt in (("child_a", "maps"), ("child_b", "youtube")):
            st.db.execute("INSERT INTO child_exceptions(id,entity_id,target,created_at,expires_at,created_by,status,ended_at) VALUES (?,?,?,?,?,?,?,?)",
                          ("x_" + eid, eid, tgt, st._now().isoformat(timespec="seconds"), exp, json.dumps(SYSTEM), "active", None))
    return e


def block(store, now, overrides=()):
    return child_render.render_block(store.child_policy(), SD, now, overrides)


class Migration(unittest.TestCase):
    def test_the_old_data_is_migrated_once_and_audited(self):
        e = old_world()
        st = e.store
        self.assertTrue(st.maps_migration_pending())
        out = st.migrate_maps_always_open()
        self.assertEqual((out["rules_changed"], out["exceptions_cancelled"], out["moved_from_weekdays"]), (["child_a", "child_b"], 1, sorted(MAPS)))
        for eid in ("child_a", "child_b"):
            self.assertEqual([r["id"] for r in st.schedule(eid)["rules"]], ["yt_roblox"])
        lists = st.child_house_lists()["lists"]
        self.assertEqual(lists["allow_weekdays"], [])
        self.assertTrue(set(MAPS) <= set(lists["allow_always"]))
        self.assertTrue(set(child_invariants.REQUIRED_ALWAYS) <= set(lists["allow_always"]))                  # nothing else was lost
        self.assertEqual([(x["entity"], x["target"]) for x in st.child_exceptions()], [("child_b", "youtube")])      # the maps exception is gone, the youtube one stays
        row = st.audit(limit=1)[0]
        self.assertEqual(row["action"], "child_maps_always_open")
        self.assertEqual(sorted(row["before"]["rules"]), ["child_a", "child_b"])
        self.assertEqual(row["before"]["allow_weekdays"], sorted(MAPS))
        self.assertTrue(st.verify_audit()[0])
        self.assertFalse(st.maps_migration_pending())
        self.assertEqual(st.child_publish_error, None)

    def test_idempotent(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        seq = e.store.audit_seq()
        self.assertIsNone(e.store.migrate_maps_always_open())
        self.assertEqual(e.store.audit_seq(), seq, "a second run writes nothing")

    def test_a_combined_rule_loses_only_the_maps_part(self):
        e = old_world()
        with e.store._tx():
            e.store.db.execute("UPDATE schedules SET rules=? WHERE entity_id='child_a'",
                               (json.dumps([{**YT, "variables": {"opens": "youtube,maps"}}]),))
        e.store.migrate_maps_always_open()
        self.assertEqual(e.store.schedule("child_a")["rules"][0]["variables"]["opens"], "youtube")

    def test_nothing_to_do_without_an_imported_policy(self):
        e = Env()
        self.assertFalse(e.store.maps_migration_pending())
        self.assertIsNone(e.store.migrate_maps_always_open())

    def test_the_published_snapshot_passes_the_invariants_and_has_no_maps_anywhere(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        snap = json.load(open(os.path.join(e.store.out, "child-policy.json")))
        self.assertEqual(child_invariants.verify_snapshot(snap), [])
        self.assertNotIn('"maps"', json.dumps(snap["devices"]))
        self.assertEqual(snap["lists"]["allow_weekdays"], [])


class RenderedBlock(unittest.TestCase):
    def test_the_only_difference_is_the_maps_lines_becoming_unconditional(self):
        e = old_world()
        st = e.store
        times = [at(d, h, m) for d in range(7) for h in (0, 8, 12, 15, 16, 18, 19, 23) for m in (0, 30)]
        old = {t: block(st, t) for t in times}
        st.migrate_maps_always_open()
        maps_lines = {"@@||%s^$client=%s" % (d, ip) for d in MAPS for ip in ("192.168.31.60", "192.168.31.61")}
        changed = 0
        for t in times:
            new = block(st, t)
            gone = set(old[t]) - set(new)
            added = set(new) - set(old[t])
            self.assertEqual(gone, set(), t)                                          # nothing that was allowed became blocked
            self.assertTrue(added <= maps_lines, (t, added - maps_lines))              # nothing else was added
            if t.isoweekday() == 7:
                self.assertEqual(added, maps_lines, t)                                  # Sunday: the Maps lines appear
                changed += 1
            else:
                self.assertEqual(added, set(), t)                                       # Monday-Saturday: byte-identical to before (the old weekday window was all day)
            self.assertTrue(maps_lines <= set(new), t)                                  # and they are there on every day
        self.assertGreater(changed, 10)

    def test_every_child_has_the_maps_domains_on_all_seven_days_at_every_hour(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        for ip in ("192.168.31.60", "192.168.31.61"):
            for d in range(7):
                for h in range(24):
                    lines = set(block(e.store, at(d, h, 15)))
                    for dom in MAPS:
                        self.assertIn("@@||%s^$client=%s" % (dom, ip), lines, (ip, d, h, dom))

    def test_an_everything_exception_still_renders_only_the_deny_rules(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        lines = block(e.store, at(0, 12), [{"client": "192.168.31.60", "target": "all"}])
        self.assertFalse([l for l in lines if "192.168.31.60" in l and l.startswith("@@")])

    def test_deny_still_beats_the_always_open_maps(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        self.assertEqual(e.req("POST", "/v1/child/child_a/domains", json={"kind": "deny", "domain": "maps.google.com"}).status_code, 200)
        a = {l for l in block(e.store, at(2, 12)) if l.endswith("$client=192.168.31.60")}
        self.assertNotIn("@@||maps.google.com^$client=192.168.31.60", a)
        self.assertIn("||maps.google.com^$client=192.168.31.60", a)
        self.assertIn("@@||maps.google.com^$client=192.168.31.61", set(block(e.store, at(2, 12))))      # the other child is unaffected

    def test_the_weekday_mechanism_exists_and_is_empty(self):
        e = old_world()
        e.store.migrate_maps_always_open()
        self.assertEqual(e.store.child_house_lists()["lists"]["allow_weekdays"], [])
        pol = e.store.child_policy()
        pol["lists"]["allow_weekdays"] = ["weekday.example.com"]
        self.assertIn("@@||weekday.example.com^$client=192.168.31.60", child_render.render_block(pol, SD, at(5, 12)))      # Saturday
        self.assertNotIn("@@||weekday.example.com^$client=192.168.31.60", child_render.render_block(pol, SD, at(6, 12)))   # Sunday


class NothingAcceptsMapsAnyMore(unittest.TestCase):
    def setUp(self):
        self.e = old_world()
        self.e.store.migrate_maps_always_open()

    def codes(self, r):
        return [p["code"] for p in r.json()["error"]["problems"]]

    def test_exception_target(self):
        r = self.e.req("POST", "/v1/child/child_a/exceptions", role="household", json={"target": "maps", "minutes": 30})
        self.assertEqual((r.status_code, self.codes(r)), (422, ["maps_always_open"]))
        self.assertEqual(self.e.req("POST", "/v1/child/child_a/exceptions", role="household", json={"target": "youtube", "minutes": 60}).status_code, 201)

    def test_window_target(self):
        g = self.e.req("GET", "/v1/schedules/child_a")
        for opens in ("maps", "youtube,maps"):
            r = self.e.req("PUT", "/v1/schedules/child_a", headers={"If-Match": g.headers["ETag"]},
                           json={"rules": [{"id": "r", "days": [1], "start": "16:00", "end": "17:00", "variables": {"opens": opens}}]})
            self.assertEqual((r.status_code, self.codes(r)), (422, ["maps_always_open"]))

    def test_a_new_device_gets_one_window_and_no_maps_rule(self):
        r = self.e.req("POST", "/v1/child", json={"name": "Anna", "ip": "192.168.31.70"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual([x["id"] for x in r.json()["rules"]], ["yt_roblox"])
        self.assertNotIn("maps", model.CHILD_TARGETS)
        self.assertNotIn("maps", model.EXCEPTION_TARGETS)

    def test_the_host_job_refuses_a_snapshot_that_still_has_a_maps_rule_or_exception(self):
        snap = json.load(open(os.path.join(self.e.store.out, "child-policy.json")))
        bad = copy.deepcopy(snap)
        bad["devices"][0]["rules"] = [YT, OLD_MAPS]
        self.assertTrue([p for p in child_invariants.verify_snapshot(bad) if p["code"] == "window"])
        bad = copy.deepcopy(snap)
        bad["devices"][0]["exceptions"] = [{"id": "x", "target": "maps", "created_at": "2026-10-22T12:00:00+02:00", "expires_at": "2026-10-22T13:00:00+02:00",
                                            "expires_ts": 1790000000, "created_by": "t"}]
        self.assertTrue([p for p in child_invariants.verify_snapshot(bad) if p["code"] == "exception"])


class Rollback(unittest.TestCase):
    def test_rollback_restores_the_exact_old_snapshot(self):
        e = old_world()
        st = e.store
        before = st.child_snapshot()
        st.migrate_maps_always_open()
        self.assertNotEqual(st.child_snapshot()["checksum"], before["checksum"])
        out = st.rollback_maps_always_open()
        self.assertEqual((out["rules_restored"], out["exceptions_revived"]), (["child_a", "child_b"], 1))
        after = st.child_snapshot()
        for k in ("lists", "always_services"):
            self.assertEqual(after[k], before[k])
        self.assertEqual([d["rules"] for d in after["devices"]], [d["rules"] for d in before["devices"]])
        self.assertEqual([[x["target"] for x in d["exceptions"]] for d in after["devices"]], [["maps"], ["youtube"]])
        self.assertTrue(st.verify_audit()[0])
        self.assertEqual(st.audit(limit=1)[0]["action"], "child_maps_always_open_rollback")

    def test_rollback_without_a_migration_is_a_noop(self):
        self.assertIsNone(Env().store.rollback_maps_always_open())


if __name__ == "__main__":
    unittest.main()
