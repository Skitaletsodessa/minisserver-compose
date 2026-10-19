import os
import json
import sqlite3
import unittest
from tests.helpers import Env, SYSTEM

ALL = [1, 2, 3, 4, 5, 6, 7]
PLANT = [{"id": "day", "days": ALL, "start": "07:00", "end": "20:00"}]


def etag(r):
    return r.headers["ETag"]


class Auth(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def test_health_is_public_but_minimal(self):
        r = self.e.req("GET", "/v1/health", token="")
        self.assertEqual(r.json(), {"status": "ok"})

    def test_no_token_and_bad_token(self):
        self.assertEqual(self.e.req("GET", "/v1/entities", token="").status_code, 401)
        self.assertEqual(self.e.req("GET", "/v1/entities", token="nonsense").status_code, 401)

    def test_roles(self):
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        self.assertEqual(self.e.req("GET", "/v1/entities", role="readonly").status_code, 200)
        # readonly cannot write
        r = self.e.req("PUT", "/v1/schedules/plant_rack1", role="readonly", headers={"If-Match": etag(g)}, json={"rules": PLANT})
        self.assertEqual(r.status_code, 403)
        # household cannot touch guardrails, tokens, entity settings
        self.assertEqual(self.e.req("PUT", "/v1/entities/plant_rack1/guardrails", role="household", json={"max_on_minutes": 999}).status_code, 403)
        self.assertEqual(self.e.req("GET", "/v1/tokens", role="household").status_code, 403)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_rack1", role="household", json={"managed": True}).status_code, 403)

    def test_household_edits_only_household_editable_entities(self):
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        ok = self.e.req("PUT", "/v1/schedules/plant_rack1", role="household", headers={"If-Match": etag(g)},
                        json={"rules": [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}]})
        self.assertEqual(ok.status_code, 200)
        d = self.e.req("GET", "/v1/schedules/bathroom_towels")
        no = self.e.req("PUT", "/v1/schedules/bathroom_towels", role="household", headers={"If-Match": etag(d)},
                        json={"rules": d.json()["rules"][:1]})
        self.assertEqual(no.status_code, 403)
        # admin may, and may flip the flag
        self.assertEqual(self.e.req("PATCH", "/v1/entities/bathroom_towels", json={"flags": {"household_editable": True}}).status_code, 200)
        d = self.e.req("GET", "/v1/schedules/bathroom_towels")
        self.assertEqual(self.e.req("PUT", "/v1/schedules/bathroom_towels", role="household", headers={"If-Match": etag(d)},
                                    json={"rules": d.json()["rules"][:1]}).status_code, 200)

    def test_failed_attempts_are_rate_limited(self):
        for _ in range(10):
            self.assertEqual(self.e.req("GET", "/v1/entities", token="wrong").status_code, 401)
        r = self.e.req("GET", "/v1/entities", token="wrong")
        self.assertEqual(r.status_code, 429)
        # even the right token is refused from that address during the window (a guessed-at endpoint stays closed)
        self.assertEqual(self.e.req("GET", "/v1/entities").status_code, 429)

    def test_revoked_and_expired_tokens(self):
        tid, secret = self.e.store.create_token(SYSTEM, "temp", "readonly")
        self.assertEqual(self.e.req("GET", "/v1/entities", token=secret).status_code, 200)
        self.assertEqual(self.e.req("DELETE", "/v1/tokens/" + tid).status_code, 204)
        self.assertEqual(self.e.req("GET", "/v1/entities", token=secret).status_code, 401)
        tid2, secret2 = self.e.store.create_token(SYSTEM, "old", "readonly", expires="2020-01-01T00:00:00+01:00")
        self.assertEqual(self.e.req("GET", "/v1/entities", token=secret2).status_code, 401)

    def test_token_secret_is_returned_once_and_stored_hashed(self):
        r = self.e.req("POST", "/v1/tokens", json={"name": "gateway", "role": "household"})
        self.assertEqual(r.status_code, 201)
        secret = r.json()["token"]
        listing = self.e.req("GET", "/v1/tokens").text
        self.assertNotIn(secret, listing)
        db = sqlite3.connect(os.path.join(self.e.store.dir, "schedules.db"))
        stored = " ".join(str(c) for row in db.execute("SELECT * FROM tokens") for c in row)
        self.assertNotIn(secret, stored)
        self.assertNotIn(secret, open(os.path.join(self.e.store.dir, "audit.jsonl")).read())
        self.assertEqual(self.e.req("POST", "/v1/tokens", json={"name": "gateway", "role": "admin"}).status_code, 422)   # duplicate name
        self.assertEqual(self.e.req("POST", "/v1/tokens", json={"name": "x", "role": "root"}).status_code, 422)


