import copy
import json
import datetime as dt
import os
import sys
import unittest
from sched import child_render, model
from tests.helpers import Env, SYSTEM

MON_SAT = [1, 2, 3, 4, 5, 6]
RULES = [{"id": "yt_roblox", "days": MON_SAT, "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}]
from sched.child_invariants import REQUIRED_ALWAYS  # noqa: E402
PAYLOAD = {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "20:36:D0:14:8D:5A", "rules": RULES},
                       {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": RULES}],
           "lists": {"allow_always": sorted(REQUIRED_ALWAYS) + ["example.org", "time.example.net", "maps.example.com"], "allow_weekdays": ["weekday.example.com"], "allow_extra": [], "deny_extra": ["bad.example.org"]},
           "always_services": ["telegram"]}
SD = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com", "ytimg.com"], "roblox": ["roblox.com"]}


def at(day, h, m=0):          # 2026-10-19 is a Monday
    return dt.datetime(2026, 10, 19 + day, h, m, tzinfo=model.TZ)


class Import(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def put(self, payload, role="admin"):
        return self.e.req("PUT", "/v1/child-policy/import", role=role, json=payload)

    def test_import_and_read_back(self):
        r = self.put(PAYLOAD)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["devices"], ["child_a", "child_b"])
        g = self.e.req("GET", "/v1/child-policy").json()
        self.assertEqual([d["ip"] for d in g["devices"]], ["192.168.31.60", "192.168.31.61"])
        self.assertEqual(g["devices"][0]["mac"], "20:36:d0:14:8d:5a")
        self.assertEqual(g["lists"]["deny_extra"], [])                                      # Task 29: the extra lists are per child now (the legacy house-wide ones are copied into each device)
        self.assertEqual(g["devices"][0]["deny_extra"], ["bad.example.org"])
        ents = {x["id"]: x for x in self.e.req("GET", "/v1/entities").json()["entities"]}
        self.assertEqual(ents["child_a"]["kind"], "child_window")
        self.assertFalse(ents["child_a"]["managed"])
        self.assertEqual(ents["child_a"]["meta"]["ip"], "192.168.31.60")

    def test_roles(self):
        self.assertEqual(self.put(PAYLOAD, "household").status_code, 403)
        self.assertEqual(self.put(PAYLOAD, "readonly").status_code, 403)
        self.put(PAYLOAD)
        self.assertNotIn("lists", self.e.req("GET", "/v1/child-policy", role="household").json())
        self.assertIn("lists", self.e.req("GET", "/v1/child-policy", role="readonly").json())
        self.assertEqual(self.e.req("POST", "/v1/child-policy/render", role="household", json={"service_domains": SD}).status_code, 403)

    def test_child_entities_are_editable_by_role_but_not_as_plugs(self):
        """Task 29: windows are edited through the schedule endpoints (guardrails apply); PATCH managed and generic skip_date exceptions are not for children."""
        self.put(PAYLOAD)
        g = self.e.req("GET", "/v1/schedules/child_a")
        self.assertEqual(self.e.req("PUT", "/v1/schedules/child_a", headers={"If-Match": g.headers["ETag"]}, json={"rules": RULES[:1]}).status_code, 200)
        self.assertEqual(self.e.req("PATCH", "/v1/entities/child_a", json={"managed": True}).status_code, 422)
        self.assertEqual(self.e.req("POST", "/v1/exceptions", json={"entity_id": "child_a", "kind": "skip_date", "date": "2099-01-01"}).status_code, 403)


    def test_child_entities_never_reach_the_rendered_ha_config(self):
        import json
        self.put(PAYLOAD)
        meta = json.load(open(os.path.join(self.e.store.out, "render.json")))
        self.assertFalse([f for f in meta["files"] if "child" in f])

    def test_validation(self):
        bad = copy.deepcopy(PAYLOAD)
        bad["lists"]["allow_always"] = ["not a domain", "x$modifier.com"]
        self.assertEqual(self.put(bad).status_code, 422)
        bad = copy.deepcopy(PAYLOAD)
        bad["devices"][0]["rules"][0]["variables"] = {"opens": "tiktok"}
        self.assertEqual(self.put(bad).status_code, 422)
        bad = copy.deepcopy(PAYLOAD)
        bad["devices"][0]["ip"] = "300.1.1"
        self.assertEqual(self.put(bad).status_code, 422)
        bad = copy.deepcopy(PAYLOAD)
        bad["devices"][1]["id"] = "child_a"
        self.assertEqual(self.put(bad).status_code, 422)
        bad = copy.deepcopy(PAYLOAD)
        bad["devices"][0]["id"] = "plant_rack1"
        self.assertEqual(self.put(bad).status_code, 422)             # may not clobber a non-child entity
        self.assertEqual(self.put({"devices": []}).status_code, 422)
        self.assertEqual(self.e.req("GET", "/v1/child-policy").json()["devices"], [])    # nothing was stored by the failed attempts

    def test_end_of_day_only_for_child_windows_and_overlap_is_per_target(self):
        plug = {"id": "p", "kind": "plug", "guardrails": {"max_on_minutes": 1440, "allowed_from": "00:00", "allowed_to": "23:59"}}
        _n, p = model.validate_rules(plug, [{"id": "a", "days": [1], "start": "05:00", "end": "24:00"}])
        self.assertTrue(p)                                         # 24:00 is rejected for a plug
        child = {"id": "c", "kind": "child_window", "guardrails": {}}
        self.assertEqual(model.validate_rules(child, RULES)[1], [])     # one window: no overlap
        clash = RULES + [{"id": "yt2", "days": [1], "start": "18:00", "end": "20:00", "variables": {"opens": "youtube"}}]
        self.assertEqual([x["code"] for x in model.validate_rules(child, clash)[1]], ["overlap"])

    def test_reimport_replaces_and_is_audited(self):
        self.put(PAYLOAD)
        p2 = copy.deepcopy(PAYLOAD)
        p2["devices"] = p2["devices"][:1]
        self.put(p2)
        self.assertEqual([d["id"] for d in self.e.req("GET", "/v1/child-policy").json()["devices"]], ["child_a"])
        a = self.e.req("GET", "/v1/audit?limit=1").json()["audit"][0]
        self.assertEqual((a["action"], a["before"]["devices"], a["after"]["devices"]), ("import_child_policy", ["child_a", "child_b"], ["child_a"]))


class Render(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.req("PUT", "/v1/child-policy/import", json=PAYLOAD)
        self.pol = self.e.store.child_policy()

    def lines(self, now, ov=()):
        return child_render.render_block(self.pol, SD, now, ov)

    def allowed(self, now, ip="192.168.31.60", ov=()):
        return {l.split("@@||")[1].split("^")[0] for l in self.lines(now, ov) if l.startswith("@@||") and l.endswith("$client=" + ip)}

    def test_window_and_sunday(self):
        self.assertNotIn("youtube.com", self.allowed(at(0, 15, 59)))
        self.assertIn("youtube.com", self.allowed(at(0, 16, 0)))
        self.assertIn("roblox.com", self.allowed(at(0, 18, 59)))
        self.assertNotIn("youtube.com", self.allowed(at(0, 19, 0)))
        self.assertNotIn("youtube.com", self.allowed(at(6, 17, 0)))                  # Sunday: no window
        for day in range(7):                                                           # Maps is always open: every day, every hour, Sunday included
            for h in (0, 8, 12, 23):
                self.assertIn("maps.example.com", self.allowed(at(day, h)), (day, h))
        self.assertIn("weekday.example.com", self.allowed(at(5, 23, 59)))             # the weekday mechanism still exists: Monday-Saturday ...
        self.assertNotIn("weekday.example.com", self.allowed(at(6, 0, 0)))            # ... and not on Sunday
        self.assertIn("t.me", self.allowed(at(6, 12)))                                # Telegram every day

    def test_overrides_and_deny(self):
        ov = [{"client": "192.168.31.60", "target": "youtube"}]
        self.assertIn("youtube.com", self.allowed(at(6, 12), ov=ov))
        self.assertNotIn("youtube.com", self.allowed(at(6, 12), ip="192.168.31.61", ov=ov))
        everything = self.lines(at(0, 12), [{"client": "192.168.31.60", "target": "all"}])
        self.assertNotIn("||*^$client=192.168.31.60", everything)
        self.assertIn("||bad.example.org^$client=192.168.31.60", everything)         # block-forever survives "open everything"
        self.assertIn("||*^$client=192.168.31.61", everything)

    def test_api_render_matches_the_function(self):
        r = self.e.req("POST", "/v1/child-policy/render", json={"at": "2026-10-19T17:00:00+02:00", "service_domains": SD})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["lines"], self.lines(at(0, 17)))
        self.assertEqual(self.e.req("POST", "/v1/child-policy/render", json={"at": "x", "service_domains": SD}).status_code, 400)
        self.assertEqual(self.e.req("POST", "/v1/child-policy/render", json={"service_domains": {"telegram": []}}).status_code, 400)


@unittest.skipUnless(os.path.exists("/srv/compose/adguard/childpolicy.py"), "needs the running policy code on this machine")
class AgainstTheRunningCode(unittest.TestCase):
    """Same inputs to adguard/childpolicy.py:render_block and to the portal renderer (stubbed service catalogue): identical lines."""
    def test_identical_over_the_week(self):
        sys.path.insert(0, "/srv/compose/adguard")
        import childpolicy as cp
        env = Env()
        payload = copy.deepcopy(PAYLOAD)
        payload["lists"] = {"allow_always": cp.read_list("allow-always.txt"), "allow_weekdays": cp.read_list("allow-weekdays.txt"),
                            "allow_extra": cp.extra_list("allow"), "deny_extra": ["bad.example.org"] + cp.extra_list("deny")}
        env.req("PUT", "/v1/child-policy/import", json=payload)
        pol = env.store.child_policy()

        class Stub:
            def call(self, method, path, body=None):
                return {"blocked_services": [{"id": k, "rules": ["||%s^" % d for d in v]} for k, v in SD.items()]}
        orig = cp.read_list, cp.extra_list
        cp.read_list = lambda name: payload["lists"]["allow_always"] if name == "allow-always.txt" else payload["lists"]["allow_weekdays"]
        cp.extra_list = lambda kind: payload["lists"]["allow_extra" if kind == "allow" else "deny_extra"]
        try:
            clients = [(d["ip"], d["name"]) for d in pol["devices"]]
            n = 0
            for day in range(7):
                for minute in range(0, 1440, 30):
                    now = dt.datetime(2026, 10, 19 + day, minute // 60, minute % 60, tzinfo=model.TZ)
                    for ov in ([], [{"client": clients[0][0], "target": "all"}], [{"client": clients[1][0], "target": "youtube"}, {"client": clients[1][0], "target": "roblox"}]):
                        self.assertEqual(cp.render_block(Stub(), clients, now, ov), child_render.render_block(pol, SD, now, ov), (now, ov))
                        n += 1
            self.assertGreater(n, 900)
        finally:
            cp.read_list, cp.extra_list = orig




class Devices(unittest.TestCase):
    """Task 29: the set of child devices after the cutover (the import is refused then): admin-only add/remove, and house infrastructure is never a child device."""
    def setUp(self):
        self.e = Env()
        self.assertEqual(self.e.req("PUT", "/v1/child-policy/import", json=PAYLOAD).status_code, 200)

    def add(self, body, role="admin"):
        return self.e.req("POST", "/v1/child", role=role, json=body)

    def test_add_with_standard_windows_and_the_snapshot_follows(self):
        r = self.add({"name": "Anna's phone", "ip": "192.168.31.70", "mac": "AA:BB:CC:00:11:22"})
        self.assertEqual(r.status_code, 201, r.text)
        d = r.json()
        self.assertEqual((d["id"], d["name"], d["ip"], d["mac"]), ("child_anna_s_phone", "Anna's phone", "192.168.31.70", "aa:bb:cc:00:11:22"))
        self.assertEqual(sorted(x["id"] for x in d["rules"]), ["yt_roblox"])
        self.assertFalse(d["flags"]["guardrails_proposed"])
        snap = self.e.req("GET", "/v1/child/snapshot").json()
        self.assertIn("192.168.31.70", [x["ip"] for x in snap["devices"]])
        self.assertEqual(self.e.req("GET", "/v1/child/child_anna_s_phone").status_code, 200)

    def test_like_copies_the_windows_of_another_device(self):
        g = self.e.req("GET", "/v1/schedules/child_a")
        self.assertEqual(self.e.req("PUT", "/v1/schedules/child_a", headers={"If-Match": g.headers["ETag"]}, json={"rules": RULES[:1]}).status_code, 200)
        d = self.add({"name": "Copy", "ip": "192.168.31.71", "like": "child_a"}).json()
        self.assertEqual([x["id"] for x in d["rules"]], ["yt_roblox"])
        self.assertEqual(self.add({"name": "Bad like", "ip": "192.168.31.72", "like": "nope"}).status_code, 422)

    def test_house_infrastructure_and_non_lan_addresses_are_refused(self):
        for ip in ("192.168.31.1", "192.168.31.2", "192.168.31.5", "8.8.8.8", "127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "999.1.1.1", "", "::1", "fe80::1", "192.168.31.60"):
            r = self.add({"name": "X device", "ip": ip})
            self.assertEqual(r.status_code, 422, ip)
        r = self.add({"name": "X", "ip": "192.168.31.2"})
        self.assertIn("whole house", json.dumps(r.json()))
        # the import and the snapshot verifier know the rule too (second enforcement)
        bad = copy.deepcopy(PAYLOAD)
        bad["devices"][0]["ip"] = "192.168.31.2"
        self.assertEqual(self.e.req("PUT", "/v1/child-policy/import", json=bad).status_code, 422)
        snap = self.e.req("GET", "/v1/child/snapshot").json()
        snap["devices"][0]["ip"] = "192.168.31.5"
        from sched import child_invariants
        self.assertTrue(any(p["code"] == "protected_ip" for p in child_invariants.verify_snapshot(snap)))

    def test_duplicates_roles_and_bad_bodies(self):
        self.assertEqual(self.add({"name": "A-phone", "ip": "192.168.31.80"}).status_code, 422)                  # the name exists (case-insensitive)
        self.assertEqual(self.add({"name": "Fresh", "ip": "192.168.31.60"}).status_code, 422)                    # the ip exists
        self.assertEqual(self.add({"name": "Fresh", "ip": "192.168.31.81", "mac": "20:36:d0:14:8d:5a"}).status_code, 422)   # the mac exists
        self.assertEqual(self.add({"name": "Fresh", "ip": "192.168.31.81", "mac": "nope"}).status_code, 422)
        self.assertEqual(self.add({"name": "!!!", "ip": "192.168.31.82"}).status_code, 422)
        self.assertEqual(self.add({"name": "Fresh", "ip": "192.168.31.83"}, role="household").status_code, 403)
        self.assertEqual(self.add({"name": "Fresh", "ip": "192.168.31.83"}, role="readonly").status_code, 403)
        for bad in ({}, {"name": "x"}, {"name": 1, "ip": "192.168.31.84"}, {"name": "x", "ip": "192.168.31.84", "extra": 1}):
            self.assertEqual(self.add(bad).status_code, 400, bad)

    def test_remove_keeps_a_record_and_never_the_last_device(self):
        self.e.req("POST", "/v1/child/child_b/domains", json={"kind": "allow", "domain": "school.example.org"})
        self.assertEqual(self.e.req("DELETE", "/v1/child/child_b", role="household").status_code, 403)
        self.assertEqual(self.e.req("DELETE", "/v1/child/nope").status_code, 404)
        rm0 = self.e.req("DELETE", "/v1/child/child_b")
        self.assertEqual(rm0.status_code, 200, rm0.text)
        self.assertEqual((rm0.json()["child_id"], rm0.json()["name"]), ("child_b", "b-tablet"))
        snap = self.e.req("GET", "/v1/child/snapshot").json()
        self.assertEqual([x["id"] for x in snap["devices"]], ["child_a"])
        self.assertEqual(self.e.req("GET", "/v1/child/child_b").status_code, 404)
        rows = self.e.req("GET", "/v1/audit?limit=5").json()["audit"]
        rm = next(r for r in rows if r["action"] == "child_device_remove")
        self.assertEqual(rm["before"]["domains"]["allow"], ["school.example.org"])                               # what is needed to re-create it by hand
        self.assertEqual(rm["before"]["rules"][0]["id"], "yt_roblox")
        r = self.e.req("DELETE", "/v1/child/child_a")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))                 # the last device
        self.assertIn("last child device", json.dumps(r.json()))



    def test_label_icon_closed_template_and_cyrillic_names(self):
        d = self.add({"name": "Настя", "label": "планшет", "ip": "192.168.31.90", "icon": "tablet", "closed": True}).json()
        self.assertEqual((d["id"], d["name"], d["label"], d["icon"], d["child"], d["rules"]), ("child_nastya_planshet", "Настя планшет", "планшет", "tablet", "Настя", []))
        snap = self.e.req("GET", "/v1/child/snapshot").json()
        mine = next(x for x in snap["devices"] if x["id"] == d["id"])
        self.assertEqual(mine["rules"], [])                                                  # closed: no window opens anything
        self.assertEqual(d["status"]["state"], "closed")
        self.assertEqual(self.add({"name": "Влад", "ip": "192.168.31.91", "icon": "fridge"}).status_code, 422)
        self.assertEqual(self.add({"name": "Влад", "ip": "192.168.31.91", "like": "child_a", "closed": True}).status_code, 422)     # a template XOR closed
        c = self.add({"name": "Copy", "label": "phone", "ip": "192.168.31.92", "like": "child_a"}).json()
        self.assertEqual(sorted(x["id"] for x in c["rules"]), ["yt_roblox"])

    def test_copy_also_copies_the_per_child_lists(self):
        self.e.req("POST", "/v1/child/child_a/domains", json={"kind": "allow", "domain": "school.example.org"})
        c = self.add({"name": "Copy", "ip": "192.168.31.93", "like": "child_a"}).json()
        self.assertEqual(c["domains"], {"allow": ["school.example.org"], "deny": ["bad.example.org"]})
        self.assertEqual(self.e.req("GET", "/v1/child/child_a").json()["domains"]["allow"], ["school.example.org"])

    def test_a_removal_can_be_undone_within_24_hours_with_everything(self):
        self.e.req("POST", "/v1/child/child_b/domains", json={"kind": "deny", "domain": "bad.example.org"})
        x = self.e.req("POST", "/v1/child/child_b/exceptions", json={"target": "youtube", "minutes": 30}).json()
        g = self.e.req("GET", "/v1/schedules/child_b")
        self.e.req("PUT", "/v1/schedules/child_b", headers={"If-Match": g.headers["ETag"]}, json={"rules": RULES[:1]})
        rm = self.e.req("DELETE", "/v1/child/child_b").json()
        listed = self.e.req("GET", "/v1/child/removed").json()["removed"]
        self.assertEqual([(r["removal_id"], r["child_id"], r["ip"]) for r in listed], [(rm["removal_id"], "child_b", "192.168.31.61")])
        self.assertEqual(self.e.req("GET", "/v1/child/removed", role="household").status_code, 403)
        self.assertEqual(self.e.req("POST", "/v1/child/removed/%s/restore" % rm["removal_id"], role="household").status_code, 403)
        r = self.e.req("POST", "/v1/child/removed/%s/restore" % rm["removal_id"])
        self.assertEqual(r.status_code, 201, r.text)
        d = r.json()
        self.assertEqual((d["id"], d["ip"], [x["id"] for x in d["rules"]], d["domains"]["deny"], [e["target"] for e in d["exceptions"]]), ("child_b", "192.168.31.61", ["yt_roblox"], ["bad.example.org"], ["youtube"]))
        self.assertEqual(self.e.req("GET", "/v1/child/removed").json()["removed"], [])
        self.assertEqual(self.e.req("POST", "/v1/child/removed/%s/restore" % rm["removal_id"]).status_code, 404)           # once

    def test_a_restore_is_refused_when_the_ip_was_taken_and_after_24_hours(self):
        rm = self.e.req("DELETE", "/v1/child/child_b").json()
        self.assertEqual(self.add({"name": "New", "ip": "192.168.31.61"}).status_code, 201)                                 # the freed IP goes to a new device
        r = self.e.req("POST", "/v1/child/removed/%s/restore" % rm["removal_id"])
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))
        import datetime as dt
        self.assertEqual(len(self.e.store.child_removed()), 1)
        later = self.e.store._now() + dt.timedelta(hours=25)
        self.assertEqual(self.e.store.child_removed(later), [])                                                             # dropped after 24 h


if __name__ == "__main__":
    unittest.main()
