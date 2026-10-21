"""Adding and removing a child device from the portal (Task 29b): the REAL schedule service, a fake AdGuard (real HTTP) and a miniature of the host job that renders the
service's policy with the real renderer. Role matrix, candidates, the verified add (and every way it is rolled back), the confirmed removal with client cleanup, the 24 h
undo, the checklist, one change at a time."""
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase  # noqa: E402
from fakeagh import FakeJob  # noqa: E402

BASE = "/v1/parental/children"
REAL = ["192.168.31.60", "192.168.31.61"]


def setUpModule():
    e = BaseCase.ENV = Env()
    a = {"person_id": "test", "phone_id": None, "ip": "test"}
    for name, role, parent in (("Bob", "household", False), ("Gus", "admin", False), ("Rita", "readonly", True)):
        e.people.create(a, name, role, parent)
        code, _ = e.people.create_enrolment_code(a, name.lower())
        e.tokens[name.lower()] = e.people.enrol(code, {"label": name + "-phone", "model": "Test"}, "10.0.0.1")["token"]
    e.job = FakeJob(e).start()
    e.arp = os.path.join(e.tmp, "arp")
    e.o["devices"].arp_file = e.arp
    e.o["devices"].apply_timeout_s, e.o["devices"].poll_s = 3.0, 0.05


def tearDownModule():
    BaseCase.ENV.job.stop()
    BaseCase.ENV.close()


def write_arp(rows):
    with open(BaseCase.ENV.arp, "w") as f:
        f.write("IP address       HW type     Flags       HW address            Mask     Device\n")
        for ip, mac in rows:
            f.write("%s 0x1 0x2 %s * eno1\n" % (ip, mac))


def entry(minutes_ago, client, name, typ="A", blocked=False):
    import datetime as dt
    t = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"time": t, "client": client, "question": {"name": name + ".", "type": typ}, "reason": "FilteredBlackList" if blocked else "NotFilteredNotFound"}


class D(BaseCase):
    def setUp(self):
        super().setUp()
        e = self.e
        e.agh.down = False
        e.job.enabled = True
        e.job.client_flags = {"use_global_settings": False, "safe_search": {"enabled": True, "google": True, "youtube": True}}
        e.parity_runs, e.parity_streak = True, {}
        fi = e.o["filters"]
        fi.parity_wait_s, fi.poll_s = 0.6, 0.02
        e.o["devices"]._busy = False
        e.o["devices"].apply_timeout_s = 3.0
        e.o["parental"]._cache.clear()
        e.agh.querylog = []
        e.agh.auto_clients = []
        write_arp([("192.168.31.1", "9c:9d:7e:74:f6:7d"), ("192.168.31.2", "aa:aa:aa:aa:aa:02"), ("192.168.31.5", "b8:27:eb:2b:87:17"), ("192.168.31.60", "20:36:d0:14:8d:5a"),
                   ("192.168.31.61", "04:34:f6:13:a3:97"), ("192.168.31.240", "d8:d6:68:1b:73:b8")])
        # the service starts every test with the two seeded children only
        for c in list(e.sched.store.child_devices()):
            if c["id"] not in ("child_a", "child_b"):
                e.sched.store.remove_child_device(e.sched.sys, c["id"], keep=False)
        e.sched.store.db.execute("DELETE FROM child_removed")
        e.agh.clients = []
        time.sleep(0.5)

    def add(self, body, who="ivan", **kw):
        return self.e.req("POST", BASE, who=who, json=body, **kw)

    def lines(self, ips):
        return [x for x in self.e.agh.user_rules if any(x.endswith("$client=" + i) for i in ips)]

    def names(self):
        return sorted(c["id"] for c in self.e.req("GET", BASE, who="ivan").json()["children"])

    def steps(self, r):
        return [(s["name"], s["ok"]) for s in r.json()["steps"]]


