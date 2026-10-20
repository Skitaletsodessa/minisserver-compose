"""AdGuard filter lists with the safety net (Task 29 Part 2): fake AdGuard (real HTTP, lists with CONTENT, so check_host blocks what the lists block), fake DNS for the
origin and the replica, a parity-state file. Proves: every change is validated, probed, followed by the replica and the parity checker, and ANY failure puts the
lists back to the snapshot and says why. Also the fetch guard (no private addresses), the rule counter and the DNS client."""
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from helpers import Env, BaseCase  # noqa: E402
from gw import agh as aghmod, filterlists as fl  # noqa: E402

FL = "/v1/parental/filter-lists"
BASEURL = "https://l.example/base.txt"
SENTINEL = "doubleclick.net"


def setUpModule():
    BaseCase.ENV = Env()
    a = {"person_id": "test", "phone_id": None, "ip": "test"}
    e = BaseCase.ENV
    for name, role, parent in (("Gus", "admin", False),):
        e.people.create(a, name, role, parent)
        code, _ = e.people.create_enrolment_code(a, name.lower())
        e.tokens[name.lower()] = e.people.enrol(code, {"label": name + "-phone", "model": "Test"}, "10.0.0.1")["token"]


def tearDownModule():
    BaseCase.ENV.close()


def text(domains):
    return "! test list\n" + "\n".join("||%s^" % d for d in domains) + "\n"


class F(BaseCase):
    def setUp(self):
        super().setUp()
        e = self.e
        e.agh.down = False
        e.agh.filters, e.agh.contents, e.agh.active, e.agh.reject_add, e.agh.calls = [], {}, {}, set(), []
        e.agh.stats = {"top_queried_domains": [{"example.com": 9}, {"news.example.cz": 5}, {"query1test.example.com": 3}], "top_blocked_domains": [{"ads.example.org": 7}]}
        e.fetch_contents = {}
        e.dns.follow, e.dns.down = True, set()
        e.dns.sync_replica()
        e.parity_runs, e.parity_streak = True, {}
        e._write_parity(time.time() - 600)
        fi = e.o["filters"]
        fi.limits = dict(fl.LIMITS)
        fi.replica_wait_s, fi.parity_wait_s = 0.3, 0.5
        fi._busy = False
        e.db.x("DELETE FROM filter_changes")
        self.base_id = e.agh.add_list("Base list", BASEURL, [SENTINEL, "ads.example.org", "tracker.example.org"])
        e.fetch_contents[BASEURL] = text([SENTINEL, "ads.example.org", "tracker.example.org"])
        e.o["parental"]._cache.clear()

    def new_list(self, url="https://l.example/new.txt", domains=("new1.example.org", "new2.example.org", "new3.example.org"), serve=True):
        self.e.agh.contents[url] = list(domains)               # what AdGuard would download
        if serve:
            self.e.fetch_contents[url] = text(domains)         # what the gateway's own validation download sees
        return url

    def add(self, body, who="ivan", **kw):
        return self.e.req("POST", FL, who=who, json=body, **kw)

    def steps(self, r):
        return [(s["name"], s["ok"]) for s in r.json()["change"]["steps"]]

    def urls(self):
        return sorted(f["url"] for f in self.e.agh.filters)


class Reads(F):
    def test_view_and_catalogue(self):
        e = self.e
        js = e.req("GET", FL, who="ivan").json()
        self.assertEqual([l["name"] for l in js["lists"]], ["Base list"])
        self.assertEqual(js["limits"], {"max_rules": 300000, "hard_cap_rules": 1000000, "change_percent": 30})
        self.assertEqual(js["history"], [])
        cat = e.req("GET", FL + "/catalogue", who="ivan").json()["items"]
        self.assertGreaterEqual(len(cat), 8)
        self.assertTrue(all(c["url"].startswith("https://adguardteam.github.io/HostlistsRegistry/assets/filter_") for c in cat))
        self.assertEqual(len({c["catalogue_id"] for c in cat}), len(cat))
        self.assertEqual(e.req("GET", FL, who="gus").status_code, 403)                            # an admin who is not a parent


