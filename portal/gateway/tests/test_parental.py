"""The parental module through the gateway (Task 29 Part 2): the REAL schedule service (in-process, with two child devices), a fake AdGuard (real HTTP, basic auth)
and synthetic people. Role matrix, windows (If-Match, preview, undo, guardrails), exceptions, per-child domains, activity (on demand, nothing stored), health."""
import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase, SEEN  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

TZ = ZoneInfo("Europe/Prague")
A, B = "child_a", "child_b"
BASE = "/v1/parental/children/"


def setUpModule():
    e = BaseCase.ENV = Env()
    noon = dt.datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    e.sched.store.clock = lambda: noon.isoformat(timespec="seconds")          # the service's guardrails look at "now": keep it inside every allowed range
    a = {"person_id": "test", "phone_id": None, "ip": "test"}
    for name, role, parent in (("Bob", "household", False), ("Gus", "admin", False), ("Rita", "readonly", True)):
        e.people.create(a, name, role, parent)
        code, _ = e.people.create_enrolment_code(a, name.lower())
        e.tokens[name.lower()] = e.people.enrol(code, {"label": name + "-phone", "model": "Test"}, "10.0.0.1")["token"]


def tearDownModule():
    BaseCase.ENV.close()


def now_utc(minutes_ago=0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def entry(minutes_ago, client, name, typ="A", blocked=False):
    return {"time": now_utc(minutes_ago), "client": client, "question": {"name": name + ".", "type": typ}, "reason": "FilteredBlackList" if blocked else "NotFilteredNotFound"}


class P(BaseCase):
    def setUp(self):
        super().setUp()
        e = self.e
        e.agh.down = False
        e.agh.querylog, e.agh.filters, e.agh.contents, e.agh.user_rules = [], [], {}, []
        e.o["parental"]._cache.clear()
        for cid in (A, B):                                         # the household-editable flag is part of the seed; start every test from it
            e.sched.store.patch_entity(e.sched.sys, cid, {"flags": {"household_editable": True}})
        # exceptions and domains of earlier tests
        for c in e.sched.store.child_devices():
            for x in e.sched.store.child_exceptions(c["id"]):
                e.sched.store.cancel_child_exception(e.sched.sys, c["id"], x["id"])
            for kind, vals in e.sched.store.child_domains(c["id"]).items():
                for d in vals:
                    e.sched.store.change_child_domain(e.sched.sys, c["id"], kind, d, False)
        default = [{"id": r["id"], "days": r["days"], "start": r["start"], "end": r["end"], "variables": {"opens": ",".join(r["opens"])}} for r in self.rules()]
        for cid in (A, B):                                         # every test starts from the seeded windows
            cur = e.sched.store.schedule(cid)
            e.sched.store.put_schedule(e.sched.sys, cid, default, cur["version"])
        self.etag_a = '"%d"' % e.sched.store.schedule(A)["version"]

    @staticmethod
    def byid(rules):
        return sorted(rules, key=lambda r: r["id"])

    @staticmethod
    def yt(rules):
        return next(r for r in rules if r["id"] == "yt_roblox")

    def rules(self, yt_start="16:00", yt_end="19:00"):
        return [{"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": yt_start, "end": yt_end, "opens": ["youtube", "roblox"]},
                {"id": "maps", "days": [1, 2, 3, 4, 5, 6], "start": "00:00", "end": "24:00", "opens": ["maps"]}]

    def put_windows(self, who, cid, rules, etag=None, **kw):
        if etag is None:
            etag = self.e.req("GET", BASE + cid + "/windows", who=who).headers["ETag"]
        return self.e.req("PUT", BASE + cid + "/windows", who=who, json={"rules": rules}, headers={"If-Match": etag}, **kw)


class Roles(P):
    def test_unauthenticated_is_401_everywhere(self):
        for m, p in (("GET", "children"), ("GET", "children/child_a"), ("GET", "health"), ("GET", "filter-lists")):
            r = self.e.req(m, "/v1/parental/" + p, token="", validate=False)
            self.assertEqual(r.status_code, 401, p)

    def test_a_person_who_is_not_a_parent_gets_403_on_every_operation(self):
        e = self.e
        ops = [("GET", "children"), ("GET", "children/child_a"), ("GET", "children/child_a/windows"), ("GET", "children/child_a/exceptions"), ("GET", "children/child_a/activity"),
               ("GET", "children/child_a/domains"), ("GET", "health"), ("GET", "filter-lists"), ("GET", "filter-lists/catalogue")]
        for who in ("ola", "bob", "gus"):                         # readonly non-parent, household non-parent, ADMIN non-parent
            for m, p in ops:
                r = e.req(m, "/v1/parental/" + p, who=who)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"), (who, p))
            r = e.req("POST", BASE + "child_a/exceptions", who=who, json={"target": "youtube", "minutes": 30})
            self.assertEqual(r.status_code, 403, who)
            r = e.req("POST", BASE + "child_a/domains", who=who, json={"kind": "allow", "domain": "x.example.org"})
            self.assertEqual(r.status_code, 403, who)

    def test_a_readonly_parent_sees_status_only(self):
        e = self.e
        self.assertEqual(e.req("GET", "/v1/parental/children", who="rita").status_code, 200)
        d = e.req("GET", BASE + A, who="rita").json()
        self.assertEqual([k for k, v in d["can"].items() if v], [])
        self.assertEqual(e.req("GET", "/v1/parental/health", who="rita").status_code, 200)
        for m, p in (("GET", "children/child_a/activity"), ("GET", "children/child_a/windows"), ("GET", "children/child_a/domains"), ("GET", "children/child_a/exceptions"), ("GET", "filter-lists")):
            self.assertEqual(e.req(m, "/v1/parental/" + p, who="rita").status_code, 403, p)
        self.assertEqual(e.req("POST", BASE + A + "/exceptions", who="rita", json={"target": "youtube", "minutes": 30}).status_code, 403)

    def test_household_parent_works_but_filter_lists_are_admin_only(self):
        e = self.e
        for p in ("children", "children/child_a", "children/child_a/windows", "children/child_a/exceptions", "children/child_a/domains", "children/child_a/activity", "health"):
            self.assertEqual(e.req("GET", "/v1/parental/" + p, who="dasha").status_code, 200, p)
        for m, p, kw in (("GET", "filter-lists", {}), ("GET", "filter-lists/catalogue", {}), ("POST", "filter-lists", {"json": {"catalogue_id": "oisd_basic"}}),
                         ("PATCH", "filter-lists/101", {"json": {"enabled": False}}), ("DELETE", "filter-lists/101", {}), ("POST", "filter-lists/101/refresh", {}),
                         ("POST", "filter-lists/101/revert", {})):
            r = e.req(m, "/v1/parental/" + p, who="dasha", **kw)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"), p)


class Children(P):
    def test_list_and_detail_carry_identity_status_and_what_the_caller_may_do(self):
        e = self.e
        e.agh.auto_clients = [{"ip": "192.168.31.60", "name": "a-phone.lan", "source": "ARP"}]
        e.agh.querylog = [entry(5, "192.168.31.60", "youtube.com"), entry(6, "192.168.31.60", "ads.example.org", blocked=True), entry(7, "192.168.31.61", "other.example.org")]
        js = e.req("GET", "/v1/parental/children", who="ivan").json()
        by = {c["id"]: c for c in js["children"]}
        self.assertEqual(sorted(by), [A, B])
        a, b = by[A], by[B]
        self.assertEqual(a["identity"], {"ip": "192.168.31.60", "mac": "20:36:d0:14:8d:5a", "vendor": "Test Vendor Inc", "randomised_mac": False, "hostname": "a-phone.lan"})
        self.assertTrue(b["identity"]["randomised_mac"] and b["identity"]["vendor"] is None and b["identity"]["hostname"] is None)
        self.assertEqual((a["device_kind"], b["device_kind"]), ("phone", "tablet"))
        self.assertEqual((a["status"]["queries_today"], a["status"]["blocked_today"], b["status"]["queries_today"]), (2, 1, 1))
        self.assertTrue(a["status"]["dns_seen"])
        self.assertIn(a["status"]["state"], ("open", "closed", "restricted"))
        d = e.req("GET", BASE + A, who="ivan").json()
        self.assertEqual(self.byid(d["windows"]["rules"]), self.byid(self.rules()))
        self.assertTrue(d["can"]["edit_windows"] and d["can"]["edit_filter_lists"] and not d["can"]["edit_guardrails"])
        self.assertIn("Mi WiFi", d["note"])
        dd = e.req("GET", BASE + A, who="dasha").json()
        self.assertTrue(dd["can"]["edit_windows"] and not dd["can"]["edit_filter_lists"])
        self.assertEqual(e.req("GET", BASE + "nope", who="ivan").status_code, 404)

    def test_adguard_down_degrades_the_status_but_the_list_still_answers(self):
        e = self.e
        e.agh.down = True
        js = e.req("GET", "/v1/parental/children", who="ivan").json()
        c = js["children"][0]
        self.assertEqual((c["status"]["queries_today"], c["status"]["policy_ok"], c["status"]["dns_seen"]), (None, False, False))


class Windows(P):
    def test_get_put_with_if_match_and_the_rules_round_trip(self):
        e = self.e
        r = e.req("GET", BASE + A + "/windows", who="dasha")
        self.assertEqual((r.status_code, self.byid(r.json()["rules"]), r.json()["guardrails"]["allowed_from"], r.json()["guardrails"]["proposed"], r.json()["can_edit"]), (200, self.byid(self.rules()), "08:00", False, True))
        etag = r.headers["ETag"]
        r = self.put_windows("dasha", A, self.rules("16:00", "18:00"), etag)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["changed"])
        self.assertNotEqual(r.headers["ETag"], etag)
        self.assertEqual(self.yt(r.json()["rules"])["end"], "18:00")
        self.assertEqual(self.yt(e.sched.store.schedule(A)["rules"])["end"], "18:00")
        row = e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["target"]), ("parental_windows_put", "dasha", A))
        r = self.put_windows("dasha", A, self.rules("16:00", "18:00"), etag)                 # the old ETag: someone changed it meanwhile
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (412, "version_conflict"))
        r = e.req("PUT", BASE + A + "/windows", who="dasha", json={"rules": self.rules()})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (428, "precondition_required"))

    def test_guardrails_reject_with_the_reasons_and_never_clamp(self):
        e = self.e
        before = e.sched.store.schedule(A)["rules"]
        r = self.put_windows("dasha", A, self.rules("07:00", "22:00"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))
        codes = {p["code"] for p in r.json()["error"]["details"]["problems"]}
        self.assertTrue(codes & {"allowed_range", "max_open"}, codes)
        self.assertEqual(e.sched.store.schedule(A)["rules"], before)                          # nothing was stored, nothing was clamped
        self.assertFalse(e.db.audit_rows(1)[0]["ok"])

    def test_the_policy_invariants_are_not_editable_through_the_rules_shape(self):
        for bad in ({"id": "x", "days": [1], "start": "16:00", "end": "17:00", "opens": ["telegram"]},                 # not a window target: the always-on baseline is not a window
                    {"id": "x", "days": [1], "start": "16:00", "end": "17:00", "opens": []},
                    {"id": "x", "days": [1], "start": "16:00", "end": "17:00", "opens": ["youtube"], "level": 3},
                    {"id": "x", "days": [1], "start": "16:00", "end": "17:00"}):
            r = self.put_windows("ivan", A, [bad])
            self.assertEqual(r.status_code, 400, bad)
        r = self.e.req("PUT", BASE + A + "/windows", who="ivan", json={"rules": [], "safe_search": False}, headers={"If-Match": self.etag_a})
        self.assertEqual(r.status_code, 400)

    def test_preview_changes_nothing_and_says_what_would_be_open(self):
        e = self.e
        v = e.sched.store.schedule(A)["version"]
        r = e.req("POST", BASE + A + "/windows/preview", who="dasha", json={"rules": self.rules("17:00", "18:00")})
        js = r.json()
        self.assertEqual((r.status_code, js["valid"], js["problems"]), (200, True, []))
        self.assertTrue(js["open_next_7_days"] and all(x["opens"] for x in js["open_next_7_days"]))
        self.assertIn("changed", js["diff"])
        self.assertEqual(e.sched.store.schedule(A)["version"], v)
        r = e.req("POST", BASE + A + "/windows/preview", who="dasha", json={"rules": self.rules("07:00", "22:00")})
        self.assertEqual((r.status_code, r.json()["valid"]), (200, False))
        self.assertTrue(r.json()["problems"])

    def test_undo_reverts_the_last_change_as_a_new_one(self):
        e = self.e
        self.assertEqual(self.put_windows("ivan", A, self.rules("16:00", "17:30")).status_code, 200)
        r = e.req("POST", BASE + A + "/windows/undo", who="ivan")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.yt(r.json()["rules"])["end"], "19:00")
        self.assertEqual(e.db.audit_rows(1)[0]["action"], "parental_windows_undo")

    def test_a_device_that_is_not_household_editable_refuses_dasha_but_not_ivan(self):
        e = self.e
        e.sched.store.patch_entity(e.sched.sys, B, {"flags": {"household_editable": False}})
        r = self.put_windows("dasha", B, self.rules(), etag=self.etag_a)
        self.assertEqual(r.status_code, 403)
        d = e.req("GET", BASE + B, who="dasha").json()
        self.assertFalse(d["can"]["edit_windows"])
        self.assertTrue(e.req("GET", BASE + B, who="ivan").json()["can"]["edit_windows"])