class Schedules(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def put(self, eid, rules, role="admin", version=None):
        g = self.e.req("GET", "/v1/schedules/" + eid)
        return self.e.req("PUT", "/v1/schedules/" + eid, role=role, headers={"If-Match": version or etag(g)}, json={"rules": rules})

    def test_get_and_list(self):
        r = self.e.req("GET", "/v1/schedules/bathroom_towels")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["ETag"], '"1"')
        self.assertEqual(r.json()["guardrails"]["max_on_minutes"], 120)
        self.assertEqual(len(self.e.req("GET", "/v1/schedules").json()["schedules"]), 8)
        self.assertEqual(self.e.req("GET", "/v1/schedules/nope").status_code, 404)

    def test_put_bumps_the_version_and_audits(self):
        r = self.put("plant_rack1", [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}])
        self.assertEqual((r.status_code, r.json()["version"], r.json()["changed"]), (200, 2, True))
        self.assertEqual(r.headers["ETag"], '"2"')
        a = self.e.req("GET", "/v1/audit?entity=plant_rack1&limit=1").json()["audit"][0]
        self.assertEqual((a["action"], a["actor"]["name"], a["before"]["rules"][0]["start"], a["after"]["rules"][0]["start"]),
                         ("update", "t-admin", "07:00", "07:30"))

    def test_identical_put_changes_nothing(self):
        r = self.put("plant_rack1", PLANT)
        self.assertEqual((r.status_code, r.json()["version"], r.json()["changed"]), (200, 1, False))

    def test_if_match_is_required_and_checked(self):
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", json={"rules": PLANT}).status_code, 428)
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", headers={"If-Match": '"99"'}, json={"rules": PLANT}).status_code, 412)

    def test_two_editors_cannot_overwrite_each_other(self):
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        a = self.e.req("PUT", "/v1/schedules/plant_rack1", headers={"If-Match": etag(g)}, json={"rules": [{"id": "day", "days": ALL, "start": "07:10", "end": "20:00"}]})
        b = self.e.req("PUT", "/v1/schedules/plant_rack1", role="household", headers={"If-Match": etag(g)},
                       json={"rules": [{"id": "day", "days": ALL, "start": "07:20", "end": "20:00"}]})
        self.assertEqual((a.status_code, b.status_code), (200, 412))
        self.assertEqual(b.headers["ETag"], '"2"')
        self.assertEqual(self.e.req("GET", "/v1/schedules/plant_rack1").json()["rules"][0]["start"], "07:10")

    def test_guardrail_violation_is_rejected_with_reasons_and_nothing_changes(self):
        r = self.put("bathroom_towels", [{"id": "morning", "days": ALL, "start": "06:30", "end": "09:00"}])
        self.assertEqual(r.status_code, 422)
        codes = [p["code"] for p in r.json()["error"]["problems"]]
        self.assertEqual(codes, ["max_on"])
        self.assertIn("150 min", r.json()["error"]["problems"][0]["message"])
        self.assertEqual(self.e.req("GET", "/v1/schedules/bathroom_towels").headers["ETag"], '"1"')
        self.assertEqual(len(self.e.req("GET", "/v1/schedules/bathroom_towels").json()["rules"]), 2)

    def test_every_violation_in_one_request_is_reported(self):
        r = self.put("bathroom_towels", [{"id": "a", "days": ALL, "start": "05:00", "end": "09:30"}, {"id": "b", "days": [], "start": "10:00", "end": "11:00"}])
        self.assertEqual(r.status_code, 422)
        self.assertGreaterEqual(len(r.json()["error"]["problems"]), 2)

    def test_preview_changes_nothing(self):
        before = self.e.store.audit_seq()
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        r = self.e.req("POST", "/v1/schedules/plant_rack1/preview", json={"rules": [{"id": "day", "days": ALL, "start": "08:00", "end": "20:00"}]})
        j = r.json()
        self.assertEqual((r.status_code, j["valid"]), (200, True))
        self.assertEqual(j["diff"]["changed"][0]["after"]["start"], "08:00")
        self.assertEqual(len(j["next_7_days"]), 14)
        self.assertEqual(self.e.store.audit_seq(), before)
        self.assertEqual(self.e.req("GET", "/v1/schedules/plant_rack1").headers["ETag"], etag(g))
        bad = self.e.req("POST", "/v1/schedules/plant_rack1/preview", json={"rules": [{"id": "day", "days": ALL, "start": "03:00", "end": "20:00"}]}).json()
        self.assertEqual((bad["valid"], bad["problems"][0]["code"]), (False, "dst_ambiguous"))

    def test_undo_is_a_new_change_and_audited(self):
        self.put("plant_rack1", [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}])
        r = self.e.req("POST", "/v1/schedules/plant_rack1/undo")
        self.assertEqual((r.status_code, r.json()["version"], r.json()["rules"][0]["start"]), (200, 3, "07:00"))
        actions = [a["action"] for a in self.e.req("GET", "/v1/audit?entity=plant_rack1").json()["audit"]]
        self.assertEqual(actions[:3], ["undo", "update", "import"])
        self.assertEqual(self.e.req("POST", "/v1/schedules/parrot_uv/undo").status_code, 404)        # nothing to undo

    def test_household_undoes_only_its_own_changes(self):
        self.put("plant_rack1", [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}], role="admin")
        self.assertEqual(self.e.req("POST", "/v1/schedules/plant_rack1/undo", role="household").status_code, 403)
        self.put("plant_rack2", [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}], role="household")
        self.assertEqual(self.e.req("POST", "/v1/schedules/plant_rack2/undo", role="household").status_code, 200)

    def test_undo_respects_changed_guardrails(self):
        self.put("plant_rack1", [{"id": "day", "days": ALL, "start": "07:00", "end": "18:00"}])        # 11 h
        # tighten the guardrail so the older (13 h) schedule would now be illegal
        self.assertEqual(self.e.req("PUT", "/v1/entities/plant_rack1/guardrails",
                                    json={"max_on_minutes": 700, "allowed_from": "05:00", "allowed_to": "22:00", "min_gap_minutes": 360}).status_code, 200)
        r = self.e.req("POST", "/v1/schedules/plant_rack1/undo")
        self.assertEqual((r.status_code, r.json()["error"]["problems"][0]["code"]), (422, "max_on"))

    def test_guardrail_change_cannot_orphan_the_current_schedule(self):
        r = self.e.req("PUT", "/v1/entities/bathroom_towels/guardrails", json={"max_on_minutes": 60, "allowed_from": "06:00", "allowed_to": "21:00", "min_gap_minutes": 30})
        self.assertEqual(r.status_code, 422)
        self.assertIn("would violate", r.json()["error"]["problems"][0]["message"])
        self.assertEqual(self.e.req("GET", "/v1/entities").json()["entities"][0]["guardrails"]["max_on_minutes"], 120)

    def test_guardrail_change_is_audited(self):
        self.e.req("PUT", "/v1/entities/bathroom_towels/guardrails", json={"max_on_minutes": 100, "allowed_from": "06:00", "allowed_to": "21:00", "min_gap_minutes": 30})
        a = self.e.req("GET", "/v1/audit?entity=bathroom_towels&limit=1").json()["audit"][0]
        self.assertEqual((a["action"], a["before"]["max_on_minutes"], a["after"]["max_on_minutes"]), ("guardrails", 120, 100))

    def test_bad_bodies(self):
        h = {"If-Match": '"1"'}
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", headers=h, content=b"not json").status_code, 400)
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", headers=h, json=[1]).status_code, 400)
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", headers=h, json={"nope": 1}).status_code, 400)
        self.assertEqual(self.e.req("PUT", "/v1/schedules/plant_rack1", headers=h, content=b"x" * 70000).status_code, 413)