class Roles(D):
    def test_only_the_admin_who_is_a_parent_may_add_remove_restore_or_list_candidates(self):
        e = self.e
        ok = {"name": "Anna", "ip": "192.168.31.70"}
        for who in ("dasha", "rita", "ola", "bob", "gus"):
            for m, p, kw in (("GET", "/v1/parental/candidates", {}), ("POST", BASE, {"json": ok}), ("DELETE", BASE + "/child_a", {"json": {"confirm": "x"}}),
                             ("GET", "/v1/parental/removed-children", {}), ("POST", "/v1/parental/removed-children/r1/restore", {})):
                r = e.req(m, p, who=who, **kw)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden"), (who, m, p))
        self.assertEqual(e.req("GET", BASE + "/child_a/setup-checklist", who="dasha").status_code, 200)               # a parent may see the checklist
        for who in ("rita", "ola", "bob", "gus"):
            self.assertEqual(e.req("GET", BASE + "/child_a/setup-checklist", who=who).status_code, 403, who)
        self.assertEqual(self.names(), ["child_a", "child_b"])


class Candidates(D):
    def test_only_unknown_lan_devices_with_identity_and_queries_and_nothing_stored(self):
        e = self.e
        write_arp([("192.168.31.1", "9c:9d:7e:74:f6:7d"), ("192.168.31.2", "aa:aa:aa:aa:aa:02"), ("192.168.31.5", "b8:27:eb:2b:87:17"), ("192.168.31.60", "20:36:d0:14:8d:5a"),
                   ("192.168.31.61", "04:34:f6:13:a3:97"), ("192.168.31.240", "d8:d6:68:1b:73:b8"), ("192.168.31.70", "4a:11:22:33:44:55"), ("192.168.31.71", "20:36:d0:99:88:77"),
                   ("192.168.31.72", "00:00:00:00:00:00"), ("172.17.0.2", "02:42:ac:11:00:02"), ("10.0.0.9", "aa:aa:aa:aa:aa:09")])
        e.agh.auto_clients = [{"ip": "192.168.31.70", "name": "annas-secret-phone.lan", "source": "ARP"}]
        e.agh.querylog = [entry(3, "192.168.31.70", "privatesite.example.org"), entry(4, "192.168.31.70", "other.example.org"), entry(5, "192.168.31.71", "x.example.org")]
        js = e.req("GET", "/v1/parental/candidates", who="ivan").json()
        ips = [c["ip"] for c in js["candidates"]]
        self.assertEqual(ips, ["192.168.31.70", "192.168.31.71"])                                         # children, infrastructure, known gear, incomplete ARP, docker and other nets: out
        a, b = js["candidates"]
        self.assertEqual((a["randomised_mac"], a["warnings"], a["hostname"], a["queries_today"], a["vendor"]), (True, ["randomised_mac"], "annas-secret-phone.lan", 2, None))
        self.assertEqual((b["randomised_mac"], b["vendor"], b["queries_today"], b["warnings"]), (False, "Test Vendor Inc", 1, []))
        self.assertEqual(js["excluded"], {"children": 2, "infrastructure": 3, "known_devices": 11})
        blob = open(e.db.jsonl, "rb").read() + b"".join(open(os.path.join(e.data, "gateway.db" + s), "rb").read() for s in ("", "-wal") if os.path.exists(os.path.join(e.data, "gateway.db" + s)))
        for secret in (b"annas-secret-phone", b"privatesite.example.org"):
            self.assertNotIn(secret, blob)
        write_arp([("192.168.31.60", "20:36:d0:14:8d:5a")])
        self.assertEqual(e.req("GET", "/v1/parental/candidates", who="ivan").json()["candidates"], [])