class Exceptions(P):
    def test_grant_list_replace_and_cancel(self):
        e = self.e
        r = e.req("POST", BASE + A + "/exceptions", who="dasha", json={"target": "youtube", "minutes": 30})
        self.assertEqual(r.status_code, 201, r.text)
        x = r.json()
        self.assertEqual((x["target"], x["created_by"]), ("youtube", "dasha"))
        self.assertTrue(0 < x["remaining_s"] <= 1800)
        self.assertEqual([y["id"] for y in e.req("GET", BASE + A + "/exceptions", who="ivan").json()["exceptions"]], [x["id"]])
        d = e.req("GET", BASE + A, who="ivan").json()
        self.assertIn("youtube", d["status"]["open_services"])
        self.assertEqual(d["status"]["exceptions"][0]["id"], x["id"])
        r2 = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "youtube", "minutes": 60})
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(len(e.req("GET", BASE + A + "/exceptions", who="ivan").json()["exceptions"]), 1)             # replaced, not stacked
        self.assertEqual(e.req("DELETE", BASE + A + "/exceptions/" + r2.json()["id"], who="dasha").status_code, 204)
        self.assertEqual(e.req("GET", BASE + A + "/exceptions", who="ivan").json()["exceptions"], [])
        self.assertEqual(e.req("DELETE", BASE + A + "/exceptions/" + r2.json()["id"], who="ivan").status_code, 404)
        acts = [r["action"] for r in e.db.audit_rows(6)]
        self.assertIn("parental_exception_grant", acts)
        self.assertIn("parental_exception_cancel", acts)

    def test_guardrails_and_bad_bodies(self):
        e = self.e
        r = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "youtube", "minutes": 9999})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))
        for bad in ({"target": "tiktok", "minutes": 30}, {"target": "youtube", "minutes": 0}, {"target": "youtube", "minutes": "30"}, {"minutes": 30},
                    {"target": "youtube", "minutes": 30, "extra": 1}, {"target": "youtube", "until_end_of_day": "yes"}):
            self.assertEqual(e.req("POST", BASE + A + "/exceptions", who="ivan", json=bad).status_code, 400, bad)
        r = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "youtube"})                  # neither minutes nor end of day
        self.assertEqual(r.status_code, 422)

    def test_a_repeat_with_the_same_idempotency_key_is_the_same_exception(self):
        e = self.e
        h = {"Idempotency-Key": "same-key-1234567"}
        r1 = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "roblox", "minutes": 20}, headers=h)
        r2 = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "roblox", "minutes": 20}, headers=h)
        self.assertEqual((r1.status_code, r2.status_code, r1.json()["id"], r2.json()["id"], r2.json().get("replayed")), (201, 201, r1.json()["id"], r1.json()["id"], True))
        self.assertEqual(len(e.req("GET", BASE + A + "/exceptions", who="ivan").json()["exceptions"]), 1)


