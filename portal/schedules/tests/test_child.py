import copy
import datetime as dt
import os
import sys
import unittest
from sched import child_render, model
from tests.helpers import Env, SYSTEM

MON_SAT = [1, 2, 3, 4, 5, 6]
RULES = [{"id": "yt_roblox", "days": MON_SAT, "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}},
         {"id": "maps", "days": MON_SAT, "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}}]
PAYLOAD = {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "20:36:D0:14:8D:5A", "rules": RULES},
                       {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": RULES}],
           "lists": {"allow_always": ["example.org", "time.example.net"], "allow_weekdays": ["maps.example.com"], "allow_extra": [], "deny_extra": ["bad.example.org"]},
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
        self.assertEqual(g["lists"]["deny_extra"], ["bad.example.org"])
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

    def test_it_is_read_only_in_step_a(self):
        self.put(PAYLOAD)
        g = self.e.req("GET", "/v1/schedules/child_a")
        self.assertEqual(self.e.req("PUT", "/v1/schedules/child_a", headers={"If-Match": g.headers["ETag"]}, json={"rules": RULES[:1]}).status_code, 403)
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
        self.assertEqual(model.validate_rules(child, RULES)[1], [])     # yt window inside the all-day maps window: different targets, no overlap
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
        self.assertIn("maps.example.com", self.allowed(at(5, 23, 59)))                # Saturday evening still Maps
        self.assertNotIn("maps.example.com", self.allowed(at(6, 0, 0)))               # Sunday: Maps dropped
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
                    for ov in ([], [{"client": clients[0][0], "target": "all"}], [{"client": clients[1][0], "target": "maps"}, {"client": clients[1][0], "target": "roblox"}]):
                        self.assertEqual(cp.render_block(Stub(), clients, now, ov), child_render.render_block(pol, SD, now, ov), (now, ov))
                        n += 1
            self.assertGreater(n, 900)
        finally:
            cp.read_list, cp.extra_list = orig


if __name__ == "__main__":
    unittest.main()
