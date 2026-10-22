"""Test doubles for AdGuard Home (Task 29 Part 2): a fake control API with real HTTP and basic auth, and a fake DNS for the probes."""
import base64
import time
import json
import os

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from fakes import ServerThread, free_port

AGH_PASSWORD = "agh-test-password-0123456789"
ALWAYS_ON = ["api.telegram.org", "t.me", "kidsmanagement-pa.googleapis.com", "www.google.com"]


class FakeAgh:
    """The parts of the AdGuard control API the gateway uses. Lists have CONTENT (domains), so check_host answers like the real engine: Filtered, with the id of the
    blocking list. `client` + `child_ips` model the per-child catch-all (everything blocked except the always-on hosts)."""
    def __init__(self, tmp):
        self.password_file = os.path.join(tmp, "agh-password")
        with open(self.password_file, "w") as f:
            f.write(AGH_PASSWORD + "\n")
        os.chmod(self.password_file, 0o600)
        self.next_id = 100
        self.filters = []                       # id, name, url, enabled, rules_count, last_updated
        self.contents = {}                      # url -> [domains]: what the list serves NOW
        self.active = {}                        # url -> [domains]: what AdGuard has loaded (set at add and at refresh)
        self.user_rules = []
        self.child_ips = set()
        self.querylog = []                      # newest first: time, client, question{name,type}, reason
        self.auto_clients = []
        self.clients = []                       # configured clients (name, ids, use_global_settings, safe_search): what the host job creates for each child
        self.stats = {"top_queried_domains": [], "top_blocked_domains": []}
        self.down = False
        self.reject_add = set()                 # urls for which add_url answers 400
        self.blocking_mode = "default"
        self.lag = 0.0                          # seconds the filtering ENGINE needs to take a list change in (real AdGuard: about a second)
        self.engine = []                        # (id, enabled, [domains]) as the engine currently sees it
        self._engine_due = None
        self.calls = []
        self.service_rules = {"telegram": ["||t.me^", "||telegram.org^"], "youtube": ["||youtube.com^"], "roblox": ["||roblox.com^"]}
        port = free_port()
        self.url = "http://127.0.0.1:%d/control" % port
        self.app = Starlette(routes=[Route("/control/{rest:path}", self.handle, methods=["GET", "POST"])])
        self.srv = ServerThread(self.app, port)

    def start(self):
        self.srv.start()
        return self

    def stop(self):
        self.srv.stop()

    def add_list(self, name, url, domains, enabled=True):
        self.next_id += 1
        self.contents[url] = list(domains)
        self.active[url] = list(domains)
        self.filters.append({"id": self.next_id, "name": name, "url": url, "enabled": enabled, "rules_count": len(domains), "last_updated": "2026-10-20T03:00:00Z"})
        self.sync_engine()
        return self.next_id

    def blocking_ids(self, name):
        return [i for i, en, doms in self.engine if en and any(name == d or name.endswith("." + d) for d in doms)]

    def sync_engine(self):
        self.engine = [(f["id"], f["enabled"], list(self.active.get(f["url"], []))) for f in self.filters]
        self._engine_due = None

    def _changed(self):
        if self.lag <= 0:
            self.sync_engine()
        elif self._engine_due is None:
            self._engine_due = __import__("time").time() + self.lag

    def _tick(self):
        if self._engine_due is not None and __import__("time").time() >= self._engine_due:
            self.sync_engine()

    def check(self, name, client=None):
        if client and client in self.child_ips:
            if name in ALWAYS_ON:
                rule = "@@||%s^$client=%s" % (name, client)
                return {"reason": "NotFilteredWhiteList", "rule": rule, "rules": [{"text": rule, "filter_list_id": 0}]}
            rule = "||*^$client=" + client
            return {"reason": "FilteredBlackList", "rule": rule, "rules": [{"text": rule, "filter_list_id": 0}]}
        ids = self.blocking_ids(name)
        if ids:
            return {"reason": "FilteredBlackList", "rule": "||%s^" % name, "rules": [{"text": "||%s^" % name, "filter_list_id": i} for i in ids]}
        return {"reason": "NotFilteredNotFound", "rules": []}

    async def handle(self, request):
        self.calls.append((request.method, request.url.path, request.url.query))
        self._tick()
        if self.down:
            return JSONResponse({}, status_code=503)
        want = "Basic " + base64.b64encode(("admin:%s" % AGH_PASSWORD).encode()).decode()
        if request.headers.get("authorization") != want:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        path = "/" + request.path_params["rest"]
        q = request.query_params
        body = {}
        if request.method == "POST":
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        if path == "/status":
            return JSONResponse({"running": True, "version": "v0.107.test"})
        if path == "/filtering/status":
            return JSONResponse({"enabled": True, "interval": 24, "filters": [dict(f) for f in self.filters], "whitelist_filters": [], "user_rules": list(self.user_rules)})
        if path == "/filtering/check_host":
            return JSONResponse(self.check(q.get("name", ""), q.get("client")))
        if path == "/filtering/add_url":
            if body["url"] in self.reject_add or body["url"] not in self.contents:
                return JSONResponse({"error": "data from URL is not valid"}, status_code=400)
            if any(f["url"] == body["url"] for f in self.filters):
                return JSONResponse({"error": "filter already exists"}, status_code=400)
            self.next_id += 1
            self.active[body["url"]] = list(self.contents[body["url"]])
            self.filters.append({"id": self.next_id, "name": body["name"], "url": body["url"], "enabled": True, "rules_count": len(self.contents[body["url"]]),
                                 "last_updated": "2026-10-20T20:00:00Z"})
            self._changed()
            return JSONResponse({"added": len(self.contents[body["url"]])})
        if path == "/filtering/remove_url":
            self.filters = [f for f in self.filters if f["url"] != body["url"]]
            self._changed()
            return JSONResponse({})
        if path == "/filtering/set_url":
            for f in self.filters:
                if f["url"] == body["url"]:
                    f["name"], f["enabled"] = body["data"]["name"], body["data"]["enabled"]
            self._changed()
            return JSONResponse({})
        if path == "/dns_info":
            return JSONResponse({"blocking_mode": self.blocking_mode})
        if path == "/filtering/refresh":
            n = 0
            for f in self.filters:
                if f["enabled"] and f["url"] in self.contents:
                    self.active[f["url"]] = list(self.contents[f["url"]])
                    f["rules_count"] = len(self.contents[f["url"]])
                    n += 1
            self._changed()
            return JSONResponse({"updated": n})
        if path == "/blocked_services/all":
            return JSONResponse({"blocked_services": [{"id": k, "rules": v} for k, v in self.service_rules.items()]})
        if path == "/clients":
            return JSONResponse({"clients": [dict(c) for c in self.clients], "auto_clients": self.auto_clients})
        if path == "/clients/delete":
            self.clients = [c for c in self.clients if c["name"] != body.get("name")]
            return JSONResponse({})
        if path == "/stats":
            return JSONResponse(self.stats)
        if path == "/querylog":
            limit = int(q.get("limit", "500"))
            search = q.get("search")
            older = q.get("older_than")
            rows = [e for e in self.querylog if (not search or search in e["client"] or search in e["question"]["name"]) and (not older or e["time"] < older)]
            page = rows[:limit]
            return JSONResponse({"data": page, "oldest": page[-1]["time"] if len(page) == limit else ""})
        return JSONResponse({"error": "no such call in the fake: " + path}, status_code=404)