class Add(F):
    def test_a_good_list_passes_every_step_and_is_recorded(self):
        e = self.e
        url = fl.REGISTRY % 4
        self.new_list(url)
        r = self.add({"catalogue_id": "dan_pollocks_list"})
        self.assertEqual(r.status_code, 200, r.text)
        js = r.json()
        self.assertEqual(js["result"], "applied")
        self.assertEqual(self.steps(r), [("validate", True), ("apply", True), ("probe", True), ("replica", True), ("parity", True)])
        self.assertEqual(js["list"]["url"], url)
        self.assertEqual(js["change"]["list_id"], js["list"]["id"])
        self.assertEqual(js["change"]["by"], "Ivan")
        self.assertIn(url, self.urls())
        self.assertEqual(e.req("GET", FL, who="ivan").json()["history"][0]["id"], js["change"]["id"])
        row = e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["person"], row["ok"]), ("filter_list_add", "ivan", True))
        self.assertNotIn("filter_reverts", json.dumps(e.req("GET", "/v1/health", who="ivan").json()["filter_reverts"]))     # nothing reverted

    def test_a_list_that_blocks_a_name_the_house_needs_is_reverted_automatically(self):
        e = self.e
        url = self.new_list("https://l.example/bad.txt", ["example.org", "bad1.example.org", "bad2.example.org"])      # example.org is the harmless canary of the proof
        r = self.add({"url": url, "name": "Bad list"})
        js = r.json()
        self.assertEqual((r.status_code, js["result"]), (200, "reverted"), r.text)
        self.assertEqual(self.steps(r), [("validate", True), ("apply", True), ("probe", False), ("revert", True), ("probe_after_revert", True)])
        self.assertIn("example.org is blocked now", js["change"]["steps"][2]["detail"])
        self.assertIn("probe failed", js["change"]["reason"])
        self.assertEqual(self.urls(), [BASEURL])                                                   # the lists are as they were
        self.assertTrue(any(c[1].endswith("remove_url") for c in e.agh.calls))
        rv = e.req("GET", "/v1/health", who="ivan").json()["filter_reverts"]
        self.assertEqual([x["id"] for x in rv], [js["change"]["id"]])
        row = e.db.audit_rows(1)[0]
        self.assertEqual((row["action"], row["ok"]), ("filter_list_add", False))

    def test_a_replica_that_does_not_follow_reverts(self):
        e = self.e
        e.dns.follow = False
        url = self.new_list()
        r = self.add({"url": url, "name": "Needs the replica"})
        self.assertEqual(r.json()["result"], "reverted")
        self.assertIn(("replica", False), self.steps(r))
        self.assertIn("did not follow", r.json()["change"]["reason"])
        self.assertEqual(self.urls(), [BASEURL])

    def test_an_unreachable_replica_reverts_too(self):
        e = self.e
        e.dns.down = {"192.168.31.5"}
        r = self.add({"url": self.new_list(), "name": "x"})
        self.assertEqual(r.json()["result"], "reverted")
        self.assertIn("did not answer", r.json()["change"]["reason"])

    def test_parity_mismatch_reverts(self):
        e = self.e
        e.parity_streak = {"rules": 3}
        r = self.add({"url": self.new_list(), "name": "x"})
        js = r.json()
        self.assertEqual(js["result"], "reverted")
        self.assertIn(("parity", False), self.steps(r))
        self.assertIn("mismatch: rules", js["change"]["reason"])
        self.assertEqual(self.urls(), [BASEURL])

    def test_a_parity_checker_that_does_not_run_reverts(self):
        e = self.e
        e.parity_runs = False
        r = self.add({"url": self.new_list(), "name": "x"})
        self.assertEqual(r.json()["result"], "reverted")
        self.assertIn("did not run", r.json()["change"]["reason"])

    def test_adguard_refusing_the_list_is_a_reverted_change_with_the_reason(self):
        e = self.e
        url = self.new_list()
        e.agh.reject_add.add(url)
        r = self.add({"url": url, "name": "x"})
        self.assertEqual((r.json()["result"], self.steps(r)[:2]), ("reverted", [("validate", True), ("apply", False)]))
        self.assertIn("AdGuard refused", r.json()["change"]["steps"][1]["detail"])

    def test_adguard_down_is_a_clean_502(self):
        e = self.e
        url = self.new_list()
        e.agh.down = True
        r = self.add({"url": url, "name": "x"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "adguard_unreachable"))
        e.agh.down = False
        self.assertEqual(e.db.q1("SELECT COUNT(*) n FROM filter_changes")["n"], 0)

    def test_validation_refuses_before_anything_is_touched(self):
        e = self.e
        r = self.add({"url": "https://l.example/missing.txt", "name": "x"})                           # not downloadable
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "unreachable"))
        r = self.add({"url": BASEURL, "name": "again"})                                              # already installed
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "duplicate"))
        url = "https://l.example/empty.txt"
        e.fetch_contents[url] = "! only comments\n\n# nothing\n"
        self.assertEqual(self.add({"url": url, "name": "x"}).json()["error"]["code"], "empty_list")
        for bad in ({}, {"catalogue_id": "oisd_basic", "url": "https://l.example/x"}, {"url": "https://l.example/x"}, {"catalogue_id": "nope"}, {"url": "https://l.example/x", "name": "n" * 81},
                    {"url": 5, "name": "x"}, {"catalogue_id": "oisd_basic", "confirm_large": "yes"}, {"catalogue_id": "oisd_basic", "z": 1}):
            r = self.add(bad)
            self.assertIn(r.status_code, (400, 404), bad)
        self.assertEqual(self.urls(), [BASEURL])
        self.assertEqual(e.db.q1("SELECT COUNT(*) n FROM filter_changes")["n"], 0)

    def test_size_limits_the_hard_cap_needs_confirmation_and_never_above_the_cap(self):
        e = self.e
        e.o["filters"].limits.update({"max_rules": 5, "hard_cap_rules": 8})
        mid = self.new_list("https://l.example/mid.txt", ["m%d.example.org" % i for i in range(6)])
        r = self.add({"url": mid, "name": "mid"})
        self.assertEqual((r.status_code, r.json()["error"]["code"], r.json()["error"]["details"]["rules"]), (422, "confirm_large_required", 6))
        r = self.add({"url": mid, "name": "mid", "confirm_large": True})
        self.assertEqual((r.status_code, r.json()["result"]), (200, "applied"), r.text)
        big = self.new_list("https://l.example/big.txt", ["b%d.example.org" % i for i in range(9)])
        r = self.add({"url": big, "name": "big", "confirm_large": True})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (422, "over_hard_cap"))

    def test_the_same_idempotency_key_adds_once(self):
        e = self.e
        url = self.new_list()
        h = {"Idempotency-Key": "fl-add-key-12345"}
        r1 = self.add({"url": url, "name": "once"}, headers=h)
        r2 = self.add({"url": url, "name": "once"}, headers=h)
        self.assertEqual((r1.json()["result"], r2.json().get("replayed"), r2.json()["change"]["id"]), ("applied", True, r1.json()["change"]["id"]))
        self.assertEqual(sum(1 for c in e.agh.calls if c[1].endswith("add_url")), 1)


