"""Test environment: gateway app + fake HA + the real schedule service, device tokens for three synthetic people, contract validation."""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import yaml
import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import httpx  # noqa: E402
from gw import server, auth  # noqa: E402
from fakes import FakeHA, SchedHarness, ServerThread, HA_TOKEN, free_port  # noqa: E402
from fakeagh import FakeAgh, FakeDns, AGH_PASSWORD  # noqa: E402

SPEC = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml"), encoding="utf-8"))
PASSWORD = "correct horse battery staple 42"
SEEN = set()          # operationIds exercised through validated requests (test_zz_contract checks that the whole spec was covered)


def _rx(template):
    return re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$")


def validate_response(method, path, resp):
    """Every JSON response must be a documented status code and match its schema (SSE and HTML pages are not part of the JSON contract)."""
    if not path.startswith("/v1/") or path == "/v1/events":
        return
    op = None
    for tmpl, item in SPEC["paths"].items():
        if _rx(tmpl.replace("{rest}", "x")).match(path) or _rx(tmpl).match(path):
            if method.lower() in item:
                op = item[method.lower()]
                break
    assert op, "no operation in openapi.yaml for %s %s" % (method, path)
    code = str(resp.status_code)
    assert code in op["responses"], "%s %s -> %s is not documented (documented: %s) body=%s" % (method, path, code, sorted(op["responses"]), resp.text[:200])
    SEEN.add(op["operationId"])
    spec = op["responses"][code]
    if "$ref" in spec:
        spec = SPEC["components"]["responses"][spec["$ref"].split("/")[-1]]
    content = spec.get("content", {}).get("application/json")
    if content is None:
        assert not resp.content, "%s %s -> %s should have no body" % (method, path, code)
        return
    schema = dict(content["schema"])
    schema["components"] = SPEC["components"]
    jsonschema.Draft202012Validator(schema).validate(resp.json())
    for h in spec.get("headers", {}):
        assert h in resp.headers, "%s %s -> %s lacks header %s" % (method, path, code, h)