class FakeDns:
    """dns(server, name) for the probes: the origin answers from the fake AdGuard's state; the replica from a copy that follows only while `follow` is set."""
    def __init__(self, agh, origin="192.168.31.2", replica="192.168.31.5"):
        self.agh, self.origin, self.replica = agh, origin, replica
        self.follow = True
        self.replica_state = None
        self.down = set()

    def sync_replica(self):
        self.replica_state = ({f["url"]: f["enabled"] for f in self.agh.filters}, {u: list(d) for u, d in self.agh.active.items()})

    def __call__(self, server, name):
        if server in self.down:
            raise OSError("no answer")
        if server == self.replica:
            if self.follow or self.replica_state is None:
                self.sync_replica()
            enabled, contents = self.replica_state
            blocked = any(en and any(name == d or name.endswith("." + d) for d in contents.get(u, [])) for u, en in enabled.items())
        else:
            blocked = bool(self.agh.blocking_ids(name))
        if self.agh.blocking_mode == "nxdomain" and blocked:
            return {"rcode": 3, "addrs": []}
        if blocked:
            return {"rcode": 0, "addrs": ["0.0.0.0"]}
        if name.endswith(".invalid"):                              # a name that does not exist: NXDOMAIN whether or not a list mentions it in the default mode
            return {"rcode": 3, "addrs": []}
        return {"rcode": 0, "addrs": ["93.184.216.34"]}


class FakeJob:
    """The host policy job in miniature: every `period` seconds it renders the REAL service's policy with the REAL renderer into the fake AdGuard's user_rules, makes the
    child IPs catch-all (check_host), and creates a client entry per device (the real job never deletes one: that is the gap the portal now closes)."""
    def __init__(self, env, period=0.15):
        import threading
        self.env, self.period = env, period
        self.enabled, self.stop_flag = True, False
        self.client_flags = {"use_global_settings": False, "safe_search": {"enabled": True, "google": True, "youtube": True}}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_flag = True
        self.thread.join(timeout=3)

    def run(self):
        while not self.stop_flag:
            if self.enabled:
                try:
                    self.apply()
                except Exception:                                  # noqa: BLE001 - the store may be mid-write
                    pass
            time.sleep(self.period)

    def apply(self):
        import datetime as dt
        from sched import child_render, model
        store, agh = self.env.sched.store, self.env.agh
        pol = store.child_policy()
        now = self.env.now_fn() if getattr(self.env, "now_fn", None) else dt.datetime.now(model.TZ)
        sd = {k: [r[2:-1] for r in v] for k, v in agh.service_rules.items()}
        ips = {d["ip"]: d for d in pol["devices"]}
        ov = [{"client": ips_by_id, "target": x["target"]} for x in store.child_exceptions() for ips_by_id in [next((d["ip"] for d in pol["devices"] if d["id"] == x["entity"]), None)] if ips_by_id]
        block = child_render.render_block(pol, sd, now, ov)
        agh.user_rules = ["||other.example.org^"] + block
        agh.child_ips = set(ips)
        for d in pol["devices"]:
            if not any(c["name"] == d["name"] for c in agh.clients):
                agh.clients.append({"name": d["name"], "ids": [d["ip"]], **json.loads(json.dumps(self.client_flags))})