class Add(D):
    def test_a_closed_device_is_created_verified_and_leaves_the_other_children_alone(self):
        e = self.e
        base = self.lines(REAL)
        write_arp([("192.168.31.60", "20:36:d0:14:8d:5a"), ("192.168.31.70", "6a:11:22:33:44:55")])
        r = self.add({"name": "Настя", "device_label": "телефон", "ip": "192.168.31.70", "mac": "6a:11:22:33:44:55", "icon": "phone", "template": "closed"})
        self.assertEqual(r.status_code, 200, r.text)
        js = r.json()
        self.assertEqual((js["result"], js["child_id"]), ("created", "child_nastya_telefon"))
        self.assertEqual(self.steps(r), [("create", True), ("job", True), ("verify", True), ("rules", True), ("parity", True)])
        self.assertEqual(sorted(w["code"] for w in js["warnings"]), ["randomised_mac", "static_unverifiable"])           # a warning, not a refusal
        kids = {c["id"]: c for c in e.req("GET", BASE, who="ivan").json()["children"]}
        mine = kids["child_nastya_telefon"]
        self.assertEqual((mine["name"], mine["device_kind"], mine["icon"], mine["status"]["state"]), ("Настя телефон", "phone", "phone", "closed"))
        d = e.req("GET", BASE + "/child_nastya_telefon", who="ivan").json()
        self.assertEqual(d["windows"]["rules"], [])                                                                      # closed: no window opens anything
        self.assertEqual(self.lines(REAL), base)                                                                         # the others: byte-identical
        self.assertTrue(any(x.endswith("$client=192.168.31.70") for x in e.agh.user_rules))
        self.assertEqual([c["name"] for c in e.agh.clients if "192.168.31.70" in c["ids"]], ["Настя телефон"])
        row = e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["ok"]), ("child_device_add", "ivan", True))

    def test_a_copy_template_takes_windows_and_lists(self):
        e = self.e
        e.sched.store.change_child_domain(e.sched.sys, "child_a", "allow", "school.example.org", True)
        r = self.add({"name": "Влад", "device_label": "планшет", "ip": "192.168.31.71", "icon": "tablet", "template": "copy:child_a"})
        self.assertEqual(r.json()["result"], "created", r.text)
        d = e.req("GET", BASE + "/" + r.json()["child_id"], who="dasha").json()
        self.assertEqual(sorted(x["id"] for x in d["windows"]["rules"]), ["maps", "yt_roblox"])
        self.assertEqual(d["domains"]["allow"], ["school.example.org"])

    def test_refusals_change_nothing(self):
        e = self.e
        for body, code in (({"name": "X", "ip": "192.168.31.2"}, "infrastructure_ip"), ({"name": "X", "ip": "192.168.31.1"}, "infrastructure_ip"), ({"name": "X", "ip": "192.168.31.5"}, "infrastructure_ip"),
                           ({"name": "X", "ip": "192.168.31.60"}, "duplicate"), ({"name": "X", "ip": "999.1.1.1"}, "bad_ip"), ({"name": "X", "ip": "not-an-ip"}, "bad_ip"),
                           ({"name": "X", "ip": "10.0.0.5"}, "not_lan"), ({"name": "X", "ip": "192.168.31.80", "template": "copy:nope"}, "bad_template"),
                           ({"name": "X", "ip": "192.168.31.80", "template": "weird"}, "bad_template")):
            r = self.add(body)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, code), body)
        for bad in ({}, {"name": "X"}, {"ip": "192.168.31.80"}, {"name": "X", "ip": "192.168.31.80", "icon": "fridge"}, {"name": "X", "ip": "192.168.31.80", "extra": 1}, {"name": 5, "ip": "192.168.31.80"}):
            self.assertEqual(self.add(bad).status_code, 400, bad)
        self.assertEqual(self.names(), ["child_a", "child_b"])
        self.assertEqual(e.sched.store.child_removed(), [])

    def test_warnings_for_a_mac_that_differs_an_address_nobody_answers_at_and_other_gear(self):
        write_arp([("192.168.31.72", "20:36:d0:00:00:01")])
        r = self.add({"name": "Amy", "ip": "192.168.31.72", "mac": "20:36:d0:00:00:02"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(w["code"] for w in r.json()["warnings"]), ["mac_differs", "static_unverifiable"])
        r = self.add({"name": "Bea", "ip": "192.168.31.73"})
        self.assertIn("not_seen", [w["code"] for w in r.json()["warnings"]])
        r = self.add({"name": "Cleo", "ip": "192.168.31.240"})
        self.assertIn("known_device", [w["code"] for w in r.json()["warnings"]])

    def test_a_job_that_does_not_apply_rolls_the_device_back_with_the_reason(self):
        e = self.e
        e.job.enabled = False
        e.o["devices"].apply_timeout_s = 0.8
        r = self.add({"name": "Slow", "ip": "192.168.31.74"})
        js = r.json()
        self.assertEqual((r.status_code, js["result"], js["child_id"]), (200, "reverted", None), r.text)
        self.assertEqual(self.steps(r)[:2], [("create", True), ("job", False)])
        self.assertIn("job failed", js["reason"])
        self.assertEqual(self.names(), ["child_a", "child_b"])                                                          # the entity is gone again
        self.assertEqual(e.sched.store.child_removed(), [])                                                             # and nothing is left to "undo"
        self.assertFalse(e.db.audit_rows(1)[0]["ok"])
        e.job.enabled = True

    def test_a_client_entry_without_the_child_flags_fails_the_verification(self):
        e = self.e
        e.job.client_flags = {"use_global_settings": True, "safe_search": {"enabled": False, "google": False, "youtube": False}}
        r = self.add({"name": "Flags", "ip": "192.168.31.75"})
        js = r.json()
        self.assertEqual(js["result"], "reverted")
        self.assertIn(("verify", False), self.steps(r))
        self.assertIn("child settings", js["reason"])
        self.assertEqual(self.names(), ["child_a", "child_b"])
        self.assertEqual([c for c in e.agh.clients if "192.168.31.75" in c["ids"]], [])                                  # the leftover client entry was deleted too

    def test_a_replica_that_does_not_agree_rolls_back(self):
        e = self.e
        e.parity_streak = {"rules": 3}
        r = self.add({"name": "Par", "ip": "192.168.31.76"})
        self.assertEqual(r.json()["result"], "reverted")
        self.assertIn(("parity", False), self.steps(r))
        self.assertEqual(self.names(), ["child_a", "child_b"])

    def test_the_same_idempotency_key_adds_once(self):
        e = self.e
        h = {"Idempotency-Key": "dev-add-key-123456"}
        r1 = self.add({"name": "Once", "ip": "192.168.31.77"}, headers=h)
        r2 = self.add({"name": "Once", "ip": "192.168.31.77"}, headers=h)
        self.assertEqual((r1.json()["result"], r2.json().get("replayed")), ("created", True))
        self.assertEqual(sum(1 for c in e.sched.store.child_devices() if c["meta"]["ip"] == "192.168.31.77"), 1)

    def test_only_one_change_at_a_time(self):
        e = self.e
        e.job.enabled = False
        e.o["devices"].apply_timeout_s = 1.5
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.add({"name": "Slow2", "ip": "192.168.31.78"})))
        t.start()
        time.sleep(0.5)
        r = self.add({"name": "Fast", "ip": "192.168.31.79"})
        t.join(10)
        e.job.enabled = True
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "device_busy"))
        self.assertEqual(out["r"].json()["result"], "reverted")


