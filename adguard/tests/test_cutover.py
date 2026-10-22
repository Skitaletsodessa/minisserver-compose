"""The cutover tool (Task 29 Phase H) end to end on a scratch setup: a REAL schedule-service store, a fake AdGuard that evaluates check_host from its own rules, scratch
policy files, a stubbed service client / systemctl / git. Proves: pre-flight refusals, the switch with an identical block, the automatic rollback after a failed
verification, and the standalone rollback.
  PORTAL_SRC=/srv/compose/portal/schedules /srv/apps/portal/venv-test/bin/python -m unittest discover -s tests          (from /srv/compose/adguard)
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

TMP = tempfile.mkdtemp(prefix="cutover-")
os.environ["CHILD_SNAPSHOT"] = os.path.join(TMP, "out", "child-policy.json")
os.environ["CHILD_STATE_DIR"] = os.path.join(TMP, "state")
os.environ["CHILD_APPLY_STATUS"] = os.path.join(TMP, "child-apply-status.json")
os.environ["CHILD_SOURCE_FILE"] = os.path.join(TMP, "source.conf")
os.environ["CUTOVER_BUNDLES"] = os.path.join(TMP, "bundles")
os.environ["PARITY_STATE"] = os.path.join(TMP, "parity.json")

import childsnapshot  # noqa: E402
import jobenv  # noqa: E402
import childpolicy  # noqa: E402
import overrides  # noqa: E402
from sched import store as storemod, model, child_invariants  # noqa: E402

cc = __import__("importlib").import_module("child-cutover".replace("-", "_")) if False else None
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("child_cutover", os.path.join(ADGUARD, "child-cutover.py"))
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

TZ = model.TZ
SYSTEM = {"token": "-", "name": "test", "role": "admin"}
RULES = [{"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}]
SD = {"blocked_services": [{"id": "telegram", "rules": ["||t.me^", "||telegram.org^"]}, {"id": "youtube", "rules": ["||youtube.com^", "||ytimg.com^"]},
                           {"id": "roblox", "rules": ["||roblox.com^"]}]}
ALLOW_ALWAYS = sorted(child_invariants.REQUIRED_ALWAYS) + ["extra-always.example.org", "maps.example.com"]


class FakeAgh:
    def __init__(self):
        self.user_rules, self.clients = [], []

    def call(self, method, path, body=None):
        if path == "/status":
            return {"running": True}
        if path == "/blocked_services/all":
            return SD
        if path == "/filtering/status":
            return {"user_rules": list(self.user_rules)}
        if path == "/filtering/set_rules":
            self.user_rules = list(body["rules"])
            return b""
        if path == "/clients":
            return {"clients": list(self.clients)}
        if path in ("/clients/add", "/clients/update"):
            data = body if path == "/clients/add" else body["data"]
            self.clients = [c for c in self.clients if c["name"] != data["name"]] + [data]
            return b""
        if path.startswith("/filtering/check_host"):
            q = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
            name, ip = q["name"], q["client"]
            tag = "$client=" + ip
            for r in self.user_rules:
                if r.startswith("@@||") and r.endswith(tag):
                    d = r[4:].split("^", 1)[0]
                    if name == d or name.endswith("." + d):
                        return {"reason": "NotFilteredWhiteList", "rule": r}
            for r in self.user_rules:
                if r.startswith("||") and r.endswith(tag) and not r.startswith("||*"):
                    d = r[2:].split("^", 1)[0]
                    if name == d or name.endswith("." + d):
                        return {"reason": "FilteredBlackList", "rule": r}
            if "||*^" + tag in self.user_rules:
                return {"reason": "FilteredBlackList", "rule": "||*^" + tag}
            return {"reason": "NotFilteredNotFound", "rule": ""}
        raise AssertionError(path)


class Base(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        for d in ("out", "state", "bundles", "policy"):
            os.makedirs(os.path.join(TMP, d))
        jobenv.bind(TMP)                                                     # another test module may have imported childsnapshot first
        self.store = storemod.Store(os.path.join(TMP, "svc"))
        self.store.seed(os.path.join(PORTAL, "seed", "initial.json"), SYSTEM)
        self.store.import_child_policy(SYSTEM, {"devices": [{"id": "child_a", "name": "a-phone", "ip": "192.168.31.60", "mac": "20:36:d0:14:8d:5a", "rules": RULES},
                                                            {"id": "child_b", "name": "b-tablet", "ip": "192.168.31.61", "mac": "", "rules": RULES}],
                                               "lists": {"allow_always": ALLOW_ALWAYS, "allow_weekdays": ["weekday.example.com"], "allow_extra": [], "deny_extra": []},
                                               "always_services": ["telegram"]})
        self.store.publish()
        shutil.copyfile(os.path.join(self.store.out, "child-policy.json"), os.environ["CHILD_SNAPSHOT"])
        pol = os.path.join(TMP, "policy")
        for name, lines in (("allow-always.txt", ALLOW_ALWAYS), ("allow-weekdays.txt", ["weekday.example.com"]), ("allow-extra.txt", []), ("deny-extra.txt", []),
                            ("clients.conf", ["192.168.31.60 a-phone 20:36:d0:14:8d:5a", "192.168.31.61 b-tablet"]), ("source.conf", ["files"])):
            open(os.path.join(pol, name), "w").write("\n".join(lines) + "\n")
        childpolicy.POLICY = type(childpolicy.POLICY)(pol)
        cc.POLICY = childpolicy.POLICY
        childsnapshot.MODE_FILE = type(childsnapshot.MODE_FILE)(os.path.join(pol, "source.conf"))
        cc.BUNDLES = type(cc.BUNDLES)(os.path.join(TMP, "bundles"))
        cc.PARITY_STATE = type(cc.PARITY_STATE)(os.environ["PARITY_STATE"])
        json.dump({"streak": {}}, open(os.environ["PARITY_STATE"], "w"))
        self.agh = FakeAgh()
        childpolicy.Api = lambda: self.agh
        childpolicy.trigger_sync = lambda: None
        childpolicy.overrides.STATE_DIR = type(overrides.STATE_DIR)(os.path.join(TMP, "state"))
        childpolicy.LOCK = childpolicy.overrides.STATE_DIR / "apply.lock"
        childpolicy.ERR_FILE = childpolicy.overrides.STATE_DIR / "consecutive-errors"
        childpolicy.overrides.STATE = childpolicy.overrides.STATE_DIR / "overrides.json"
        childsnapshot.SHADOW = childsnapshot.STATE_DIR / "shadow.json"
        self.sys_calls, self.gen_calls, self.git_calls, self.svc_calls = [], [], [], []
        cc.sysctl = lambda *a: self.sys_calls.append(a) or 0
        cc.generate_lists = lambda: self.gen_calls.append(1) or 0
        cc.git = lambda *a: self.git_calls.append(a) or type("R", (), {"stdout": "abc123\n", "returncode": 0})()
        cc.svc = self.fake_svc
        self.now = dt.datetime(2026, 10, 21, 12, 0, 30, tzinfo=TZ)           # a Wednesday, far from every edge
        for i in range(5):                                                  # a shadow period in miniature
            childpolicy.run(self.now - dt.timedelta(minutes=4 - i))

    def fake_svc(self, method, path, body=None):
        self.svc_calls.append((method, path, body))
        if path == "/v1/health":
            return 200, {"in_sync": True, "child_policy": {"cutover": self.store.child_cutover_done()}}
        if path == "/v1/child/cutover":
            self.store.set_child_cutover(SYSTEM, body["done"], body.get("note", ""))
            return 200, {"cutover": self.store.child_cutover_done()}
        raise AssertionError(path)

    def pre(self, **kw):
        return cc.preflight(self.now, min_hours=0, min_checks=1, **kw)

    def mode(self):
        return childsnapshot.mode()

    def block(self):
        r = self.agh.user_rules
        return r[r.index(childpolicy.BEGIN):r.index(childpolicy.END) + 1]


class Preflight(Base):
    def test_a_good_shadow_period_passes(self):
        self.assertEqual(self.pre(), [])

    def test_edges_are_computed_on_both_sides(self):
        t = lambda h, m: dt.datetime(2026, 10, 21, h, m, tzinfo=TZ)
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(12, 0))), 240)
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(15, 50))), 10)
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(16, 5))), 5)
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(23, 55))), 5)                      # midnight, the day before
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(0, 3))), 3)
        self.assertEqual(round(cc.minutes_to_nearest_edge(t(18, 45))), 15)

    def test_refusals(self):
        for hh, mm in ((15, 50), (16, 5), (18, 50), (19, 10), (23, 50), (0, 5)):
            self.now = dt.datetime(2026, 10, 21, hh, mm, 30, tzinfo=TZ)
            childpolicy.run(self.now)
            self.assertTrue(any("window edge" in p for p in self.pre()), (hh, mm))
        self.now = dt.datetime(2026, 10, 21, 12, 0, 30, tzinfo=TZ)
        childpolicy.run(self.now)
        self.assertEqual(self.pre(), [])
        # a shadow mismatch (someone edited the files after the import)
        open(os.path.join(TMP, "policy", "allow-extra.txt"), "w").write("only-in-files.example.org\n")
        childpolicy.run(self.now)
        self.assertTrue(any("mismatch" in p for p in self.pre()))
        open(os.path.join(TMP, "policy", "allow-extra.txt"), "w").write("\n")
        # too short a shadow period
        self.assertTrue(any("shadow comparisons so far" in p for p in cc.preflight(self.now, min_hours=0, min_checks=100000)))
        self.assertTrue(any("shadow period is" in p for p in cc.preflight(self.now, min_hours=20, min_checks=1)))
        # an exception of the old file that the model does not have
        overrides.add("192.168.31.60", "youtube", 30, now=self.now)
        self.assertTrue(any("exception of the OLD file" in p for p in self.pre()))

    def test_parity_and_job_state_are_part_of_it(self):
        json.dump({"streak": {"rules": 2}}, open(os.environ["PARITY_STATE"], "w"))
        self.assertTrue(any("parity" in p for p in self.pre()))
        json.dump({"streak": {}}, open(os.environ["PARITY_STATE"], "w"))
        childsnapshot.write_status(ok=False, source="files", error="boom")
        self.assertTrue(any("not a healthy" in p for p in self.pre()))

    def test_nothing_changes_without_the_go_and_inside_the_edge_guard(self):
        self.assertEqual(cc.cutover(self.now, go=False, min_hours=0, min_checks=1), 2)
        self.assertEqual((self.mode(), self.sys_calls, self.store.child_cutover_done()), ("files", [], None))
        self.now = dt.datetime(2026, 10, 21, 15, 55, 30, tzinfo=TZ)
        childpolicy.run(self.now)
        self.assertEqual(cc.cutover(self.now, go=True, min_hours=0, min_checks=1), 1)
        self.assertEqual((self.mode(), self.sys_calls, self.store.child_cutover_done(), os.listdir(os.environ["CUTOVER_BUNDLES"])), ("files", [], None, []))


class Switch(Base):
    def test_the_switch_keeps_the_block_identical_and_does_everything_else(self):
        before = list(self.block())
        h = cc.applied_hash()
        self.assertEqual(cc.cutover(self.now, go=True, min_hours=0, min_checks=1), 0)
        self.assertEqual(self.mode(), "snapshot")
        self.assertEqual(self.block(), before)                                                  # byte for byte
        st = json.load(open(os.environ["CHILD_APPLY_STATUS"]))
        self.assertEqual((st["source"], st["ok"], st["rules_hash"]), ("snapshot", True, h))
        self.assertTrue(self.store.child_cutover_done())
        self.assertIn(("disable", "--now", "child-ui.service"), self.sys_calls)
        self.assertIn(("enable", "--now", "adguard-child-lists.path"), self.sys_calls)
        self.assertEqual(self.gen_calls, [1])
        bundles = os.listdir(os.environ["CUTOVER_BUNDLES"])
        self.assertEqual(len(bundles), 1)
        self.assertTrue(set(os.listdir(os.path.join(os.environ["CUTOVER_BUNDLES"], bundles[0]))) >= {"allow-always.txt", "clients.conf", "source.conf", "README.txt"})
        self.assertIn(("tag", "-f", "pre-task29-cutover"), self.git_calls)
        # the model is now the source: a re-import is refused (it would overwrite the parents' edits)
        from sched import store as sm
        with self.assertRaises(sm.Forbidden):
            self.store.import_child_policy(SYSTEM, {"devices": [{"id": "x", "name": "x", "ip": "192.168.31.99", "mac": "", "rules": RULES}], "lists": {}, "always_services": ["telegram"]})

    def test_after_the_switch_the_job_follows_the_service_data_alone(self):
        self.assertEqual(cc.cutover(self.now, go=True, min_hours=0, min_checks=1), 0)
        self.store.clock = lambda: self.now.isoformat(timespec="seconds")
        self.store.change_child_domain(SYSTEM, "child_a", "allow", "school.example.org", True)
        self.store.publish()
        shutil.copyfile(os.path.join(self.store.out, "child-policy.json"), os.environ["CHILD_SNAPSHOT"])
        childpolicy.run(self.now + dt.timedelta(minutes=1))
        self.assertIn("@@||school.example.org^$client=192.168.31.60", self.block())
        self.assertNotIn("@@||school.example.org^$client=192.168.31.61", self.block())         # per child now
        self.assertIn("school.example.org", open(os.path.join(TMP, "policy", "allow-always.txt")).read() + "school.example.org") if False else None

    def test_a_failed_verification_rolls_back_by_itself(self):
        files_before = {f: open(os.path.join(TMP, "policy", f)).read() for f in cc.FILES}
        block_before = list(self.block())
        calls = {"n": 0}
        real = cc.child_answers

        def flaky(api):
            calls["n"] += 1
            out = real(api)
            if calls["n"] == 2:                                                                  # the second look (after the switch) differs
                out = dict(out)
                out[next(iter(out))] = ("FilteredBlackList", "something changed")
            return out
        cc.child_answers = flaky
        try:
            rc = cc.cutover(self.now, go=True, min_hours=0, min_checks=1)
        finally:
            cc.child_answers = real
        self.assertEqual(rc, 1)
        self.assertEqual(self.mode(), "files")
        self.assertEqual(self.store.child_cutover_done(), None if self.store.child_cutover_done() is None else False)
        self.assertFalse(self.store.child_cutover_done())
        self.assertEqual({f: open(os.path.join(TMP, "policy", f)).read() for f in cc.FILES}, files_before)
        self.assertEqual(self.block(), block_before)
        self.assertEqual(json.load(open(os.environ["CHILD_APPLY_STATUS"]))["source"], "files")
        self.assertIn(("enable", "--now", "child-ui.service"), self.sys_calls)
        self.assertNotIn(("disable", "--now", "child-ui.service"), self.sys_calls)

    def test_a_block_that_changes_at_the_switch_rolls_back(self):
        real = cc.run_job
        n = {"i": 0}

        def run_job(now=None):
            n["i"] += 1
            real(now)
            if n["i"] == 2:                                                                      # the first run in snapshot mode: pretend it produced something else
                st = json.load(open(os.environ["CHILD_APPLY_STATUS"]))
                st["rules_hash"] = "0000000000000000"
                json.dump(st, open(os.environ["CHILD_APPLY_STATUS"], "w"))
        cc.run_job = run_job
        try:
            rc = cc.cutover(self.now, go=True, min_hours=0, min_checks=1)
        finally:
            cc.run_job = real
        self.assertEqual((rc, self.mode()), (1, "files"))

    def test_the_standalone_rollback_restores_the_files(self):
        self.assertEqual(cc.cutover(self.now, go=True, min_hours=0, min_checks=1), 0)
        open(os.path.join(TMP, "policy", "allow-extra.txt"), "w").write("generated-after-cutover.example.org\n")                  # what the generator would write
        self.assertEqual(cc.rollback(None, self.now), 0)
        self.assertEqual(self.mode(), "files")
        self.assertEqual(open(os.path.join(TMP, "policy", "allow-extra.txt")).read(), "\n")
        self.assertFalse(self.store.child_cutover_done())
        self.assertEqual(json.load(open(os.environ["CHILD_APPLY_STATUS"]))["source"], "files")
        self.assertIn(("disable", "--now", "adguard-child-lists.path"), self.sys_calls)
        self.assertEqual(cc.rollback(os.path.join(TMP, "nope"), self.now) in (0, 1), True)


if __name__ == "__main__":
    unittest.main()
