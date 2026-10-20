"""Schedules through the gateway: a proxy of the REAL schedule service. Who acted is recorded there (X-Acting-User); the household role gets exactly what
household_editable allows, and the SERVICE refuses the rest too (checked directly, not only through the gateway)."""
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase  # noqa: E402

ALL = [1, 2, 3, 4, 5, 6, 7]


def setUpModule():
    BaseCase.ENV = Env()


def tearDownModule():
    BaseCase.ENV.close()


def direct(env, method, path, secret, body=None, headers=None):
    """Call the schedule service itself, bypassing the gateway."""
    h = {"Authorization": "Bearer " + secret, "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(env.sched.url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class Proxy(BaseCase):
    def doc(self, sid, who="ivan"):
        r = self.e.req("GET", "/v1/schedules/" + sid, who=who)
        self.assertEqual(r.status_code, 200)
        return r

    def put(self, sid, rules, who="ivan", etag=None, **kw):
        etag = etag or self.doc(sid, who).headers["ETag"]
        return self.e.req("PUT", "/v1/schedules/" + sid, who=who, headers={"If-Match": etag}, json={"rules": rules}, **kw)

    def last_service_audit(self, entity):
        return self.e.sched.store.audit(entity=entity, limit=1)[0]

    def test_get_has_etag_guardrails_and_can_edit(self):
        r = self.doc("bathroom_towels", "dasha")
        j = r.json()
        self.assertTrue(r.headers["ETag"])
        self.assertEqual(j["guardrails"]["max_on_minutes"], 120)
        self.assertEqual(len(j["rules"]), 2)
        self.assertFalse(j["can_edit"])                                       # household, dryer: not household_editable
        self.assertTrue(self.doc("plant_rack1", "dasha").json()["can_edit"])
        self.assertTrue(self.doc("bathroom_towels", "ivan").json()["can_edit"])
        self.assertFalse(self.doc("plant_rack1", "ola").json()["can_edit"])

    def test_unknown_and_child_schedules_are_not_reachable(self):
        self.assertEqual(self.e.req("GET", "/v1/schedules/nope", who="ivan").status_code, 404)
        self.assertEqual(self.e.req("GET", "/v1/schedules/child_vlad_tablet_lenovo", who="ivan").status_code, 404)

    def test_household_edit_goes_through_and_the_service_records_who_acted(self):
        rules = [{"id": "day", "days": ALL, "start": "07:15", "end": "20:00"}]
        r = self.put("plant_rack1", rules, who="dasha")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rules"][0]["start"], "07:15")
        a = self.last_service_audit("plant_rack1")
        self.assertEqual(a["action"], "update")
        self.assertEqual(a["actor"]["name"], "gw-household")                    # the household service token, because the person is household
        self.assertEqual(a["actor"]["acting"], {"person": "dasha", "phone": self.e.people_ids["dasha"]})
        g = self.e.db.audit_rows(1)[0]
        self.assertEqual((g["action"], g["person"], g["target"], g["ok"]), ("schedule_put", "dasha", "plant_rack1", True))

    def test_admin_edit_uses_the_admin_service_token(self):
        self.put("plant_rack2", [{"id": "day", "days": ALL, "start": "07:20", "end": "20:00"}], who="ivan")
        a = self.last_service_audit("plant_rack2")
        self.assertEqual((a["actor"]["name"], a["actor"]["role"]), ("gw-admin", "admin"))
        self.assertEqual(a["actor"]["acting"]["person"], "ivan")

    def test_household_cannot_edit_a_schedule_that_is_not_household_editable__gateway_AND_service(self):
        d = self.doc("bathroom_towels", "ivan")
        rules = d.json()["rules"][:1]
        r = self.e.req("PUT", "/v1/schedules/bathroom_towels", who="dasha", headers={"If-Match": d.headers["ETag"]}, json={"rules": rules})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"))
        # the SERVICE refuses it as well - with the gateway's own household token, even when the header claims an admin ...
        st, body = direct(self.e, "PUT", "/v1/schedules/bathroom_towels", self.e.sched.house_secret, {"rules": rules},
                          {"If-Match": d.headers["ETag"], "X-Acting-User": "person=ivan;phone=-"})
        self.assertEqual(st, 403)
        # ... and with a plain household token
        st, body = direct(self.e, "PUT", "/v1/schedules/bathroom_towels", self.e.sched.plain_house_secret, {"rules": rules}, {"If-Match": d.headers["ETag"]})
        self.assertEqual(st, 403)
        self.assertEqual(self.doc("bathroom_towels").json()["version"], d.json()["version"])           # nothing changed

    def test_readonly_cannot_write_anything(self):
        d = self.doc("plant_rack1", "ola")
        for method, path, body in (("PUT", "/v1/schedules/plant_rack1", {"rules": d.json()["rules"]}), ("POST", "/v1/schedules/plant_rack1/preview", {"rules": []}),
                                   ("POST", "/v1/schedules/plant_rack1/undo", None), ("POST", "/v1/schedules/plant_rack1/exceptions", {"kind": "skip_date", "date": "2099-01-01"})):
            r = self.e.req(method, path, who="ola", headers={"If-Match": d.headers["ETag"]}, json=body)
            self.assertEqual(r.status_code, 403, path)

    def test_a_guardrail_violation_is_passed_through_with_its_reasons(self):
        d = self.doc("bathroom_towels", "ivan")
        bad = [{"id": "morning", "days": ALL, "start": "06:30", "end": "09:00"}, d.json()["rules"][1]]
        r = self.e.req("PUT", "/v1/schedules/bathroom_towels", who="ivan", headers={"If-Match": d.headers["ETag"]}, json={"rules": bad})
        e = r.json()["error"]
        self.assertEqual((r.status_code, e["code"]), (422, "validation_failed"))
        self.assertEqual(e["details"]["problems"][0]["code"], "max_on")
        self.assertIn("150 min", e["details"]["problems"][0]["message"])
        self.assertEqual(self.doc("bathroom_towels").json()["version"], d.json()["version"])
        self.assertFalse(self.e.db.audit_rows(1)[0]["ok"])

    def test_the_gateway_has_no_way_to_edit_guardrails_or_flags(self):
        for method, path in (("PUT", "/v1/entities/bathroom_towels/guardrails"), ("PATCH", "/v1/entities/bathroom_towels"), ("PUT", "/v1/schedules/bathroom_towels/guardrails")):
            r = self.e.req(method, path, who="ivan", json={"max_on_minutes": 999}, validate=False)
            self.assertIn(r.status_code, (404, 405), path)

    def test_if_match_is_required_and_a_stale_one_is_refused(self):
        rules = [{"id": "day", "days": ALL, "start": "07:00", "end": "20:00"}]
        r = self.e.req("PUT", "/v1/schedules/plant_cacti", who="ivan", json={"rules": rules})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (428, "precondition_required"))
        r = self.e.req("PUT", "/v1/schedules/plant_cacti", who="ivan", headers={"If-Match": '"999"'}, json={"rules": rules})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (412, "version_conflict"))

    def test_two_editors_cannot_overwrite_each_other(self):
        d = self.doc("plant_orchids", "ivan")
        a = self.e.req("PUT", "/v1/schedules/plant_orchids", who="ivan", headers={"If-Match": d.headers["ETag"]}, json={"rules": [{"id": "day", "days": ALL, "start": "07:10", "end": "20:00"}]})
        b = self.e.req("PUT", "/v1/schedules/plant_orchids", who="dasha", headers={"If-Match": d.headers["ETag"]}, json={"rules": [{"id": "day", "days": ALL, "start": "07:20", "end": "20:00"}]})
        self.assertEqual((a.status_code, b.status_code), (200, 412))
        self.assertEqual(self.doc("plant_orchids").json()["rules"][0]["start"], "07:10")

    def test_preview_changes_nothing(self):
        d = self.doc("plant_rack1", "dasha")
        before = self.e.sched.store.audit_seq()
        r = self.e.req("POST", "/v1/schedules/plant_rack1/preview", who="dasha", json={"rules": [{"id": "day", "days": ALL, "start": "08:00", "end": "20:00"}]})
        j = r.json()
        self.assertEqual((r.status_code, j["valid"]), (200, True))
        self.assertEqual(len(j["next_7_days"]), 14)
        bad = self.e.req("POST", "/v1/schedules/plant_rack1/preview", who="dasha", json={"rules": [{"id": "day", "days": ALL, "start": "03:00", "end": "20:00"}]}).json()
        self.assertFalse(bad["valid"])
        self.assertEqual(self.e.sched.store.audit_seq(), before)
        self.assertEqual(self.doc("plant_rack1").headers["ETag"], d.headers["ETag"])

    def test_undo_restores_and_both_audits_show_it(self):
        orig = self.doc("plant_cacti").json()["rules"]
        self.put("plant_cacti", [{"id": "day", "days": ALL, "start": "07:40", "end": "20:00"}], who="dasha")
        r = self.e.req("POST", "/v1/schedules/plant_cacti/undo", who="dasha")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["rules"], orig)
        a = self.last_service_audit("plant_cacti")
        self.assertEqual((a["action"], a["actor"]["acting"]["person"]), ("undo", "dasha"))
        self.assertEqual([x[0] for x in self.audit_actions(3)][:2], ["schedule_undo", "schedule_put"])

    def test_exceptions_round_trip(self):
        r = self.e.req("POST", "/v1/schedules/plant_rack1/exceptions", who="dasha", json={"kind": "skip_date", "date": "2099-01-01"})
        self.assertEqual(r.status_code, 422)                                     # beyond a year: the service's guardrail
        import datetime as dt
        day = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        r = self.e.req("POST", "/v1/schedules/plant_rack1/exceptions", who="dasha", json={"kind": "skip_date", "date": day})
        self.assertEqual(r.status_code, 201, r.text)
        xid = r.json()["id"]
        lst = self.e.req("GET", "/v1/schedules/plant_rack1/exceptions", who="ola").json()["exceptions"]
        self.assertEqual([x["id"] for x in lst], [xid])
        self.assertEqual(self.e.req("DELETE", "/v1/exceptions/" + xid, who="dasha").status_code, 204)
        self.assertEqual(self.e.req("DELETE", "/v1/exceptions/" + xid, who="dasha").status_code, 404)
        self.assertEqual(self.e.req("DELETE", "/v1/exceptions/nope", who="ola").status_code, 403)
        # the dryer is not household editable: the service refuses the household there too
        r = self.e.req("POST", "/v1/schedules/bathroom_towels/exceptions", who="dasha", json={"kind": "skip_date", "date": day})
        self.assertEqual(r.status_code, 403)

    def test_vacuum_plans_are_schedules_too(self):
        d = self.doc("vacuum_bedroom", "dasha")
        self.assertTrue(d.json()["can_edit"])
        self.assertEqual([r["id"] for r in d.json()["rules"]], ["morning", "evening"])

    def test_schedule_put_is_idempotent_with_a_key(self):
        d = self.doc("plant_rack2", "ivan")
        h = {"If-Match": d.headers["ETag"], "Idempotency-Key": "put-key-0001"}
        rules = [{"id": "day", "days": ALL, "start": "07:11", "end": "20:00"}]
        a = self.e.req("PUT", "/v1/schedules/plant_rack2", who="ivan", headers=h, json={"rules": rules})
        b = self.e.req("PUT", "/v1/schedules/plant_rack2", who="ivan", headers=h, json={"rules": rules})
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertTrue(b.json()["replayed"])
        self.assertEqual(b.json()["version"], a.json()["version"])

    def test_the_schedule_service_down_is_a_502_not_a_crash(self):
        saved = self.e.core.sched.url
        self.e.core.sched.url = "http://127.0.0.1:9"
        try:
            r = self.e.req("GET", "/v1/schedules/plant_rack1", who="ivan")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "schedule_service_unreachable"))
            self.assertEqual(self.e.req("GET", "/v1/devices", who="ivan").status_code, 200)        # the cards keep working (HA state is not behind the schedule service)
        finally:
            self.e.core.sched.url = saved