class Remove(D):
    def make(self, name="Дима", ip="192.168.31.81"):
        r = self.add({"name": name, "device_label": "телефон", "ip": ip, "template": "copy:child_a"})
        self.assertEqual(r.json()["result"], "created", r.text)
        return r.json()["child_id"], "%s телефон" % name

    def test_removal_needs_the_exact_name_and_cleans_everything_up(self):
        e = self.e
        cid, full = self.make()
        base = self.lines(REAL)
        for bad in ("", "Дима", "дима телефон"):
            r = e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": bad})
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "confirm_mismatch"), bad)
        self.assertIn(cid, self.names())
        self.assertEqual(e.req("DELETE", BASE + "/" + cid, who="ivan", json={}).status_code, 400)
        r = e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full})
        js = r.json()
        self.assertEqual((r.status_code, js["result"]), (200, "removed"), r.text)
        self.assertEqual([(s["name"], s["ok"]) for s in js["steps"]], [("remove", True), ("job", True), ("client", True), ("others", True), ("parity", True)])
        self.assertNotIn(cid, self.names())
        self.assertEqual([c for c in e.agh.clients if "192.168.31.81" in c["ids"]], [])                                  # the client entry is gone on the origin
        self.assertFalse(any(x.endswith("$client=192.168.31.81") for x in e.agh.user_rules))                             # the IP is an ordinary client again
        self.assertEqual(self.lines(REAL), base)
        self.assertEqual(e.agh.check("never-allowed.example.net", "192.168.31.81")["reason"], "NotFilteredNotFound")
        self.assertEqual([r["child_id"] for r in e.req("GET", "/v1/parental/removed-children", who="ivan").json()["removed"]], [cid])
        self.assertEqual(e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full}).status_code, 404)

    def test_the_removal_is_idempotent_with_the_same_key(self):
        cid, full = self.make("Идем", "192.168.31.82")
        h = {"Idempotency-Key": "dev-del-key-123456"}
        r1 = self.e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full}, headers=h)
        r2 = self.e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full}, headers=h)
        self.assertEqual((r1.json()["result"], r2.json().get("replayed")), ("removed", True))

    def test_undo_within_24_hours_restores_windows_lists_and_exceptions_and_is_verified(self):
        e = self.e
        cid, full = self.make("Вернём", "192.168.31.83")
        e.sched.store.change_child_domain(e.sched.sys, cid, "deny", "bad.example.org", True)
        e.req("POST", BASE + "/" + cid + "/exceptions", who="ivan", json={"target": "youtube", "minutes": 30})
        e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full})
        rid = e.req("GET", "/v1/parental/removed-children", who="ivan").json()["removed"][0]["removal_id"]
        r = e.req("POST", "/v1/parental/removed-children/%s/restore" % rid, who="ivan")
        js = r.json()
        self.assertEqual((r.status_code, js["result"], js["child_id"]), (200, "created", cid), r.text)
        self.assertEqual([s["name"] for s in js["steps"]], ["create", "job", "verify", "rules", "parity"])
        d = e.req("GET", BASE + "/" + cid, who="ivan").json()
        self.assertEqual((sorted(x["id"] for x in d["windows"]["rules"]), d["domains"]["deny"], [x["target"] for x in d["exceptions"]]), (["maps", "yt_roblox"], ["bad.example.org"], ["youtube"]))
        self.assertEqual(e.req("GET", "/v1/parental/removed-children", who="ivan").json()["removed"], [])
        self.assertEqual(e.req("POST", "/v1/parental/removed-children/%s/restore" % rid, who="ivan").status_code, 404)

    def test_a_restore_is_refused_when_the_address_was_taken_meanwhile(self):
        e = self.e
        cid, full = self.make("Занят", "192.168.31.84")
        e.req("DELETE", BASE + "/" + cid, who="ivan", json={"confirm": full})
        self.assertEqual(self.add({"name": "Другой", "ip": "192.168.31.84"}).json()["result"], "created")
        rid = e.req("GET", "/v1/parental/removed-children", who="ivan").json()["removed"][0]["removal_id"]
        r = e.req("POST", "/v1/parental/removed-children/%s/restore" % rid, who="ivan")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "validation_failed"))