class Env:
    def __init__(self, background=True, probes=False):
        self.tmp = tempfile.mkdtemp(prefix="gw-test-")
        self.ha = FakeHA().start()
        self.sched = SchedHarness(self.tmp).start()
        self.ha_token = os.path.join(self.tmp, "ha-token")
        with open(self.ha_token, "w") as f:
            f.write(HA_TOKEN + "\n")
        os.chmod(self.ha_token, 0o600)
        self.data = os.path.join(self.tmp, "data")
        # AdGuard (Task 29 Part 2): a fake control API + fake DNS, a parity-state file the "checker" touches while parity_runs is set, a tiny OUI file
        self.agh = FakeAgh(self.tmp).start()
        self.agh.child_ips = {"192.168.31.60", "192.168.31.61"}
        self.dns = FakeDns(self.agh)
        self.parity = os.path.join(self.tmp, "parity-state.json")
        self.parity_streak = {}
        self.parity_runs = True
        self._write_parity(time.time() - 600)
        self.oui = os.path.join(self.tmp, "oui.txt")
        with open(self.oui, "w") as f:
            f.write("20-36-D0   (hex)\t\tTest Vendor Inc\n")
        self.fetch_contents = {}                            # url -> text the gateway's own validation download returns (missing = unreachable)

        def fetch(url):
            from gw.filterlists import Refused
            if url not in self.fetch_contents:
                raise Refused(422, "unreachable", "the list could not be downloaded")
            return self.fetch_contents[url]

        async def sleep(_s):
            if self.parity_runs:                            # the 5-minute checker "runs" while the gateway waits
                self._write_parity(time.time())
            await asyncio.sleep(0.01)
        self.app = server.make(data_dir=self.data, ha_url=self.ha.url, ha_token_file=self.ha_token, sched_url=self.sched.url,
                               sched_admin=self.sched.admin_file, sched_household=self.sched.house_file, start_background=background,
                               agh_url=self.agh.url, agh_password_file=self.agh.password_file, parity_state=self.parity, oui_file=self.oui,
                               filter_opts={"fetch": fetch, "dns": self.dns, "sleep": sleep, "poll_s": 0.01, "replica_wait_s": 0.3, "parity_wait_s": 0.5})
        self.o = self.app.state.objs
        self.db, self.people, self.core, self.hub, self.haclient = self.o["db"], self.o["people"], self.o["core"], self.o["hub"], self.o["ha"]
        if not probes:                                       # probes would talk to the real network of the machine running the tests
            self.db.x("DELETE FROM service_tiles")
            self.db.x("INSERT INTO service_tiles VALUES ('fakeweb','Fake web','plug','https://example.invalid','http:%s/api/states/switch.living_room_rack','household',0)" % self.ha.url)
            self.db.x("INSERT INTO service_tiles VALUES ('adminonly','Admin only','shield','https://example.invalid/a','dns:127.0.0.1','admin',1)")
            self.db.x("INSERT INTO service_tiles VALUES ('hasvc','HA link','home_assistant','https://example.invalid/h','internal:ha','admin',2)")
            self.db.x("INSERT INTO service_tiles VALUES ('schedsvc','Schedules','schedule','https://example.invalid/s','internal:schedules','admin',3)")
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.srv = ServerThread(self.app, self.port, lifespan="on")      # a real server: SSE streams and the lifespan (HA link) work as in production
        self.srv.start()
        self.client = httpx.Client(base_url=self.base, timeout=20)
        self.people_ids = {}
        self.tokens = {}
        self._make_people()
        self.wait_link("ws")

    # ── people with device tokens ──
    def _make_people(self):
        a = {"person_id": "test", "phone_id": None, "ip": "test"}
        for name, role, parent in (("Ivan", "admin", True), ("Dasha", "household", True), ("Ola", "readonly", False)):
            self.people.create(a, name, role, parent)
            code, _ = self.people.create_enrolment_code(a, name.lower())
            res = self.people.enrol(code, {"label": name + "-phone", "model": "Test"}, "10.0.0.1")
            self.tokens[name.lower()] = res["token"]
            self.people_ids[name.lower()] = res["phone_id"]

    def wait_link(self, want, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if self.haclient.link == want and self.core.devices:
                return True
            time.sleep(0.05)
        raise AssertionError("HA link %s not reached (is %s)" % (want, self.haclient.link))

    def wait(self, cond, timeout=6):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def req(self, method, path, who="ivan", token=None, validate=True, idem=True, **kw):
        headers = dict(kw.pop("headers", {}))
        tok = token if token is not None else self.tokens.get(who)
        if tok:
            headers["Authorization"] = "Bearer " + tok
        if idem and method in ("POST", "PUT", "PATCH", "DELETE") and "Idempotency-Key" not in headers:
            headers["Idempotency-Key"] = "k-" + auth.token(8)
        r = self.client.request(method, path, headers=headers, **kw)
        if validate:
            validate_response(method, path.split("?")[0], r)
        return r

    def run(self, coro, timeout=20):
        """Run a coroutine on the server's own event loop (the one the HA link and the probes live on)."""
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.core.loop).result(timeout)

    def assert_no_secrets(self):
        """Secret hygiene: no token, password, HA/service secret or TOTP seed in the audit mirror; no raw token/password/HA secret in the database files."""
        secrets = list(self.tokens.values()) + [HA_TOKEN, self.sched.admin_secret, self.sched.house_secret, self.sched.plain_house_secret, PASSWORD]
        seeds = list(getattr(self, "seeds", {}).values())
        blobs = {"audit.jsonl": open(self.db.jsonl, "rb").read()}
        db_bytes = b""
        for suffix in ("", "-wal"):
            p = os.path.join(self.data, "gateway.db" + suffix)
            if os.path.exists(p):
                db_bytes += open(p, "rb").read()
        blobs["gateway.db"] = db_bytes
        for name, blob in blobs.items():
            for s in secrets + (seeds if name == "audit.jsonl" else []):
                assert s.encode() not in blob, "a secret appears in %s" % name

    def _write_parity(self, mtime):
        with open(self.parity, "w") as f:
            json.dump({"threat_domain": "babyglee-all.xyz", "streak": self.parity_streak, "alerted": []}, f)
        os.utime(self.parity, (mtime, mtime))

    def close(self):
        self.assert_no_secrets()
        self.client.close()
        self.srv.stop()
        self.ha.stop()
        self.sched.stop()
        self.agh.stop()