class Tiles(BaseCase):
    def test_visibility_and_badges(self):
        self.e.run(self.e.core.probe_all())
        ivan = {t["id"]: t for t in self.e.req("GET", "/v1/services", who="ivan").json()["services"]}
        dasha = {t["id"]: t for t in self.e.req("GET", "/v1/services", who="dasha").json()["services"]}
        self.assertEqual(sorted(dasha), ["fakeweb"])                              # household sees only household tiles
        self.assertEqual(sorted(ivan), ["adminonly", "fakeweb", "hasvc", "schedsvc"])
        self.assertEqual(ivan["fakeweb"]["badge"], "ok")                          # an HTTP answer below 500 counts as up
        self.assertEqual(ivan["hasvc"]["badge"], "ok")
        self.assertEqual(ivan["adminonly"]["badge"], "down")                      # nothing answers DNS on 127.0.0.1
        self.assertEqual(ivan["schedsvc"]["badge"], "degraded")                   # rendered config is not applied in this harness: in_sync false
        self.assertIsNotNone(ivan["fakeweb"]["rtt_ms"])
        self.assertIsNotNone(ivan["fakeweb"]["last_ok"])
        self.assertTrue(ivan["adminonly"]["url"].startswith("https://"))

    def test_the_ha_tile_follows_the_link(self):
        self.e.ha.down = True
        self.assertTrue(self.e.wait(lambda: self.e.haclient.link == "down", 40))
        self.e.run(self.e.core.probe_all())
        self.assertEqual({t["id"]: t for t in self.e.req("GET", "/v1/services", who="ivan").json()["services"]}["hasvc"]["badge"], "down")
        self.e.ha.down = False
        self.assertTrue(self.e.wait(lambda: self.e.haclient.link == "ws", 40))
        self.e.run(self.e.core.probe_all())
        self.assertEqual({t["id"]: t for t in self.e.req("GET", "/v1/services", who="ivan").json()["services"]}["hasvc"]["badge"], "ok")

    def test_a_failing_probe_never_sends_anything(self):
        # the gateway has no Telegram code at all
        src = open(os.path.join(os.path.dirname(HERE), "gw", "core.py")).read() + open(os.path.join(os.path.dirname(HERE), "gw", "api.py")).read()
        for word in ("telegram", "watchnotify", "api.telegram"):
            self.assertNotIn(word, src.lower())


if __name__ == "__main__":
    unittest.main()
