"""Test doubles for AdGuard Home (Task 29 Part 2): a fake control API with real HTTP and basic auth, and a fake DNS for the probes."""
import base64
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
        self.stats = {"top_queried_domains": [], "top_blocked_domains": []}
        self.down = False
        self.reject_add = set()                 # urls for which add_url answers 400
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
        return self.next_id

    def blocking_ids(self, name):
        return [f["id"] for f in self.filters if f["enabled"] and any(name == d or name.endswith("." + d) for d in self.active.get(f["url"], []))]

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
            return JSONResponse({"added": len(self.contents[body["url"]])})
        if path == "/filtering/remove_url":
            self.filters = [f for f in self.filters if f["url"] != body["url"]]
            return JSONResponse({})
        if path == "/filtering/set_url":
            for f in self.filters:
                if f["url"] == body["url"]:
                    f["name"], f["enabled"] = body["data"]["name"], body["data"]["enabled"]
            return JSONResponse({})
        if path == "/filtering/refresh":
            n = 0
            for f in self.filters:
                if f["enabled"] and f["url"] in self.contents:
                    self.active[f["url"]] = list(self.contents[f["url"]])
                    f["rules_count"] = len(self.contents[f["url"]])
                    n += 1
            return JSONResponse({"updated": n})
        if path == "/blocked_services/all":
            return JSONResponse({"blocked_services": [{"id": k, "rules": v} for k, v in self.service_rules.items()]})
        if path == "/clients":
            return JSONResponse({"clients": [], "auto_clients": self.auto_clients})
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
        return {"rcode": 0, "addrs": ["0.0.0.0"] if blocked else ["93.184.216.34"]}