class Change(F):
    def test_disable_enable_rename_and_the_sentinel_rule(self):
        e = self.e
        r = e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json={"enabled": False})
        # the only list blocking the sentinel is the one being disabled: that is the intended effect, not a failure
        self.assertEqual((r.status_code, r.json()["result"]), (200, "applied"), r.text)
        self.assertFalse(next(f for f in e.agh.filters if f["id"] == self.base_id)["enabled"])
        self.assertEqual(self.steps(r)[:3], [("validate", True), ("apply", True), ("probe", True)])
        r = e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json={"enabled": True})
        self.assertEqual(r.json()["result"], "applied")
        r = e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json={"name": "Renamed"})
        self.assertEqual((r.json()["result"], r.json()["list"]["name"]), ("applied", "Renamed"))
        self.assertEqual(e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json={"name": "Renamed"}).json()["error"]["code"], "no_change")
        for bad in ({}, {"enabled": "no"}, {"zzz": 1}, {"name": 5}):
            self.assertEqual(e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json=bad).status_code, 400, bad)
        self.assertEqual(e.req("PATCH", FL + "/99999", who="ivan", json={"enabled": False}).status_code, 404)

    def test_a_sentinel_that_another_list_still_blocks_stays_required(self):
        e = self.e
        other = e.agh.add_list("Other list", "https://l.example/other.txt", [SENTINEL, "o.example.org"])
        e.fetch_contents["https://l.example/other.txt"] = text([SENTINEL, "o.example.org"])
        r = e.req("DELETE", "%s/%d" % (FL, other), who="ivan")
        self.assertEqual((r.status_code, r.json()["result"]), (200, "applied"), r.text)
        self.assertNotIn("https://l.example/other.txt", self.urls())
        # direct probe: the sentinel is blocked by the base list too; unblocking it behind the gateway's back is caught
        fi = e.o["filters"]
        base = e.run(fi.baseline())
        self.assertIn(SENTINEL, base["sentinels"])
        e.agh.active[BASEURL] = ["ads.example.org"]
        steps = []
        ok = e.run(fi._probe(base, steps, "probe"))
        self.assertFalse(ok)
        self.assertIn("sentinel doubleclick.net is not blocked any more", steps[0]["detail"])

    def test_remove_a_list_and_404_for_an_unknown_one(self):
        e = self.e
        r = e.req("DELETE", "%s/%d" % (FL, self.base_id), who="ivan")
        self.assertEqual((r.status_code, r.json()["result"], r.json()["list"]), (200, "applied", None))
        self.assertEqual(self.urls(), [])
        self.assertEqual(e.req("DELETE", FL + "/424242", who="ivan").status_code, 404)
        self.assertEqual(e.req("DELETE", FL + "/abc", who="ivan", validate=False).status_code, 404)

    def test_revert_puts_one_list_back_after_add_remove_and_rename(self):
        e = self.e
        url = self.new_list()
        added = self.add({"url": url, "name": "Added"}).json()
        lid = added["list"]["id"]
        r = e.req("POST", "%s/%d/revert" % (FL, lid), who="ivan")
        self.assertEqual((r.status_code, r.json()["result"], r.json()["change"]["action"]), (200, "applied", "revert"), r.text)
        self.assertNotIn(url, self.urls())
        # reverting the REMOVAL of the base list re-adds it
        e.req("DELETE", "%s/%d" % (FL, self.base_id), who="ivan")
        self.assertEqual(self.urls(), [])
        e.agh.contents[BASEURL] = [SENTINEL, "ads.example.org", "tracker.example.org"]
        r = e.req("POST", "%s/%d/revert" % (FL, self.base_id), who="ivan")
        self.assertEqual((r.status_code, r.json()["result"]), (200, "applied"), r.text)
        self.assertEqual(self.urls(), [BASEURL])
        # rename, then revert the rename
        nid = next(f["id"] for f in e.agh.filters if f["url"] == BASEURL)
        e.req("PATCH", "%s/%d" % (FL, nid), who="ivan", json={"name": "Renamed"})
        r = e.req("POST", "%s/%d/revert" % (FL, nid), who="ivan")
        self.assertEqual(r.json()["list"]["name"], "Base list")
        # nothing to revert
        self.assertEqual(e.req("POST", FL + "/777/revert", who="ivan").json()["error"]["code"], "not_revertable")

    def test_refresh_is_held_when_a_list_changes_by_more_than_the_limit(self):
        e = self.e
        # grows by 20 % (3 -> ... use a bigger list): within the limit -> applied
        url = self.new_list("https://l.example/r.txt", ["r%d.example.org" % i for i in range(10)])
        lid = self.add({"url": url, "name": "R"}).json()["list"]["id"]
        e.agh.contents[url] = ["r%d.example.org" % i for i in range(12)]
        e.fetch_contents[url] = text(e.agh.contents[url])
        e.agh.calls.clear()
        r = e.req("POST", "%s/%d/refresh" % (FL, lid), who="ivan")
        self.assertEqual((r.status_code, r.json()["result"]), (200, "applied"), r.text)
        self.assertTrue(any(c[1].endswith("/refresh") for c in e.agh.calls))
        # shrinks from 12 to 5 rules (58 % > 30 %): HELD, nothing applied
        e.agh.contents[url] = ["r%d.example.org" % i for i in range(5)]
        e.fetch_contents[url] = text(e.agh.contents[url])
        e.agh.calls.clear()
        r = e.req("POST", "%s/%d/refresh" % (FL, lid), who="ivan")
        js = r.json()
        self.assertEqual((r.status_code, js["result"]), (200, "held"))
        self.assertIn("58%", js["hold_reason"])
        self.assertFalse(any(c[1].endswith("/refresh") for c in e.agh.calls))
        self.assertEqual(next(f for f in e.agh.filters if f["id"] == lid)["rules_count"], 12)                 # untouched
        self.assertEqual(e.req("GET", FL, who="ivan").json()["history"][0]["result"], "held")
        # confirmed: applied
        r = e.req("POST", "%s/%d/refresh" % (FL, lid), who="ivan", json={"confirm": True})
        self.assertEqual(r.json()["result"], "applied")
        self.assertEqual(next(f for f in e.agh.filters if f["id"] == lid)["rules_count"], 5)
        self.assertEqual(e.req("POST", "%s/%d/refresh" % (FL, lid), who="ivan", json={"confirm": "yes"}).status_code, 400)

    def test_a_refresh_that_breaks_the_probe_is_reverted(self):
        e = self.e
        url = self.new_list("https://l.example/r2.txt", ["x%d.example.org" % i for i in range(10)])
        lid = self.add({"url": url, "name": "R2"}).json()["list"]["id"]
        e.agh.contents[url] = ["x%d.example.org" % i for i in range(10)] + ["example.org"]                         # +10 %: allowed - but it now blocks the canary
        e.fetch_contents[url] = text(e.agh.contents[url])
        r = e.req("POST", "%s/%d/refresh" % (FL, lid), who="ivan")
        self.assertEqual(r.json()["result"], "reverted")
        self.assertIn("example.org is blocked now", r.json()["change"]["reason"])

    def test_only_one_change_runs_at_a_time(self):
        e = self.e
        fi = e.o["filters"]
        fi.parity_wait_s = 2.0
        e.parity_runs = False
        url = self.new_list()
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.add({"url": url, "name": "slow"})))
        t.start()
        time.sleep(0.8)
        r = e.req("PATCH", "%s/%d" % (FL, self.base_id), who="ivan", json={"name": "Concurrent"})
        t.join(10)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "filter_busy"))
        self.assertEqual(out["r"].json()["result"], "reverted")                                    # the slow one failed the parity wait and went back
        self.assertEqual(next(f for f in e.agh.filters if f["id"] == self.base_id)["name"], "Base list")

    def test_a_change_interrupted_by_a_restart_is_put_back(self):
        e = self.e
        fi = e.o["filters"]
        snapshot = [{"id": self.base_id, "name": "Base list", "url": BASEURL, "enabled": True, "rules_count": 3, "last_updated": None}]
        url = self.new_list("https://l.example/half.txt")
        e.agh.add_list("Half applied", url, ["h.example.org"])                                          # the state the crash left behind
        e.db.x("INSERT INTO filter_changes(id,at,by,action,list_id,list_name,result,steps,reason,snapshot) VALUES ('fcrash',?,?,?,?,?,?,?,?,?)",
               (time.time(), "Ivan", "add", None, "Half applied", "running", "[]", None, json.dumps(snapshot)))
        e.run(fi.recover())
        self.assertEqual(self.urls(), [BASEURL])
        row = e.db.q1("SELECT * FROM filter_changes WHERE id='fcrash'")
        self.assertEqual((row["result"], row["reason"]), ("reverted", "interrupted by a restart"))