import unittest  # noqa: E402


class BaseCase(unittest.TestCase):
    """Shared by the test modules: one Env per module (set in setUpModule), every test starts from a known fake-HA state."""
    ENV = None

    @property
    def e(self):
        return BaseCase.ENV

    def setUp(self):
        e = self.e
        e.ha.swallow.clear()
        e.ha.refuse = e.ha.down = e.ha.no_ws = False
        for s in ("switch.bathroom_towels", "switch.living_room_rack", "switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver", "switch.bedroom_parrot"):
            e.ha.set(s, "off")
        for v in ("vacuum.xiaomi_ov51gl_e160_robot_cleaner", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner"):
            e.ha.set(v, "docked")
        for sc in ("script.vacuum_bedroom_plan", "script.vacuum_livingroom_plan"):
            e.ha.set(sc, "off")
        e.core._last_switch.clear()
        e.core._eff.clear()
        want = {"switch.bathroom_towels": "off", "switch.living_room_rack": "off", "switch.livingroom_rack_big": "off", "switch.kitchen_orchids": "off",
                "switch.minisserver": "off", "switch.bedroom_parrot": "off", "vacuum.xiaomi_ov51gl_e160_robot_cleaner": "docked",
                "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner": "docked", "script.vacuum_bedroom_plan": "off", "script.vacuum_livingroom_plan": "off"}
        self.assertTrue(e.wait(lambda: all(e.haclient.state(k) == v for k, v in want.items()), 10), "fake HA state did not settle")
        e.ha.calls.clear()

    def audit_actions(self, n=10):
        return [(r["action"], r["person"], r["device"], r["target"], r["ok"]) for r in self.e.db.audit_rows(n)]


class Stream:
    """Reads /v1/events in a thread; collects (id, event, data) and raw lines."""
    def __init__(self, env, token, last_id=None, path="/v1/events"):
        import threading
        self.events, self.lines, self.status, self.done = [], [], None, threading.Event()
        headers = {"Authorization": "Bearer " + token}
        if last_id is not None:
            headers["Last-Event-ID"] = str(last_id)
        self.t = threading.Thread(target=self._run, args=(env, headers, path), daemon=True)
        self.t.start()

    def _run(self, env, headers, path):
        import json as _json
        cur = {}
        try:
            with httpx.Client(base_url=env.base, timeout=httpx.Timeout(30, read=30)) as c:
                with c.stream("GET", path, headers=headers) as r:
                    self.status = r.status_code
                    if r.status_code == 200:
                        SEEN.add("events")
                    for line in r.iter_lines():
                        self.lines.append(line)
                        if line.startswith("id:"):
                            cur["id"] = int(line[3:].strip())
                        elif line.startswith("event:"):
                            cur["event"] = line[6:].strip()
                        elif line.startswith("data:"):
                            cur["data"] = _json.loads(line[5:].strip())
                        elif line == "" and cur.get("event"):
                            self.events.append((cur.get("id"), cur["event"], cur.get("data")))
                            cur = {}
        except Exception:                                      # noqa: BLE001 - a closed stream ends the thread
            pass
        finally:
            self.done.set()

    def wait_event(self, pred, timeout=6):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            for ev in list(self.events):
                if pred(ev):
                    return ev
            _t.sleep(0.05)
        return None