class Checklist(D):
    def test_items_with_live_checks_and_plain_instructions(self):
        e = self.e
        e.agh.querylog = [entry(2, "192.168.31.60", "youtube.com"), entry(3, "192.168.31.60", "x.example.org")]
        js = e.req("GET", BASE + "/child_a/setup-checklist", who="ivan").json()
        by = {i["id"]: i for i in js["items"]}
        self.assertEqual(list(by), ["dhcp_reservation", "mac_randomisation", "static_dns", "private_dns_vpn", "bedtime"])
        self.assertEqual((by["dhcp_reservation"]["status"], by["mac_randomisation"]["status"], by["static_dns"]["status"]), ("unknown", "ok", "ok"))
        self.assertIn("сегодня 2", by["static_dns"]["detail"])
        self.assertEqual((by["private_dns_vpn"]["status"], by["bedtime"]["status"]), ("unknown", "info"))
        self.assertIn("Mi WiFi", by["bedtime"]["text"])
        self.assertIn("192.168.31.2", by["static_dns"]["text"])
        self.assertIn("192.168.31.5", by["static_dns"]["text"])
        write_arp([("192.168.31.61", "aa:bb:cc:dd:ee:ff")])                                                               # another device answers at the second child's address
        e.agh.querylog = []
        by = {i["id"]: i for i in e.req("GET", BASE + "/child_b/setup-checklist", who="ivan").json()["items"]}
        self.assertEqual((by["dhcp_reservation"]["status"], by["static_dns"]["status"]), ("warn", "todo"))
        self.assertEqual(e.req("GET", BASE + "/nope/setup-checklist", who="ivan").status_code, 404)


if __name__ == "__main__":
    unittest.main()
