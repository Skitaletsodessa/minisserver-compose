"""Tests of the host policy job's snapshot side (Task 29 Part 2): childsnapshot.py and the snapshot branch of childpolicy.run().
A REAL schedule-service Store produces the snapshot (PORTAL_SRC points at the portal code); AdGuard is a fake that records what the job writes; the clock is
passed in. The service is never running: everything the job does here, it does from the file - which is the point.

  PORTAL_SRC=/srv/compose/portal/schedules /srv/apps/portal/venv-test/bin/python -m unittest discover -s tests -t . -v      (from /srv/compose/adguard)
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ADGUARD = os.path.dirname(HERE)
PORTAL = os.environ.get("PORTAL_SRC", "/srv/compose/portal/schedules")
sys.path.insert(0, ADGUARD)
sys.path.insert(0, PORTAL)

TMP = tempfile.mkdtemp(prefix="childjob-")
os.environ["CHILD_SNAPSHOT"] = os.path.join(TMP, "out", "child-policy.json")
os.environ["CHILD_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["CHILD_APPLY_STATUS"] = os.path.join(TMP, "child-apply-status.json")
os.environ["CHILD_SOURCE_FILE"] = os.path.join(TMP, "source.conf")

import childsnapshot  # noqa: E402
import jobenv  # noqa: E402
import childpolicy  # noqa: E402
import overrides  # noqa: E402
from sched import store as storemod, model, child_invariants  # noqa: E402

TZ = model.TZ
MON_SAT = [1, 2, 3, 4, 5, 6]
RULES = [{"id": "yt_roblox", "days": MON_SAT, "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}},
         {"id": "maps", "days": MON_SAT, "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}}]
SYSTEM = {"token": "-", "name": "test", "role": "admin"}
SD_CATALOGUE = {"blocked_services": [{"id": "telegram", "rules": ["||t.me^", "||telegram.org^"]}, {"id": "youtube", "rules": ["||youtube.com^", "||ytimg.com^"]},
                                       {"id": "roblox", "rules": ["||roblox.com^"]}]}


def payload():
    return {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "20:36:d0:14:8d:5a", "rules": RULES},
                        {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": RULES}],
            "lists": {"allow_always": sorted(child_invariants.REQUIRED_ALWAYS) + ["extra-always.example.org"], "allow_weekdays": ["maps.example.com"],
                      "allow_extra": [], "deny_extra": []}, "always_services": ["telegram"]}


class FakeAgh:
    """AdGuard stand-in: the catalogue, user_rules, clients; records every write."""
    def __init__(self):
        self.user_rules, self.clients, self.writes = [], [], []

    def call(self, method, path, body=None):
        if path == "/status":
            return {"running": True}
        if path == "/blocked_services/all":
            return SD_CATALOGUE
        if path == "/filtering/status":
            return {"user_rules": list(self.user_rules)}
        if path == "/filtering/set_rules":
            self.writes.append(("set_rules", body))
            self.user_rules = list(body["rules"])
            return b""
        if path == "/clients":
            return {"clients": list(self.clients)}
        if path in ("/clients/add", "/clients/update"):
            self.writes.append((path, body))
            data = body if path == "/clients/add" else body["data"]
            self.clients = [c for c in self.clients if c["name"] != data["name"]] + [data]
            return b""
        raise AssertionError("unexpected AdGuard call %s %s" % (method, path))


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        os.makedirs(os.path.join(TMP, "out"))
        jobenv.bind(TMP)
        self.store = storemod.Store(os.path.join(TMP, "svc"))
        self.store.seed(os.path.join(PORTAL, "seed", "initial.json"), SYSTEM)
        self.assertTrue(self.store.import_child_policy(SYSTEM, payload()))
        self.agh = FakeAgh()
        childpolicy.Api = lambda: self.agh
        childpolicy.trigger_sync = lambda: None
        childpolicy.overrides.STATE_DIR = type(overrides.STATE_DIR)(os.path.join(TMP, "state"))
        childpolicy.LOCK = childpolicy.overrides.STATE_DIR / "apply.lock"
        childpolicy.ERR_FILE = childpolicy.overrides.STATE_DIR / "consecutive-errors"
        childpolicy.overrides.STATE = childpolicy.overrides.STATE_DIR / "overrides.json"
        self.snapshot_path = os.environ["CHILD_SNAPSHOT"]
        self.publish()

    def publish(self):
        self.store.publish()
        shutil.copyfile(os.path.join(self.store.out, "child-policy.json"), self.snapshot_path)

    def mode(self, m):
        open(os.environ["CHILD_SOURCE_FILE"], "w").write("# comment\n%s\n" % m)

    def at(self, day, h, m=0):                      # 2026-10-19 is a Monday
        return dt.datetime(2026, 10, 19 + day, h, m, tzinfo=TZ)

    def block(self):
        r = self.agh.user_rules
        return [x for x in r[r.index(childpolicy.BEGIN):r.index(childpolicy.END) + 1]]

    def allowed(self, ip):
        return {x.split("||")[1].split("^")[0] for x in self.block() if x.startswith("@@||") and x.endswith("$client=" + ip)}

    def status(self):
        return json.load(open(os.environ["CHILD_APPLY_STATUS"]))


class Mode(Base):
    def test_the_mode_file_parses_comments_and_defaults_to_the_proven_path(self):
        self.mode("snapshot")
        self.assertEqual(childsnapshot.mode(), "snapshot")
        self.mode("files")
        self.assertEqual(childsnapshot.mode(), "files")
        for junk in ("snapshots", "yes", "SNAPSHOT", ""):
            open(os.environ["CHILD_SOURCE_FILE"], "w").write(junk + "\n")
            self.assertEqual(childsnapshot.mode(), "files", junk)
        os.remove(os.environ["CHILD_SOURCE_FILE"])
        self.assertEqual(childsnapshot.mode(), "files")


class SnapshotMode(Base):
    def setUp(self):
        super().setUp()
        self.mode("snapshot")

    def test_the_job_renders_from_the_file_by_its_own_clock_with_no_service(self):
        childpolicy.run(self.at(0, 12))
        self.assertIn("||*^$client=192.168.31.60", self.block())
        self.assertNotIn("youtube.com", self.allowed("192.168.31.60"))
        self.assertIn("t.me", self.allowed("192.168.31.60"))
        self.assertIn("extra-always.example.org", self.allowed("192.168.31.60"))
        for h, m, want in ((15, 59, False), (16, 0, True), (18, 59, True), (19, 0, False)):
            childpolicy.run(self.at(0, h, m))
            self.assertEqual("youtube.com" in self.allowed("192.168.31.60"), want, "%02d:%02d" % (h, m))
        childpolicy.run(self.at(6, 17))                                              # Sunday: no window, Maps dropped
        self.assertNotIn("youtube.com", self.allowed("192.168.31.60"))
        self.assertNotIn("maps.example.com", self.allowed("192.168.31.60"))
        self.assertEqual(self.status()["source"], "snapshot")

    def test_the_block_equals_the_services_own_renderer_exactly(self):
        from sched import child_render
        sd = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com", "ytimg.com"], "roblox": ["roblox.com"]}
        for now in (self.at(0, 12), self.at(1, 17), self.at(6, 17), self.at(3, 3)):
            childpolicy.run(now)
            self.assertEqual(self.block(), child_render.render_block(self.store.child_policy(), sd, now, []))

    def test_exceptions_come_from_the_snapshot_and_expire_by_the_jobs_clock(self):
        self.store.clock = lambda: self.at(0, 12).isoformat(timespec="seconds")
        self.store.add_child_exception(SYSTEM, "child_a", "youtube", minutes=30)
        self.publish()
        childpolicy.run(self.at(0, 12, 10))
        self.assertIn("youtube.com", self.allowed("192.168.31.60"))
        self.assertNotIn("youtube.com", self.allowed("192.168.31.61"))                  # only that child
        childpolicy.run(self.at(0, 12, 31))                                           # expired: dropped by the JOB, the service did nothing
        self.assertNotIn("youtube.com", self.allowed("192.168.31.60"))
        self.store.clock = lambda: self.at(0, 12, 40).isoformat(timespec="seconds")
        self.store.add_child_exception(SYSTEM, "child_b", "all", minutes=30)
        self.publish()
        childpolicy.run(self.at(0, 12, 50))
        self.assertNotIn("||*^$client=192.168.31.61", self.block())                      # 'all' lifts the catch-all of that child
        self.assertIn("||*^$client=192.168.31.60", self.block())

    def test_per_child_lists_and_deny_beats_allow(self):
        self.store.change_child_domain(SYSTEM, "child_a", "allow", "ok.example.org", True)
        self.store.change_child_domain(SYSTEM, "child_a", "deny", "bad.example.org", True)
        self.publish()
        childpolicy.run(self.at(0, 17))
        self.assertIn("ok.example.org", self.allowed("192.168.31.60"))
        self.assertNotIn("ok.example.org", self.allowed("192.168.31.61"))
        self.assertIn("||bad.example.org^$client=192.168.31.60", self.block())
        self.assertNotIn("||bad.example.org^$client=192.168.31.61", self.block())

    def test_nothing_is_written_when_nothing_changed_and_the_status_records_the_hash(self):
        childpolicy.run(self.at(0, 12))
        n = len(self.agh.writes)
        childpolicy.run(self.at(0, 12, 1))
        self.assertEqual(len(self.agh.writes), n)
        st = self.status()
        self.assertEqual((st["ok"], st["changed"], st["snapshot_source"]), (True, False, "live"))
        self.assertEqual(st["rules_hash"], childsnapshot.rules_hash(self.block()))
        self.assertEqual(st["managed_lines"], len(self.block()) - 2)

    def test_clients_come_from_the_snapshot(self):
        childpolicy.run(self.at(0, 12))
        names = sorted(c["name"] for c in self.agh.clients)
        self.assertEqual(names, ["a-phone", "b-tablet"])
        self.assertTrue(all(c["safe_search"]["enabled"] and c["safe_search"]["youtube"] and c["use_global_settings"] is False for c in self.agh.clients))   # invariant 2

    def test_a_corrupt_or_invariant_breaking_file_falls_back_to_the_last_good_snapshot(self):
        childpolicy.run(self.at(0, 12))
        good = list(self.block())
        open(self.snapshot_path, "w").write("{ this is not json")
        childpolicy.run(self.at(0, 13))
        self.assertEqual(self.block(), good)
        st = self.status()
        self.assertEqual((st["ok"], st["snapshot_source"]), (True, "last_good"))
        self.assertIn("JSONDecodeError", st["live_problem"])
        snap = json.load(open(os.path.join(self.store.out, "child-policy.json")))
        snap["lists"]["allow_always"].remove("kidsmanagement-pa.googleapis.com")           # an invariant is gone (and the checksum is now wrong too)
        open(self.snapshot_path, "w").write(json.dumps(snap))
        childpolicy.run(self.at(0, 14))
        self.assertEqual(self.status()["snapshot_source"], "last_good")
        self.assertEqual(self.block(), good)
        snap["checksum"] = __import__("hashlib").sha256(json.dumps({k: v for k, v in snap.items() if k not in ("generated_at", "audit_seq", "checksum")},
                                                                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        open(self.snapshot_path, "w").write(json.dumps(snap))                                # a consistent file that still breaks invariant 3
        childpolicy.run(self.at(0, 15))
        self.assertEqual(self.status()["snapshot_source"], "last_good")
        self.assertIn("always-on", self.status()["live_problem"])

    def test_no_usable_snapshot_at_all_leaves_adguard_untouched_and_says_so(self):
        childpolicy.run(self.at(0, 12))
        before = list(self.agh.user_rules)
        os.remove(self.snapshot_path)
        shutil.rmtree(os.path.join(TMP, "state", "last-good-snapshot.json"), ignore_errors=True)
        try:
            os.remove(os.path.join(TMP, "state", "last-good-snapshot.json"))
        except OSError:
            pass
        writes = len(self.agh.writes)
        with self.assertRaises(RuntimeError):
            childpolicy.run(self.at(0, 16))
        self.assertEqual((self.agh.user_rules, len(self.agh.writes)), (before, writes))
        self.assertFalse(self.status()["ok"])

    def test_a_deny_that_would_block_an_always_on_host_is_ignored_defensively(self):
        snap = json.load(open(self.snapshot_path))
        snap["devices"][0]["deny_extra"] = ["google.com", "bad.example.org"]
        policy, ignored = childsnapshot.policy_from(snap)
        self.assertEqual(policy["devices"][0]["deny_extra"], ["bad.example.org"])
        self.assertEqual(ignored, ["child_a: google.com"])


class Shadow(Base):
    def test_files_mode_still_enforces_the_files_and_the_shadow_comparison_counts(self):
        self.mode("files")
        saved = (childpolicy.POLICY, childpolicy.load_clients)
        shadow = os.path.join(TMP, "files-policy")
        os.makedirs(shadow)
        for name in ("allow-always.txt", "allow-weekdays.txt", "allow-extra.txt", "deny-extra.txt"):
            open(os.path.join(shadow, name), "w").write("\n".join({"allow-always.txt": payload()["lists"]["allow_always"], "allow-weekdays.txt": ["maps.example.com"]}.get(name, [])) + "\n")
        open(os.path.join(shadow, "clients.conf"), "w").write("192.168.31.60 a-phone 20:36:d0:14:8d:5a\n192.168.31.61 b-tablet\n")
        childpolicy.POLICY = type(childpolicy.POLICY)(shadow)
        try:
            n = 0
            for day in range(7):
                for h in (0, 6, 15, 16, 17, 18, 19, 23):
                    childpolicy.run(self.at(day, h, 30 if h == 15 else 0))
                    n += 1
            st = json.load(open(os.path.join(TMP, "state", "shadow.json")))
            self.assertEqual((st["checked"], st["mismatches"]), (n, 0))
            self.assertEqual(self.status()["source"], "files")
            # a difference in the lists is a finding, recorded - and the files still win
            open(os.path.join(shadow, "allow-extra.txt"), "w").write("only-in-files.example.org\n")
            childpolicy.run(self.at(0, 12))
            st = json.load(open(os.path.join(TMP, "state", "shadow.json")))
            self.assertEqual(st["mismatches"], 1)
            self.assertIn("only-in-files.example.org", st["last_mismatch"]["detail"])
            self.assertIn("only-in-files.example.org", self.allowed("192.168.31.60"))
        finally:
            childpolicy.POLICY = saved[0]

    def test_a_broken_snapshot_is_a_recorded_finding_in_the_shadow_run_not_an_error(self):
        self.mode("files")
        os.remove(self.snapshot_path)
        shadow = os.path.join(TMP, "files-policy2")
        os.makedirs(shadow)
        for name in ("allow-always.txt", "allow-weekdays.txt", "allow-extra.txt", "deny-extra.txt"):
            open(os.path.join(shadow, name), "w").write("a.example.org\n" if name == "allow-always.txt" else "\n")
        open(os.path.join(shadow, "clients.conf"), "w").write("192.168.31.60 a-phone\n")
        saved = childpolicy.POLICY
        childpolicy.POLICY = type(saved)(shadow)
        try:
            childpolicy.run(self.at(0, 12))
            st = json.load(open(os.path.join(TMP, "state", "shadow.json")))
            self.assertEqual(st["mismatches"], 1)
            self.assertIn("no usable snapshot", st["last_mismatch"]["detail"])
        finally:
            childpolicy.POLICY = saved


if __name__ == "__main__":
    unittest.main()