class Domains(P):
    def test_allow_deny_remove_and_undo(self):
        e = self.e
        r = e.req("POST", BASE + A + "/domains", who="dasha", json={"kind": "allow", "domain": "School.Example.org"})
        self.assertEqual((r.status_code, r.json()), (200, {"allow": ["school.example.org"], "deny": []}))
        r = e.req("POST", BASE + A + "/domains", who="dasha", json={"kind": "deny", "domain": "school.example.org"})          # deny beats allow: it MOVES the name
        self.assertEqual(r.json(), {"allow": [], "deny": ["school.example.org"]})
        self.assertEqual(e.req("GET", BASE + A + "/domains", who="ivan").json(), {"allow": [], "deny": ["school.example.org"]})
        self.assertEqual(e.req("GET", BASE + B + "/domains", who="ivan").json(), {"allow": [], "deny": []})                   # per child
        r = e.req("POST", BASE + A + "/domains/undo", who="dasha")
        self.assertEqual((r.status_code, r.json()), (200, {"allow": ["school.example.org"], "deny": []}))
        r = e.req("DELETE", BASE + A + "/domains?kind=allow&domain=school.example.org", who="ivan")
        self.assertEqual(r.json(), {"allow": [], "deny": []})
        self.assertEqual(e.req("DELETE", BASE + A + "/domains?kind=allow&domain=school.example.org", who="ivan").status_code, 404)
        acts = {r["action"] for r in e.db.audit_rows(8)}
        self.assertTrue({"parental_domain_add", "parental_domain_remove", "parental_domain_undo"} <= acts)

    def test_an_always_on_host_cannot_be_blocked_and_bad_input_is_refused(self):
        e = self.e
        r = e.req("POST", BASE + A + "/domains", who="ivan", json={"kind": "deny", "domain": "api.telegram.org"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))
        for bad in ({"kind": "maybe", "domain": "x.example.org"}, {"kind": "allow"}, {"kind": "allow", "domain": 5}, {"kind": "allow", "domain": "x.example.org", "z": 1}):
            self.assertEqual(e.req("POST", BASE + A + "/domains", who="ivan", json=bad).status_code, 400, bad)
        for bad in ("not a domain!", "http://x.example.org/path", "*.example.org"):
            self.assertEqual(e.req("POST", BASE + A + "/domains", who="ivan", json={"kind": "allow", "domain": bad}).status_code, 422, bad)
        self.assertEqual(e.req("DELETE", BASE + A + "/domains?kind=zzz&domain=x.example.org", who="ivan").status_code, 400)
        self.assertEqual(e.req("DELETE", BASE + A + "/domains?kind=allow", who="ivan").status_code, 400)