class ExceptionsAndEffective(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def test_skip_date_flow(self):
        r = self.e.req("POST", "/v1/exceptions", json={"entity_id": "plant_rack1", "kind": "skip_date", "date": "2099-01-01"})
        self.assertEqual(r.status_code, 422)             # beyond one year
        from datetime import date, timedelta
        d = (date.today() + timedelta(days=3)).isoformat()
        r = self.e.req("POST", "/v1/exceptions", json={"entity_id": "plant_rack1", "kind": "skip_date", "date": d})
        self.assertEqual(r.status_code, 201)
        xid = r.json()["id"]
        lst = self.e.req("GET", "/v1/exceptions?entity=plant_rack1").json()["exceptions"]
        self.assertEqual([(x["id"], x["date"]) for x in lst], [(xid, d)])
        eff = self.e.req("GET", "/v1/effective?entity=plant_rack1&at=%sT12:00:00%%2B01:00" % d).json()
        self.assertEqual((eff["state"], eff["skipped_today"]), ("off", True))
        self.assertEqual(self.e.req("DELETE", "/v1/exceptions/" + xid).status_code, 204)
        self.assertEqual(self.e.req("DELETE", "/v1/exceptions/" + xid).status_code, 404)
        actions = [a["action"] for a in self.e.req("GET", "/v1/audit?entity=plant_rack1&limit=2").json()["audit"]]
        self.assertEqual(actions, ["exception_delete", "exception_add"])

    def test_exception_permissions_follow_the_household_flag(self):
        from datetime import date, timedelta
        d = (date.today() + timedelta(days=2)).isoformat()
        self.assertEqual(self.e.req("POST", "/v1/exceptions", role="household", json={"entity_id": "bathroom_towels", "kind": "skip_date", "date": d}).status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/exceptions", role="household", json={"entity_id": "plant_rack1", "kind": "skip_date", "date": d}).status_code, 201)
        self.assertEqual(self.e.req("POST", "/v1/exceptions", role="readonly", json={"entity_id": "plant_rack1", "kind": "skip_date", "date": d}).status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/exceptions", json={"entity_id": "nope", "kind": "skip_date", "date": d}).status_code, 404)

    def test_override_is_modelled_and_reported_not_enforced(self):
        r = self.e.req("POST", "/v1/exceptions", json={"entity_id": "plant_rack1", "kind": "override_until", "until": "21:00", "state": "on"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.e.req("POST", "/v1/exceptions", json={"entity_id": "plant_rack1", "kind": "override_until", "until": "23:00", "state": "on"}).status_code, 422)
        snap = json.load(open(os.path.join(self.e.store.out, "schedules.json")))
        self.assertNotIn("override", json.dumps(snap))        # an override never reaches the rendered config in step (a)

    def test_effective(self):
        eff = self.e.req("GET", "/v1/effective?entity=bathroom_towels&at=2026-10-19T07:00:00%2B02:00").json()
        self.assertEqual((eff["state"], eff["source"], eff["next_edge"]["action"], eff["next_edge"]["at"][11:16]), ("on", "schedule", "off", "08:00"))
        eff = self.e.req("GET", "/v1/effective?entity=vacuum_bedroom&at=2026-10-19T10:00:00%2B02:00").json()
        self.assertEqual((eff["state"], eff["next_edge"]["action"], eff["next_edge"]["rule"]), (None, "trigger", "evening"))
        self.assertEqual(self.e.req("GET", "/v1/effective").status_code, 400)
        self.assertEqual(self.e.req("GET", "/v1/effective?entity=plant_rack1&at=yesterday").status_code, 400)
        self.assertEqual(self.e.req("GET", "/v1/effective?entity=zzz").status_code, 404)


class Integrity(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def test_audit_chain_verifies_and_detects_tampering(self):
        self.assertTrue(self.e.store.verify_audit()[0])
        # the database refuses UPDATE/DELETE on the audit table
        for sql in ("UPDATE audit SET actor='x' WHERE seq=1", "DELETE FROM audit WHERE seq=1"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.e.store.db.execute(sql)
        # an attacker with raw file access who drops the triggers and edits a row is still caught by the chain
        db = sqlite3.connect(os.path.join(self.e.store.dir, "schedules.db"))
        db.execute("DROP TRIGGER audit_no_update")
        db.execute("UPDATE audit SET after='{\"forged\":1}' WHERE seq=2")
        db.commit()
        ok, rows, bad = self.e.store.verify_audit()
        self.assertEqual((ok, bad), (False, 2))
        h = self.e.req("GET", "/v1/health").json()
        self.assertEqual((h["status"], h["audit_chain"]["ok"], h["audit_chain"]["first_bad_seq"]), ("degraded", False, 2))

    def test_outputs_are_published_atomically_with_ready_last(self):
        out = self.e.store.out
        for f in ("schedules.json", "render.json", "READY", "render/scripts/sch_fingerprint.yaml"):
            self.assertTrue(os.path.exists(os.path.join(out, f)), f)
        seq0 = open(os.path.join(out, "READY")).read().split()[0]
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        self.e.req("PUT", "/v1/schedules/plant_rack1", headers={"If-Match": etag(g)}, json={"rules": [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}]})
        seq1 = open(os.path.join(out, "READY")).read().split()[0]
        self.assertGreater(int(seq1), int(seq0))
        snap = json.load(open(os.path.join(out, "schedules.json")))
        self.assertEqual([e["rules"][0]["start"] for e in snap["entities"] if e["id"] == "plant_rack1"], ["07:30"])
        self.assertEqual(snap["audit_seq"], int(seq1))

    def test_unmanaged_entities_do_not_change_the_rendered_set(self):
        h0 = json.load(open(os.path.join(self.e.store.out, "render.json")))["set_hash"]
        g = self.e.req("GET", "/v1/schedules/plant_rack1")
        self.e.req("PUT", "/v1/schedules/plant_rack1", headers={"If-Match": etag(g)}, json={"rules": [{"id": "day", "days": ALL, "start": "07:30", "end": "20:00"}]})
        self.assertEqual(json.load(open(os.path.join(self.e.store.out, "render.json")))["set_hash"], h0)
        self.e.req("PATCH", "/v1/entities/plant_rack1", json={"managed": True})
        self.assertNotEqual(json.load(open(os.path.join(self.e.store.out, "render.json")))["set_hash"], h0)

    def test_modelled_only_kinds_cannot_be_managed(self):
        self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_rack1", json={"bogus": 1}).status_code, 422)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/plant_rack1", json={"managed": "yes"}).status_code, 422)

    def test_seed_refuses_a_populated_database(self):
        from sched import store
        with self.assertRaises(store.Forbidden):
            self.e.store.seed(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "initial.json"), SYSTEM)

    def test_health_reports_sync_state_from_the_applier_status(self):
        h = self.e.req("GET", "/v1/health").json()
        self.assertFalse(h["in_sync"])
        render = json.load(open(os.path.join(self.e.store.out, "render.json")))
        with open(os.path.join(self.e.store.dir, "apply-status.json"), "w") as f:
            json.dump({"seq": render["audit_seq"], "set_hash": render["set_hash"], "result": "applied", "loaded_ok": True, "at": "now"}, f)
        h = self.e.req("GET", "/v1/health").json()
        self.assertTrue(h["in_sync"])

    def test_applier_results_are_audited(self):
        self.e.store.record_apply({"token": "-", "name": "applier", "role": "admin"}, {"seq": 5, "result": "failed", "at": "t1", "set_hash": "abc", "detail": "check_config failed"})
        a = self.e.req("GET", "/v1/audit?limit=1").json()["audit"][0]
        self.assertEqual((a["action"], a["after"]["detail"]), ("not_applied", "check_config failed"))
        self.assertEqual(self.e.store.last_apply_audited(), [5, "failed", "t1"])


if __name__ == "__main__":
    unittest.main()
