"""Test doubles: a fake Home Assistant (REST + WebSocket subscribe_entities), the REAL schedule service run in-process, and the gateway env."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import datetime as dt

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHED_SRC = os.environ.get("SCHED_SRC", "/srv/compose/portal/schedules")
sys.path.insert(0, ROOT)

HA_TOKEN = "test-ha-token-0123456789"
SWITCHES = ["switch.bathroom_towels", "switch.living_room_rack", "switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver", "switch.bedroom_parrot"]
POWER = ["sensor.bathroom_towels_power", "sensor.living_room_rack_power", "sensor.livingroom_rack_big_power", "sensor.minisserver_power", "sensor.bedroom_parrot_power"]
VAC = ["vacuum.xiaomi_ov51gl_e160_robot_cleaner", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner"]
SCRIPTS = ["script.vacuum_bedroom_plan", "script.vacuum_livingroom_plan"]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerThread:
    def __init__(self, app, port, lifespan="off"):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan=lifespan))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


class FakeHA:
    """REST: GET /api/states/{id}, POST /api/services/{domain}/{service}. WS: /api/websocket (auth, subscribe_entities). Behaviour switches for tests."""
    def __init__(self):
        now = time.time()
        self.states = {}
        for e in SWITCHES:
            self.states[e] = {"s": "off", "a": {"friendly_name": e}, "lc": now, "lu": now}
        for e in POWER:
            self.states[e] = {"s": "0.0", "a": {}, "lc": now, "lu": now}
        for e in VAC:
            self.states[e] = {"s": "docked", "a": {"battery_level": 100}, "lc": now, "lu": now}
        for e in SCRIPTS:
            self.states[e] = {"s": "off", "a": {}, "lc": now, "lu": now}
        self.calls = []
        self.ws_clients = []
        self.swallow = set()            # entities whose service calls are accepted but have no effect (-> unconfirmed)
        self.refuse = False             # REST answers 500 / ws closes
        self.down = False               # everything answers 503 and the socket is refused
        self.no_ws = False              # REST works, WebSocket upgrade refused (-> polling fallback)
        self.loop = None
        port = free_port()
        self.port = port
        self.url = "http://127.0.0.1:%d" % port
        self.app = Starlette(routes=[
            Route("/api/states/{eid}", self.get_state, methods=["GET"]),
            Route("/api/services/{domain}/{service}", self.call, methods=["POST"]),
            WebSocketRoute("/api/websocket", self.ws),
        ])
        self.srv = ServerThread(self.app, port)

    def start(self):
        self.srv.start()
        return self

    def stop(self):
        self.srv.stop()

    def _auth_ok(self, request):
        return request.headers.get("authorization") == "Bearer " + HA_TOKEN

    async def get_state(self, request):
        if self.down or self.refuse:
            return JSONResponse({}, status_code=503)
        if not self._auth_ok(request):
            return JSONResponse({"message": "unauthorized"}, status_code=401)
        eid = request.path_params["eid"]
        s = self.states.get(eid)
        if not s:
            return JSONResponse({"message": "not found"}, status_code=404)
        return JSONResponse({"entity_id": eid, "state": s["s"], "attributes": s["a"],
                             "last_changed": dt.datetime.fromtimestamp(s["lc"], dt.timezone.utc).isoformat(), "last_updated": dt.datetime.fromtimestamp(s["lu"], dt.timezone.utc).isoformat()})

    async def call(self, request):
        if self.down or self.refuse:
            return JSONResponse({}, status_code=503)
        if not self._auth_ok(request):
            return JSONResponse({"message": "unauthorized"}, status_code=401)
        domain, service = request.path_params["domain"], request.path_params["service"]
        body = await request.json()
        eid = body.get("entity_id")
        self.calls.append((domain, service, eid, {k: v for k, v in body.items() if k != "entity_id"}))
        if eid in self.swallow:
            return JSONResponse([])
        if domain == "switch" and service in ("turn_on", "turn_off"):
            self.set(eid, "on" if service == "turn_on" else "off")
        elif domain == "script" and service == "turn_on":
            self.set(eid, "on")
        elif domain == "script" and service == "turn_off":
            self.set(eid, "off")
        elif domain == "vacuum" and service == "start":
            self.set(eid, "cleaning")
        elif domain == "vacuum" and service == "pause":
            self.set(eid, "paused")
        elif domain == "vacuum" and service == "return_to_base":
            self.set(eid, "returning")
        return JSONResponse([])

    def set(self, eid, state, attrs=None):
        """Change a state and push the diff to every subscribed websocket (the way HA does)."""
        now = time.time()
        cur = self.states[eid]
        changed = cur["s"] != state
        cur["s"] = state
        if attrs:
            cur["a"].update(attrs)
        cur["lu"] = now
        if changed:
            cur["lc"] = now
        diff = {"+": {"s": state, "lc": cur["lc"], "lu": now}}
        if attrs:
            diff["+"]["a"] = attrs
        for q, ids in list(self.ws_clients):
            if eid in ids and self.loop:
                self.loop.call_soon_threadsafe(q.put_nowait, {"c": {eid: diff}})

    async def ws(self, websocket):
        self.loop = asyncio.get_running_loop()
        if self.down or self.no_ws:
            await websocket.close(code=1013)
            return
        await websocket.accept()
        await websocket.send_json({"type": "auth_required", "ha_version": "test"})
        msg = await websocket.receive_json()
        if msg.get("access_token") != HA_TOKEN:
            await websocket.send_json({"type": "auth_invalid"})
            await websocket.close()
            return
        await websocket.send_json({"type": "auth_ok"})
        q = asyncio.Queue()
        entry = None
        try:
            while True:
                recv = asyncio.ensure_future(websocket.receive_json())
                get = asyncio.ensure_future(q.get())
                done, pending = await asyncio.wait({recv, get}, return_when=asyncio.FIRST_COMPLETED, timeout=0.3)
                for p in pending:
                    p.cancel()
                if self.down or self.refuse:
                    await websocket.close(code=1011)
                    return
                if recv in done:
                    m = recv.result()
                    if m.get("type") == "subscribe_entities":
                        ids = set(m["entity_ids"])
                        entry = (q, ids)
                        self.ws_clients.append(entry)
                        await websocket.send_json({"id": m["id"], "type": "result", "success": True, "result": None})
                        added = {e: {"s": self.states[e]["s"], "a": self.states[e]["a"], "c": {}, "lc": self.states[e]["lc"], "lu": self.states[e]["lu"]} for e in ids if e in self.states}
                        await websocket.send_json({"id": m["id"], "type": "event", "event": {"a": added}})
                if get in done:
                    await websocket.send_json({"id": 1, "type": "event", "event": get.result()})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if entry in self.ws_clients:
                self.ws_clients.remove(entry)


class SchedHarness:
    """The REAL schedule service (Task 27, with X-Acting-User) running in-process on a free port, seeded, with two gateway tokens."""
    def __init__(self, tmp):
        if SCHED_SRC not in sys.path:
            sys.path.insert(0, SCHED_SRC)
        from sched import store as storemod, api as sapi
        self.storemod = storemod
        self.dir = os.path.join(tmp, "sched")
        os.makedirs(self.dir)
        self.store = storemod.Store(self.dir)
        self.store.seed(os.path.join(SCHED_SRC, "seed", "initial.json"), {"token": "-", "name": "seed", "role": "admin"})
        self.sys = {"token": "-", "name": "harness", "role": "admin"}
        _i, self.admin_secret = self.store.create_token(self.sys, "gw-admin", "admin", gateway=True)
        _i, self.house_secret = self.store.create_token(self.sys, "gw-household", "household", gateway=True)
        _i, self.plain_house_secret = self.store.create_token(self.sys, "plain-household", "household")
        self.admin_file = os.path.join(tmp, "sched-admin")
        self.house_file = os.path.join(tmp, "sched-household")
        for f, s in ((self.admin_file, self.admin_secret), (self.house_file, self.house_secret)):
            with open(f, "w") as fh:
                fh.write(s + "\n")
            os.chmod(f, 0o600)
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.srv = ServerThread(sapi.build(self.store, background=False), self.port)

    def start(self):
        self.srv.start()
        return self

    def stop(self):
        self.srv.stop()


class Clock:
    """Controllable wall clock for the people/auth tests."""
    def __init__(self, t=1_792_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s
