"""Client of the schedule service. The gateway holds TWO service tokens (admin role, household role) and uses the one matching the acting
person's role, together with an X-Acting-User header that the schedule service accepts only from gateway-flagged tokens and writes into its
audit rows. The schedule service stays the only writer of schedules and enforces the guardrails itself; its answers pass through."""
import asyncio
import json
import time
import urllib.error
import urllib.request
from .auth import GwError


class SchedClient:
    def __init__(self, url, admin_token_file, household_token_file, clock=time.time):
        self.url = url.rstrip("/")
        self.files = {"admin": admin_token_file, "household": household_token_file}
        self.clock = clock
        self.last_ok = None
        self._entities = None
        self._entities_ts = 0.0
        self._runs = None
        self._runs_ts = 0.0

    def _token(self, role):
        with open(self.files["admin" if role == "admin" else "household"], encoding="utf-8") as f:
            return f.read().strip()

    def _sync(self, method, path, role, acting, body=None, headers=None):
        h = {"Authorization": "Bearer " + self._token(role), "Content-Type": "application/json"}
        if acting:
            h["X-Acting-User"] = "person=%s;phone=%s" % (acting["person"], acting.get("phone") or "-")
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=(json.dumps(body).encode() if body is not None else None), method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                self.last_ok = self.clock()
                return r.status, (json.loads(raw) if raw.strip() else None), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), dict(e.headers)
            except ValueError:
                return e.code, None, dict(e.headers)
        except (urllib.error.URLError, OSError):
            return None, None, {}
        except Exception:                                   # noqa: BLE001 - no token in any message
            return None, None, {}

    async def call(self, method, path, role, acting=None, body=None, headers=None):
        """Returns (status, json, headers). Raises GwError 502 when the schedule service cannot be reached."""
        st, js, hd = await asyncio.to_thread(self._sync, method, path, role, acting, body, headers)
        if st is None:
            raise GwError(502, "schedule_service_unreachable", "the schedule service did not answer")
        return st, js, hd

    @staticmethod
    def translate_error(status, js):
        """Schedule-service error {error:{code,message,problems}} -> gateway error shape; the codes and the reasons are kept unchanged."""
        e = (js or {}).get("error") or {}
        details = {}
        if e.get("problems"):
            details["problems"] = e["problems"]
        return GwError(status, e.get("code") or "schedule_service_error", e.get("message") or "the schedule service refused the request", details)

    async def entities(self, force=False):
        """Entity list (cached 30 s): the source of the device list and of the allow-list."""
        if self._entities is None or force or self.clock() - self._entities_ts > 30:
            st, js, _ = await self.call("GET", "/v1/entities", "household")
            if st == 200 and js:
                self._entities, self._entities_ts = js["entities"], self.clock()
            elif self._entities is None:
                raise GwError(502, "schedule_service_error", "cannot read the entity list", {"status": st})
        return self._entities

    # ── timed runs (Task 29 Part 1): the schedule service owns them, the gateway only asks ──
    async def active_runs(self, force=False):
        """{entity id: run} of the runs the service reports as active (cached 3 s; a write drops the cache)."""
        if force or self._runs is None or self.clock() - self._runs_ts > 3:
            try:
                st, js, _ = await self.call("GET", "/v1/runs?active=1", "household")
            except GwError:
                return self._runs or {}
            if st == 200 and js:
                self._runs = {r["entity"]: r for r in js["runs"] if r["status"] == "active"}
                self._runs_ts = self.clock()
        return self._runs or {}

    def drop_runs_cache(self):
        self._runs = None

    async def run_call(self, method, path, role, acting, body=None):
        """Returns the run object. Errors keep the service's code and reasons (422 problems, 409 run_active, ...)."""
        st, js, _ = await self.call(method, path, role, acting, body)
        self.drop_runs_cache()
        if st in (200, 201):
            return js
        raise self.translate_error(st, js)

    async def effective(self, eid, role="household"):
        st, js, _ = await self.call("GET", "/v1/effective?entity=" + eid, role)
        return js if st == 200 else None

    async def health(self):
        try:
            st, js, _ = await self.call("GET", "/v1/health", "household")
        except GwError:
            return {"link": "down", "in_sync": None}
        if st == 200 and js:
            return {"link": "ok", "in_sync": js.get("in_sync"), "status": js.get("status")}
        return {"link": "error", "in_sync": None}
