"""The gateway's link to Home Assistant: a state cache fed by ONE WebSocket subscription (subscribe_entities, filtered to the allow-list),
a polling fallback when the socket cannot be kept, service calls through REST, and read-back waits.

Least privilege: the token belongs to a dedicated NON-admin HA user, and on top of that this module refuses (GwError 403 `not_allowed`) anything
outside the allow-list of entity ids and (domain, service) pairs, so a bug here cannot reach another device.
The token is read from a mode-600 file at every (re)connect, is never logged and never appears in an error message."""
import asyncio
import json
import time
import datetime as dt
import urllib.error
import urllib.request
from . import config
from .auth import GwError


def parse_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return dt.datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


class HA:
    def __init__(self, url, token_file, hub=None, clock=time.time, allowed_services=None):
        self.url = url.rstrip("/")
        self.token_file = token_file
        self.hub = hub
        self.clock = clock
        self.allowed_services = allowed_services or config.HA_ALLOWED_SERVICES
        self.watch = set()               # entity ids we read (states)
        self.controllable = set()        # entity ids we may CALL a service on (subset policy: domain/service pairs above)
        self.cache = {}
        self.link = "down"               # ws | poll | down
        self.last_event = None
        self.last_ok = None
        self._tasks = []
        self._stop = False
        self._ws_up = asyncio.Event()
        self.on_change = None            # callable(eid, old, new)

    # ── configuration of what may be touched ──
    def set_scope(self, watch, controllable):
        self.watch = set(watch)
        self.controllable = set(controllable)
        self._resub = True

    def _token(self):
        with open(self.token_file, encoding="utf-8") as f:
            return f.read().strip()

    # ── REST ──
    def _http_sync(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return None, None
        except Exception:                      # noqa: BLE001 - never let the token leak through an exception text
            return None, None

    async def http(self, method, path, body=None):
        return await asyncio.to_thread(self._http_sync, method, path, body)

    # ── cache ──
    def _set(self, eid, state, attrs, changed, updated):
        old = self.cache.get(eid)
        new = {"state": state, "attrs": attrs or {}, "changed": changed, "updated": updated, "seen": self.clock()}
        self.cache[eid] = new
        self.last_event = self.clock()
        if self.on_change and (old is None or old["state"] != state or old["attrs"] != new["attrs"]):
            self.on_change(eid, old, new)

    def get(self, eid):
        return self.cache.get(eid)

    def state(self, eid):
        c = self.cache.get(eid)
        return c["state"] if c else None

    async def refresh(self, eid):
        st, js = await self.http("GET", "/api/states/" + eid)
        if st == 200 and js:
            self.last_ok = self.clock()
            self._set(eid, js.get("state"), js.get("attributes"), parse_ts(js.get("last_changed")), parse_ts(js.get("last_updated")))
            return True
        if st == 404:
            self.last_ok = self.clock()
            return False
        return None

    async def refresh_all(self):
        ok = 0
        for eid in sorted(self.watch):
            if await self.refresh(eid):
                ok += 1
        return ok

    def stale(self):
        return self.link == "down"

    def _set_link(self, new):
        if new != self.link:
            old, self.link = self.link, new
            if self.hub:
                self.hub.publish("link", {"ha": new, "was": old})

    # ── subscribe_entities decoding ──
    def apply_entities_event(self, ev):
        for eid, s in (ev.get("a") or {}).items():
            if eid in self.watch:
                self._set(eid, s.get("s"), s.get("a"), parse_ts(s.get("lc")), parse_ts(s.get("lu")))
        for eid, diff in (ev.get("c") or {}).items():
            if eid not in self.watch:
                continue
            cur = self.cache.get(eid) or {"state": None, "attrs": {}, "changed": None, "updated": None}
            state, attrs, lc, lu = cur["state"], dict(cur["attrs"]), cur["changed"], cur["updated"]
            plus = diff.get("+") or {}
            if "s" in plus:
                state = plus["s"]
            if "a" in plus:
                attrs.update(plus["a"])
            if "lc" in plus:
                lc = parse_ts(plus["lc"])
            if "lu" in plus:
                lu = parse_ts(plus["lu"])
            for k in (diff.get("-") or {}).get("a", []):
                attrs.pop(k, None)
            self._set(eid, state, attrs, lc, lu)
        for eid in ev.get("r") or []:
            if eid in self.cache:
                self._set(eid, "unavailable", {}, self.clock(), self.clock())

    async def _ws_once(self):
        import websockets
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        async with websockets.connect(ws_url, max_size=4 * 1024 * 1024, open_timeout=8, ping_interval=20, ping_timeout=20) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if msg.get("type") != "auth_required":
                raise ConnectionError("unexpected greeting")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token()}))
            msg = json.loads(await asyncio.wait_for(ws.recv(), 8))
            if msg.get("type") != "auth_ok":
                raise ConnectionError("authentication refused")
            await ws.send(json.dumps({"id": 1, "type": "subscribe_entities", "entity_ids": sorted(self.watch)}))
            self._resub = False
            subscribed = False
            while not self._stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), 5)
                except asyncio.TimeoutError:
                    if getattr(self, "_resub", False):
                        return                                      # the allow-list changed: reconnect with the new filter
                    continue
                m = json.loads(raw)
                if m.get("type") == "result" and m.get("id") == 1:
                    if not m.get("success"):
                        raise ConnectionError("subscription refused")
                    subscribed = True
                    self.last_ok = self.clock()
                    await self.refresh_all()                        # one REST pass so the cache is complete before we claim 'ws'
                    self._set_link("ws")
                    self._ws_up.set()
                elif m.get("type") == "event" and m.get("id") == 1 and subscribed:
                    self.last_ok = self.clock()
                    self.apply_entities_event(m.get("event") or {})

    async def ws_loop(self):
        backoff = 1.0
        while not self._stop:
            try:
                await self._ws_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:                                      # noqa: BLE001
                pass
            self._ws_up.clear()
            if self.link == "ws":
                self._set_link("poll")
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)

    async def poll_loop(self):
        while not self._stop:
            await asyncio.sleep(5)
            if self._ws_up.is_set():
                continue
            ok = await self.refresh_all()
            if ok:
                self._set_link("poll")
            else:
                self._set_link("down")

    async def start(self):
        self._tasks = [asyncio.create_task(self.ws_loop()), asyncio.create_task(self.poll_loop())]

    async def stop(self):
        self._stop = True
        for t in self._tasks:
            t.cancel()

    # ── commands ──
    def _check(self, domain, service, eid):
        if service not in self.allowed_services.get(domain, ()):
            raise GwError(403, "not_allowed", "this service call is not on the gateway's allow-list", {"service": "%s.%s" % (domain, service)})
        if eid not in self.controllable:
            raise GwError(403, "not_allowed", "this entity is not on the gateway's allow-list", {"entity": eid})

    async def call(self, domain, service, eid, data=None):
        self._check(domain, service, eid)
        st, _js = await self.http("POST", "/api/services/%s/%s" % (domain, service), {"entity_id": eid, **(data or {})})
        if st is None:
            raise GwError(503, "ha_unreachable", "Home Assistant did not answer", {"entity": eid})
        if st >= 500:
            raise GwError(503, "ha_unreachable", "Home Assistant is failing", {"entity": eid, "status": st})
        if st >= 400:
            raise GwError(502, "ha_refused", "Home Assistant refused the call", {"entity": eid, "status": st})
        return True

    async def wait_state(self, eid, accept, timeout):
        """Read back: wait until accept(state) is true or the time is up. Returns (observed state, waited seconds). Never raises."""
        t0 = time.monotonic()
        last_poll = 0.0
        while True:
            s = self.state(eid)
            if s is not None and accept(s):
                return s, time.monotonic() - t0
            if time.monotonic() - t0 >= timeout:
                return s, time.monotonic() - t0
            if (self.link != "ws" or time.monotonic() - last_poll > 1.5):
                last_poll = time.monotonic()
                await self.refresh(eid)
                s = self.state(eid)
                if s is not None and accept(s):
                    return s, time.monotonic() - t0
            await asyncio.sleep(0.15)

    def health(self):
        return {"link": self.link, "last_event_age_s": round(self.clock() - self.last_event, 1) if self.last_event else None}