class Activity(P):
    def setUp(self):
        super().setUp()
        e = self.e
        e.agh.add_list("AdGuard DNS filter", "https://l.example/ads.txt", ["ads.example.org"])
        e.agh.add_list("Malicious URL Blocklist (URLHaus)", "https://l.example/urlhaus.txt", ["evil.example.net"])
        log = []
        for i in range(3):
            log += [entry(10 + i, "192.168.31.60", "youtube.com"), entry(10 + i, "192.168.31.60", "youtube.com", typ="AAAA")]
        log += [entry(20, "192.168.31.60", "ads.example.org", blocked=True), entry(21, "192.168.31.60", "ads.example.org", blocked=True),
                entry(30, "192.168.31.60", "evil.example.net", blocked=True), entry(40, "192.168.31.61", "other-child.example.org"),
                entry(60 * 24 * 3, "192.168.31.60", "old.example.com")]
        e.agh.querylog = sorted(log, key=lambda x: x["time"], reverse=True)

    def act(self, query="", who="ivan"):
        return self.e.req("GET", BASE + A + "/activity" + query, who=who)

    def test_aggregation_counts_a_queries_only_and_only_this_child(self):
        r = self.act()
        self.assertEqual(r.status_code, 200, r.text)
        rows = {x["name"]: x for x in r.json()["rows"]}
        self.assertEqual(sorted(rows), ["ads.example.org", "evil.example.net", "youtube.com"])
        self.assertEqual((rows["youtube.com"]["allowed"], rows["youtube.com"]["blocked"]), (3, 0))                 # the AAAA twins are not counted
        self.assertEqual((rows["ads.example.org"]["allowed"], rows["ads.example.org"]["blocked"]), (0, 2))
        self.assertEqual(rows["ads.example.org"]["listed_by"], "AdGuard DNS filter")
        self.assertFalse(rows["ads.example.org"]["threat"])
        self.assertTrue(rows["evil.example.net"]["threat"])
        self.assertIsNone(rows["youtube.com"]["listed_by"])
        self.assertIn("не страницы", r.json()["note"])
        self.assertEqual(r.json()["retention"], {"server": "7 дней", "replica": "6 часов, только в памяти"})

    def test_range_blocked_and_search_filters(self):
        self.assertIn("old.example.com", {x["name"] for x in self.act("?range=7d").json()["rows"]})
        self.assertNotIn("old.example.com", {x["name"] for x in self.act("?range=24h").json()["rows"]})
        self.assertEqual({x["name"] for x in self.act("?blocked=1").json()["rows"]}, {"ads.example.org", "evil.example.net"})
        self.assertEqual({x["name"] for x in self.act("?blocked=0").json()["rows"]}, {"youtube.com"})
        self.assertEqual({x["name"] for x in self.act("?q=YOUTUBE").json()["rows"]}, {"youtube.com"})
        for bad in ("?range=30d", "?blocked=2", "?cursor=zzz", "?q=" + "x" * 81):
            self.assertEqual(self.act(bad).status_code, 400, bad)

    def test_in_allow_and_in_deny_follow_the_childs_lists(self):
        e = self.e
        e.sched.store.change_child_domain(e.sched.sys, A, "allow", "youtube.com", True)
        e.sched.store.change_child_domain(e.sched.sys, A, "deny", "example.org", True)
        e.o["parental"]._cache.clear()
        rows = {x["name"]: x for x in self.act().json()["rows"]}
        self.assertTrue(rows["youtube.com"]["in_allow"] and not rows["youtube.com"]["in_deny"])
        self.assertTrue(rows["ads.example.org"]["in_deny"])                                                          # a subdomain of a denied name

    def test_cursor_pages_a_long_list_and_ends(self):
        e = self.e
        e.agh.querylog = sorted([entry(1 + i / 10.0, "192.168.31.60", "n%03d.example.org" % i) for i in range(230)], key=lambda x: x["time"], reverse=True)
        e.o["parental"]._cache.clear()
        seen, cur, pages = [], None, 0
        while True:
            js = self.act("?cursor=" + cur if cur else "").json()
            pages += 1
            seen += [x["name"] for x in js["rows"]]
            cur = js["next_cursor"]
            if not cur:
                break
        self.assertEqual((pages, len(seen), len(set(seen))), (3, 230, 230))

    def test_nothing_is_stored_by_the_gateway(self):
        e = self.e
        self.assertEqual(self.act().status_code, 200)
        blob = b""
        for suffix in ("", "-wal"):
            p = os.path.join(e.data, "gateway.db" + suffix)
            if os.path.exists(p):
                blob += open(p, "rb").read()
        blob += open(e.db.jsonl, "rb").read()
        for name in (b"youtube.com", b"ads.example.org", b"evil.example.net"):
            self.assertNotIn(name, blob)

    def test_adguard_down_is_503_and_unknown_child_404(self):
        e = self.e
        e.agh.down = True
        r = self.act()
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "adguard_unavailable"))
        e.agh.down = False
        self.assertEqual(e.req("GET", BASE + "nope/activity", who="ivan").status_code, 404)

    def test_the_adguard_password_never_leaves_the_gateway(self):
        e = self.e
        from fakeagh import AGH_PASSWORD
        self.assertTrue(e.agh.password_file)
        for r in (self.act(), e.req("GET", "/v1/parental/health", who="ivan"), e.req("GET", "/v1/parental/children", who="ivan")):
            self.assertNotIn(AGH_PASSWORD, r.text)
        e.agh.down = True
        self.assertNotIn(AGH_PASSWORD, self.act().text)
        e.agh.down = False
        self.assertNotIn(AGH_PASSWORD.encode(), open(e.db.jsonl, "rb").read())