class Guard(unittest.TestCase):
    def test_count_rules_reads_adblock_hosts_and_plain_lists(self):
        n, doms = fl.count_rules("! c\n# c\n[Adblock Plus]\n||a.example.org^\n0.0.0.0 b.example.org\n127.0.0.1 c.example.org\nd.example.org\n@@||white.example.org^\n\n")
        self.assertEqual(n, 5)
        self.assertEqual(doms, ["a.example.org", "b.example.org", "c.example.org", "d.example.org"])

    def test_public_address_refuses_loopback_private_and_names_that_resolve_there(self):
        for host in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.31.2", "169.254.1.1", "100.76.61.8", "::1"):
            self.assertFalse(fl.public_address(host, 443, set()), host)
        self.assertTrue(fl.public_address("127.0.0.1", 8000, {"127.0.0.1:8000"}))
        self.assertTrue(fl.public_address("localhost", 8000, {"localhost"}))
        self.assertFalse(fl.public_address("no-such-host.invalid", 443, set()))

    def test_fetch_refuses_bad_urls_without_a_request(self):
        for url in ("http://example.org/l.txt", "ftp://example.org/l", "https://user:pw@example.org/l", "https://127.0.0.1/l", "https://localhost/l", "https://10.0.0.5/l",
                    "https://[::1]/l", "https://example.org/a b", "https://" + "a" * 600):
            with self.assertRaises(fl.Refused, msg=url) as cm:
                fl.fetch_url(url)
            self.assertEqual(cm.exception.status, 422, url)

    def test_fetch_from_an_operator_allowed_address_size_cap_and_redirects(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "http://10.9.9.9/secret")
                    self.end_headers()
                    return
                body = b"||a.example.org^\n" * (5000 if self.path == "/big" else 3)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % port
            with self.assertRaises(fl.Refused):                                     # loopback is not allowed unless an operator wrote it down
                fl.fetch_url(base + "/l")
            allowed = {"127.0.0.1:%d" % port}
            self.assertIn("a.example.org", fl.fetch_url(base + "/l", allowed))
            with self.assertRaises(fl.Refused) as cm:
                fl.fetch_url(base + "/big", allowed, max_bytes=1000)
            self.assertEqual(cm.exception.code, "too_large")
            with self.assertRaises(fl.Refused) as cm:                               # a redirect to a private address is checked like the first hop
                fl.fetch_url(base + "/redir", allowed)
            self.assertEqual(cm.exception.code, "bad_url")
        finally:
            srv.shutdown()

    def test_diff_ops(self):
        cur = [{"url": "u1", "name": "A", "enabled": True}, {"url": "u2", "name": "B", "enabled": True}]
        want = [{"url": "u1", "name": "A2", "enabled": False}, {"url": "u3", "name": "C", "enabled": False}]
        ops = fl.FilterLists.diff_ops(cur, want)
        self.assertEqual(ops, [("set", {"name": "A2", "url": "u1", "enabled": False}), ("add", {"name": "C", "url": "u3"}), ("set", {"name": "C", "url": "u3", "enabled": False}),
                               ("remove", {"url": "u2"})])
        self.assertEqual(fl.FilterLists.diff_ops(cur, want, only_url="u2"), [("remove", {"url": "u2"})])
        self.assertEqual(fl.FilterLists.diff_ops(cur, cur), [])

    def test_dns_client_parses_answers_and_detects_blocking(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        def serve():
            for answer in ("93.184.216.34", "0.0.0.0", None):
                data, addr = sock.recvfrom(512)
                if answer is None:                                                    # NXDOMAIN
                    sock.sendto(data[:2] + b"\x81\x83" + data[4:6] + b"\x00\x00\x00\x00\x00\x00" + data[12:], addr)
                    continue
                q = data[12:]
                sock.sendto(data[:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00" + q + b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(answer), addr)
        threading.Thread(target=serve, daemon=True).start()
        a = aghmod.dns_query("127.0.0.1", "example.org", port)
        self.assertEqual((a["rcode"], a["addrs"], aghmod.dns_blocked(a)), (0, ["93.184.216.34"], False))
        a = aghmod.dns_query("127.0.0.1", "ads.example.org", port)
        self.assertEqual((a["addrs"], aghmod.dns_blocked(a)), (["0.0.0.0"], True))
        a = aghmod.dns_query("127.0.0.1", "gone.example.org", port)
        self.assertEqual((a["rcode"], aghmod.dns_blocked(a)), (3, True))
        sock.close()
        with self.assertRaises(OSError):
            aghmod.dns_query("127.0.0.1", "example.org", port, timeout=0.3)


if __name__ == "__main__":
    unittest.main()