class Health(P):
    def render(self, overrides=()):
        import httpx
        sd = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com"], "roblox": ["roblox.com"]}
        r = httpx.post(self.e.sched.url + "/v1/child-policy/render", headers={"Authorization": "Bearer " + self.e.sched.admin_secret},
                       json={"service_domains": sd, "at": dt.datetime.now(TZ).isoformat(), "overrides": list(overrides)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["lines"]

    def apply_status(self, **kw):
        with open(os.path.join(self.e.sched.dir, "child-apply-status.json"), "w") as f:
            json.dump({"at": dt.datetime.now(TZ).isoformat(timespec="seconds"), "ok": True, "source": "snapshot", "rules_hash": "abc", **kw}, f)

    def test_in_sync_when_the_block_in_adguard_is_the_expected_one(self):
        e = self.e
        lines = self.render()
        e.agh.user_rules = ["||ads.example.net^"] + lines + ["! user rule after"]
        self.apply_status()
        e.o["parental"]._cache.clear()
        js = e.req("GET", "/v1/parental/health", who="ivan").json()
        self.assertTrue(js["policy"]["in_sync"], js)
        self.assertEqual(js["policy"]["expected_hash"], js["policy"]["applied_hash"])
        self.assertEqual((js["status"], js["policy"]["source"], js["policy"]["last_apply_ok"]), ("ok" if js["replica"]["parity"] == "ok" else "degraded", "snapshot", True))
        self.assertIsNotNone(js["snapshot"]["checksum"])
        kids = {c["id"]: c for c in e.req("GET", "/v1/parental/children", who="ivan").json()["children"]}
        self.assertTrue(kids[A]["status"]["policy_ok"] and kids[B]["status"]["policy_ok"])

    def test_drift_is_visible_per_child(self):
        e = self.e
        lines = self.render()
        broken = [x for x in lines if not (x.startswith("@@||t.me^") and x.endswith("192.168.31.60"))]                  # one child lost one allow rule
        self.assertNotEqual(broken, lines)
        e.agh.user_rules = broken
        e.o["parental"]._cache.clear()
        js = e.req("GET", "/v1/parental/health", who="dasha").json()
        self.assertFalse(js["policy"]["in_sync"])
        self.assertNotEqual(js["policy"]["expected_hash"], js["policy"]["applied_hash"])
        self.assertEqual(js["status"], "degraded")
        kids = {c["id"]: c for c in e.req("GET", "/v1/parental/children", who="dasha").json()["children"]}
        self.assertFalse(kids[A]["status"]["policy_ok"])
        self.assertTrue(kids[B]["status"]["policy_ok"])
        e.agh.user_rules = []                                                                                           # the managed block is missing altogether
        e.o["parental"]._cache.clear()
        js = e.req("GET", "/v1/parental/health", who="ivan").json()
        self.assertEqual((js["policy"]["in_sync"], js["policy"]["applied_hash"]), (False, None))

    def test_an_active_exception_is_part_of_what_is_expected(self):
        # target "all" always changes the block (a window of youtube may be open at the real time of day the suite runs)
        e = self.e
        x = e.req("POST", BASE + A + "/exceptions", who="ivan", json={"target": "all", "minutes": 30}).json()
        e.agh.user_rules = self.render()                                                                                # the job has NOT applied the exception yet
        e.o["parental"]._cache.clear()
        self.assertFalse(e.req("GET", "/v1/parental/health", who="ivan").json()["policy"]["in_sync"])
        e.agh.user_rules = self.render([{"client": "192.168.31.60", "target": "all"}])                              # now it has
        e.o["parental"]._cache.clear()
        self.assertTrue(e.req("GET", "/v1/parental/health", who="ivan").json()["policy"]["in_sync"])
        e.req("DELETE", BASE + A + "/exceptions/" + x["id"], who="ivan")

    def test_parity_states_and_adguard_down(self):
        e = self.e
        e.agh.user_rules = self.render()
        e.parity_streak = {}
        e._write_parity(__import__("time").time())
        e.o["parental"]._cache.clear()
        self.assertEqual(e.req("GET", "/v1/parental/health", who="ivan").json()["replica"]["parity"], "ok")
        e.parity_streak = {"rules": 2}
        e._write_parity(__import__("time").time())
        js = e.req("GET", "/v1/parental/health", who="ivan").json()
        self.assertEqual((js["replica"]["parity"], js["status"]), ("mismatch", "degraded"))
        e.parity_streak = {}
        e._write_parity(__import__("time").time() - 3600)                                                               # the checker stopped running
        self.assertEqual(e.req("GET", "/v1/parental/health", who="ivan").json()["replica"]["parity"], "unknown")
        e.agh.down = True
        e.o["parental"]._cache.clear()
        js = e.req("GET", "/v1/parental/health", who="ivan").json()
        self.assertEqual((js["agh"]["reachable"], js["status"], js["policy"]["in_sync"]), (False, "degraded", False))
        e.agh.down = False
        e._write_parity(__import__("time").time())

    def test_list_freshness(self):
        e = self.e
        e.agh.add_list("Old list", "https://l.example/old.txt", ["a.example.org"])
        e.agh.filters[0]["last_updated"] = "2026-01-01T00:00:00Z"
        e.o["parental"]._cache.clear()
        js = e.req("GET", "/v1/parental/health", who="ivan").json()
        self.assertEqual((js["lists"]["count"], js["lists"]["stale"]), (1, True))


if __name__ == "__main__":
    unittest.main()
