"""The /v1 API (Starlette). Authentication = device token (Bearer) or web session (cookie + CSRF header). Every state-changing call takes an
Idempotency-Key. Errors: {"error": {"code", "message", "details"}}. Secrets are never put into a response except where documented as shown once."""
import asyncio
import json
import time
from collections import defaultdict, deque

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import config
from .auth import GwError, sha
from .db import iso
from .hub import Hub

SAFE = ("GET", "HEAD", "OPTIONS")
MAX_BODY = 64 * 1024
COOKIE = "gw_session"


def jload(raw):
    """Parse a request body; anything but a JSON object is a 400, never a 500."""
    try:
        v = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        raise GwError(400, "bad_json", "the body is not valid JSON")
    if not isinstance(v, dict):
        raise GwError(400, "bad_body", "a JSON object is required")
    return v


def err_response(e):
    return JSONResponse({"error": {"code": e.code, "message": e.message, "details": e.details}}, status_code=e.status, headers=e.headers)


class App:
    def __init__(self, db, people, core, hub, sched, ha, clock=time.time, parental=None, filters=None):
        self.db, self.people, self.core, self.hub, self.sched, self.ha, self.clock = db, people, core, hub, sched, ha, clock
        self.parental, self.filters = parental, filters
        self.bad = defaultdict(deque)                    # failed bearer attempts per address

    # ── principal ─────────────────────────────────────────────────────────────
    def ip(self, request):
        return request.client.host if request.client else "?"

    def authenticate(self, request, csrf=True):
        """Returns the principal dict, or raises GwError 401/403/429."""
        ip = self.ip(request)
        now = self.clock()
        q = self.bad[ip]
        while q and q[0] < now - 300:
            q.popleft()
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            if len(q) >= 30:
                raise GwError(429, "too_many_attempts", "too many failed attempts from this address", {"retry_after_s": 300}, {"Retry-After": "300"})
            p = self.people.phone_principal(h[7:].strip())
            if not p:
                q.append(now)
                raise GwError(401, "unauthorized", "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
            p["ip"] = ip
            return p
        cookie = request.cookies.get(COOKIE)
        if cookie:
            p = self.people.session_principal(cookie)
            if p:
                p["ip"] = ip
                if csrf and request.method not in SAFE:
                    if not request.headers.get("x-csrf-token") or request.headers.get("x-csrf-token") != p["csrf"]:
                        raise GwError(403, "csrf", "missing or wrong X-CSRF-Token")
                return p
        raise GwError(401, "unauthorized", "authentication required")

    @staticmethod
    def need(principal, *roles):
        if principal["role"] not in roles:
            raise GwError(403, "forbidden", "your role may not do this", {"role": principal["role"]})

    async def body(self, request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise GwError(413, "too_large", "request body too large")
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise GwError(400, "bad_json", "the body is not valid JSON")

    # ── idempotency ───────────────────────────────────────────────────────────
    async def idem(self, request, principal, route, handler, required=True, body_bytes=b""):
        """Runs handler() -> (status, body); a repeat with the same Idempotency-Key returns the first result."""
        key = request.headers.get("idempotency-key")
        if not key:
            if required:
                raise GwError(400, "idempotency_key_required", "send an Idempotency-Key header (8-80 characters)")
            return await handler()
        if not 8 <= len(key) <= 80:
            raise GwError(400, "idempotency_key_invalid", "Idempotency-Key must be 8-80 characters")
        scope = "%s:%s" % (principal["person_id"], route)
        kh = sha(key)
        rh = sha(request.method + request.url.path + (request.url.query or "") + body_bytes.decode("utf-8", "replace"))
        self.db.x("DELETE FROM idem WHERE created<?", (self.clock() - config.IDEM_TTL_S,))
        row = self.db.q1("SELECT * FROM idem WHERE scope=? AND key_hash=?", (scope, kh))
        if row:
            if row["req_hash"] != rh:
                raise GwError(422, "idempotency_conflict", "this Idempotency-Key was used for a different request")
            body = json.loads(row["response"])
            if isinstance(body, dict):
                body["replayed"] = True
            return row["status"], body
        status, body = await handler()
        if status < 500 and status != 429:
            self.db.x("INSERT OR IGNORE INTO idem(scope,key_hash,req_hash,status,response,created) VALUES (?,?,?,?,?,?)", (scope, kh, rh, status, json.dumps(body), self.clock()))
        return status, body

    # ── handlers ──────────────────────────────────────────────────────────────
    async def health(self, request):
        auth = request.headers.get("authorization") or request.cookies.get(COOKIE)
        if not auth:
            return JSONResponse({"status": "ok"})
        self.authenticate(request, csrf=False)
        ok, rows, _bad = self.db.verify_audit()
        sh = await self.sched.health()
        ha = self.ha.health()
        status = "ok" if ok and ha["link"] != "down" and sh["link"] == "ok" else "degraded"
        return JSONResponse({"status": status, "version": config.VERSION, "data_checksum": self.db.checksum(), "ha": ha,
                             "schedule_service": {"link": sh["link"], "in_sync": sh.get("in_sync")},
                             "probes": {"last_run_age_s": round(self.clock() - self.core.probe_ts, 1) if self.core.probe_ts else None},
                             "audit_chain": {"ok": ok, "rows": rows},
                             "filter_reverts": self.filters.recent_reverts() if self.filters else []})

    async def login(self, request):
        b = await self.body(request)
        if not isinstance(b, dict) or not isinstance(b.get("username"), str) or not isinstance(b.get("password"), str):
            raise GwError(400, "bad_body", "username and password are required")
        raw, sess = self.people.login(b["username"], b["password"], b.get("totp"), self.ip(request))
        r = JSONResponse(sess)
        r.set_cookie(COOKIE, raw, httponly=True, secure=True, samesite="strict", max_age=config.SESSION_ABS_S, path="/")
        return r

    async def logout(self, request):
        p = self.authenticate(request)
        self.people.logout(p)
        r = Response(status_code=204)
        r.delete_cookie(COOKIE, path="/")
        return r

    async def session(self, request):
        p = self.authenticate(request, csrf=False)
        if p["via"] != "session":
            raise GwError(401, "unauthorized", "this endpoint is for web sessions")
        return JSONResponse(self.people.session_view(p["sid"]))

    async def enrol(self, request):
        b = await self.body(request)
        if not isinstance(b, dict) or not isinstance(b.get("code"), str) or not isinstance(b.get("device"), dict):
            raise GwError(400, "bad_body", "code and device are required")
        res = self.people.enrol(b["code"], b["device"], self.ip(request))
        r = JSONResponse(res, status_code=201, headers={"Cache-Control": "no-store"})
        return r

    async def capabilities(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(self.core.capabilities(p))

    # people
    async def people_list(self, request):
        p = self.authenticate(request, csrf=False)
        self.need(p, "admin")
        return JSONResponse({"people": self.people.list()})

    async def people_create(self, request):
        p = self.authenticate(request)
        self.need(p, "admin")
        raw = await request.body()
        b = jload(raw)

        async def go():
            if not isinstance(b, dict) or set(b) - {"name", "role", "parent"}:
                raise GwError(422, "invalid_body", "name, role and optional parent are required")
            return 201, self.people.create(p, b.get("name"), b.get("role"), bool(b.get("parent", False)))
        st, body = await self.idem(request, p, "people.create", go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def people_patch(self, request):
        p = self.authenticate(request)
        self.need(p, "admin")
        raw = await request.body()
        b = jload(raw)
        pid = request.path_params["id"]

        async def go():
            return 200, self.people.patch(p, pid, b)
        st, body = await self.idem(request, p, "people.patch", go, required=False, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def enrol_code_create(self, request):
        p = self.authenticate(request)
        self.need(p, "admin")
        pid = request.path_params["id"]

        async def go():
            import segno
            import io
            code, exp = self.people.create_enrolment_code(p, pid)
            from .auth import fmt_code
            payload = {"u": config.PUBLIC_URL, "c": fmt_code(code), "tailnet": ""}
            buf = io.BytesIO()
            segno.make(json.dumps(payload, separators=(",", ":")), error="m").save(buf, kind="svg", scale=4, border=2, dark="#000000", light="#ffffff", xmldecl=False, svgns=True, nl=False)
            return 201, {"code": fmt_code(code), "expires_at": iso(exp), "ttl_s": config.ENROL_TTL_S, "qr_payload": payload, "qr_svg": buf.getvalue().decode()}
        st, body = await self.idem(request, p, "enrol.create:" + pid, go)
        return JSONResponse(body, status_code=st, headers={"Cache-Control": "no-store"})

    async def setup_link_create(self, request):
        p = self.authenticate(request)
        self.need(p, "admin")
        pid = request.path_params["id"]

        async def go():
            raw, exp = self.people.create_setup_link(p, pid)
            return 201, {"url": "%s/setup/%s" % (config.PUBLIC_URL, raw), "expires_at": iso(exp)}
        st, body = await self.idem(request, p, "setup.create:" + pid, go)
        return JSONResponse(body, status_code=st, headers={"Cache-Control": "no-store"})

    async def phones_list(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse({"phones": self.people.phones(None if p["role"] == "admin" else p["person_id"])})

    async def phone_revoke(self, request):
        p = self.authenticate(request)
        pid = request.path_params["id"]

        async def go():
            self.people.revoke_phone(p, pid)
            return 204, None
        st, body = await self.idem(request, p, "phone.revoke:" + pid, go)
        return Response(status_code=204)

    # devices
    async def devices_list(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse({"devices": await self.core.devices_view(p), "ha_link": self.ha.link})

    async def device_get(self, request):
        p = self.authenticate(request, csrf=False)
        did = request.path_params["id"]
        if did not in self.core.devices:
            raise GwError(404, "not_found", "no such device", {"id": did})
        return JSONResponse(await self.core.device_view(p, did))

    # timed runs (Task 29 Part 1)
    @staticmethod
    def _minutes_body(raw):
        b = jload(raw)
        m = b.get("minutes")
        if isinstance(m, bool) or not isinstance(m, int) or set(b) - {"minutes"}:
            raise GwError(400, "bad_body", "{\"minutes\": <whole number>} is required")
        return m

    async def device_run_start(self, request):
        p = self.authenticate(request)
        did = request.path_params["id"]
        raw = await request.body()
        minutes = self._minutes_body(raw)

        async def go():
            return 200, await self.core.start_run(p, did, minutes)
        st, body = await self.idem(request, p, "run.start:" + did, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def device_run_extend(self, request):
        p = self.authenticate(request)
        did = request.path_params["id"]
        raw = await request.body()
        minutes = self._minutes_body(raw)

        async def go():
            return 200, await self.core.extend_run(p, did, minutes)
        st, body = await self.idem(request, p, "run.extend:" + did, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def device_run_end(self, request):
        p = self.authenticate(request)
        did = request.path_params["id"]

        async def go():
            return 200, await self.core.end_run(p, did)
        st, body = await self.idem(request, p, "run.end:" + did, go)
        return JSONResponse(body, status_code=st)

    async def device_patch(self, request):
        p = self.authenticate(request)
        self.need(p, "admin")
        did = request.path_params["id"]
        raw = await request.body()
        b = jload(raw)
        return JSONResponse(await self.core.patch_device(p, did, b))

    async def device_switch(self, request):
        p = self.authenticate(request)
        did = request.path_params["id"]
        raw = await request.body()
        b = jload(raw)
        if not isinstance(b.get("on"), bool) or set(b) - {"on"}:
            raise GwError(400, "bad_body", "{\"on\": true|false} is required")

        async def go():
            return 200, await self.core.switch(p, did, b["on"])
        st, body = await self.idem(request, p, "switch:" + did, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    # vacuums
    async def vacuums_list(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse({"vacuums": self.core.vacuums_view(p)})

    async def vac_action(self, request, action):
        p = self.authenticate(request)
        vid = request.path_params["id"]
        scenario = request.path_params.get("scenario")

        async def go():
            if action == "start":
                return 200, await self.core.start_scenario(p, vid, scenario)
            if action == "resume":
                return 200, await self.core.resume(p, vid)
            if action == "pause":
                return 200, await self.core.pause(p, vid)
            return 200, await self.core.dock(p, vid)
        st, body = await self.idem(request, p, "vac.%s:%s:%s" % (action, vid, scenario), go)
        return JSONResponse(body, status_code=st)

    async def vac_start(self, request):
        return await self.vac_action(request, "start")

    async def vac_resume(self, request):
        return await self.vac_action(request, "resume")

    async def vac_pause(self, request):
        return await self.vac_action(request, "pause")

    async def vac_dock(self, request):
        return await self.vac_action(request, "dock")

    # schedules (proxy)
    async def _sched_entity(self, sid):
        for e in await self.sched.entities():
            if e["id"] == sid and e["kind"] in ("plug", "vacuum_plan"):
                return e
        raise GwError(404, "not_found", "no such schedule", {"id": sid})

    @staticmethod
    def _srole(p):
        return "admin" if p["role"] == "admin" else "household"

    @staticmethod
    def _acting(p):
        return {"person": p["person_id"], "phone": p.get("phone_id") or "-"}

    def _can_edit(self, p, ent):
        return p["role"] == "admin" or (p["role"] == "household" and bool(ent.get("flags", {}).get("household_editable")))

    async def _proxy(self, p, method, path, *, body=None, headers=None, expect=(200,)):
        st, js, hd = await self.sched.call(method, path, self._srole(p), self._acting(p), body, headers)
        if st not in expect:
            raise self.sched.translate_error(st, js)
        return st, js, hd

    async def schedule_get(self, request):
        p = self.authenticate(request, csrf=False)
        sid = request.path_params["id"]
        ent = await self._sched_entity(sid)
        _st, js, hd = await self._proxy(p, "GET", "/v1/schedules/" + sid)
        js["can_edit"] = self._can_edit(p, ent)
        return JSONResponse(js, headers={"ETag": hd.get("ETag") or hd.get("etag") or '"%s"' % js.get("version")})

    async def schedule_put(self, request):
        p = self.authenticate(request)
        sid = request.path_params["id"]
        ent = await self._sched_entity(sid)
        if not self._can_edit(p, ent):
            raise GwError(403, "forbidden", "your role may not edit this schedule", {"schedule": sid})
        im = request.headers.get("if-match")
        if im is None:
            raise GwError(428, "precondition_required", "send If-Match with the ETag you read")
        raw = await request.body()
        b = jload(raw)

        async def go():
            try:
                _st, js, hd = await self._proxy(p, "PUT", "/v1/schedules/" + sid, body=b, headers={"If-Match": im})
            except GwError as e:
                self.db.audit("schedule_put", person=p["person_id"], phone=p.get("phone_id"), target=sid, after={"error": e.code}, ok=False, ip=p.get("ip"))
                raise
            self.db.audit("schedule_put", person=p["person_id"], phone=p.get("phone_id"), target=sid, after={"version": js.get("version"), "changed": js.get("changed")}, ip=p.get("ip"))
            js["can_edit"] = True
            return 200, {"__etag": hd.get("ETag") or hd.get("etag"), **js}
        st, body = await self.idem(request, p, "sched.put:" + sid, go, required=False, body_bytes=raw)
        etag = body.pop("__etag", None) if isinstance(body, dict) else None
        return JSONResponse(body, status_code=st, headers={"ETag": etag or '"%s"' % body.get("version")})

    async def schedule_preview(self, request):
        p = self.authenticate(request)
        sid = request.path_params["id"]
        ent = await self._sched_entity(sid)
        if not self._can_edit(p, ent):
            raise GwError(403, "forbidden", "your role may not edit this schedule", {"schedule": sid})
        _st, js, _ = await self._proxy(p, "POST", "/v1/schedules/%s/preview" % sid, body=await self.body(request))
        return JSONResponse(js)

    async def schedule_undo(self, request):
        p = self.authenticate(request)
        sid = request.path_params["id"]
        ent = await self._sched_entity(sid)
        if not self._can_edit(p, ent):
            raise GwError(403, "forbidden", "your role may not edit this schedule", {"schedule": sid})

        async def go():
            try:
                _st, js, hd = await self._proxy(p, "POST", "/v1/schedules/%s/undo" % sid)
            except GwError as e:
                self.db.audit("schedule_undo", person=p["person_id"], phone=p.get("phone_id"), target=sid, after={"error": e.code}, ok=False, ip=p.get("ip"))
                raise
            self.db.audit("schedule_undo", person=p["person_id"], phone=p.get("phone_id"), target=sid, after={"version": js.get("version")}, ip=p.get("ip"))
            js["can_edit"] = True
            return 200, js
        st, body = await self.idem(request, p, "sched.undo:" + sid, go, required=False)
        return JSONResponse(body, status_code=st, headers={"ETag": '"%s"' % body.get("version")})

    async def exceptions_list(self, request):
        p = self.authenticate(request, csrf=False)
        sid = request.path_params["id"]
        await self._sched_entity(sid)
        _st, js, _ = await self._proxy(p, "GET", "/v1/exceptions?entity=" + sid)
        return JSONResponse(js)

    async def exception_add(self, request):
        p = self.authenticate(request)
        sid = request.path_params["id"]
        ent = await self._sched_entity(sid)
        if not self._can_edit(p, ent):
            raise GwError(403, "forbidden", "your role may not edit this schedule", {"schedule": sid})
        raw = await request.body()
        b = jload(raw)

        async def go():
            _st, js, _ = await self._proxy(p, "POST", "/v1/exceptions", body={**b, "entity_id": sid}, expect=(201,))
            self.db.audit("schedule_exception_add", person=p["person_id"], phone=p.get("phone_id"), target=sid, after={"kind": b.get("kind"), "id": js.get("id")}, ip=p.get("ip"))
            return 201, js
        st, body = await self.idem(request, p, "sched.xadd:" + sid, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def exception_delete(self, request):
        p = self.authenticate(request)
        self.need(p, "admin", "household")
        xid = request.path_params["xid"]

        async def go():
            await self._proxy(p, "DELETE", "/v1/exceptions/" + xid, expect=(204,))
            self.db.audit("schedule_exception_delete", person=p["person_id"], phone=p.get("phone_id"), target=xid, ip=p.get("ip"))
            return 204, None
        await self.idem(request, p, "sched.xdel:" + xid, go)
        return Response(status_code=204)

    async def services(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse({"services": self.core.tiles(p)})

    async def audit(self, request):
        p = self.authenticate(request, csrf=False)
        qp = request.query_params
        try:
            limit = int(qp.get("limit", "50"))
            before = int(qp["before"]) if "before" in qp else None
        except ValueError:
            raise GwError(400, "bad_query", "limit and before must be integers")
        about = None if p["role"] == "admin" else p["person_id"]
        return JSONResponse({"audit": self.db.audit_rows(limit, before, qp.get("action"), qp.get("person") if p["role"] == "admin" else None, qp.get("device") if p["role"] == "admin" else None, about)})

    # ── parental control (Task 29 Part 2): the module owns no data, see parental.py ──
    def _pm(self):
        if not self.parental:
            raise GwError(404, "not_available", "the parental-control module is not configured")
        return self.parental

    def _paudit(self, p, action, target, after=None, ok=True):
        self.db.audit(action, person=p["person_id"], phone=p.get("phone_id"), target=target, after=after, ok=ok, ip=p.get("ip"))

    @staticmethod
    def _rules_body(b):
        if not isinstance(b, dict) or set(b) != {"rules"} or not isinstance(b["rules"], list):
            raise GwError(400, "bad_body", "an object with 'rules' (a list) is required")
        for r in b["rules"]:
            if (not isinstance(r, dict) or set(r) != {"id", "days", "start", "end", "opens"} or not isinstance(r["id"], str) or not isinstance(r["days"], list)
                    or not isinstance(r["start"], str) or not isinstance(r["end"], str)
                    or not isinstance(r["opens"], list) or not r["opens"] or any(o not in ("youtube", "roblox", "maps") for o in r["opens"])):
                raise GwError(400, "bad_body", "every rule needs id, days, start, end and opens (youtube, roblox, maps)")
        return b

    async def p_children(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().children(p))

    async def p_child(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().child(p, request.path_params["id"]))

    async def p_windows_get(self, request):
        p = self.authenticate(request, csrf=False)
        js, etag = await self._pm().windows_get(p, request.path_params["id"])
        return JSONResponse(js, headers={"ETag": etag})

    async def p_windows_put(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]
        im = request.headers.get("if-match")
        if im is None:
            raise GwError(428, "precondition_required", "send If-Match with the ETag you read")
        raw = await request.body()
        b = self._rules_body(jload(raw))

        async def go():
            try:
                js, etag = await self._pm().windows_put(p, cid, b, im)
            except GwError as e:
                self._paudit(p, "parental_windows_put", cid, {"error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_windows_put", cid, {"version": js["version"], "changed": js.get("changed")})
            return 200, {"__etag": etag, **js}
        st, body = await self.idem(request, p, "par.win:" + cid, go, required=False, body_bytes=raw)
        etag = body.pop("__etag", None) if isinstance(body, dict) else None
        return JSONResponse(body, status_code=st, headers={"ETag": etag or '"%s"' % body.get("version")})

    async def p_windows_preview(self, request):
        p = self.authenticate(request)
        b = self._rules_body(jload(await request.body()))
        return JSONResponse(await self._pm().windows_preview(p, request.path_params["id"], b))

    async def p_windows_undo(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]

        async def go():
            try:
                js, etag = await self._pm().windows_undo(p, cid)
            except GwError as e:
                self._paudit(p, "parental_windows_undo", cid, {"error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_windows_undo", cid, {"version": js["version"]})
            return 200, {"__etag": etag, **js}
        st, body = await self.idem(request, p, "par.winundo:" + cid, go, required=False)
        etag = body.pop("__etag", None) if isinstance(body, dict) else None
        return JSONResponse(body, status_code=st, headers={"ETag": etag or '"%s"' % body.get("version")})

    async def p_exceptions_list(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().exceptions_list(p, request.path_params["id"]))

    async def p_exception_grant(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]
        raw = await request.body()
        b = jload(raw)
        if (set(b) - {"target", "minutes", "until_end_of_day"} or b.get("target") not in ("youtube", "roblox", "maps", "all")
                or ("minutes" in b and (isinstance(b["minutes"], bool) or not isinstance(b["minutes"], int) or b["minutes"] < 1))
                or ("until_end_of_day" in b and not isinstance(b["until_end_of_day"], bool))):
            raise GwError(400, "bad_body", "target (youtube, roblox, maps, all) and minutes or until_end_of_day are required")

        async def go():
            try:
                x = await self._pm().exception_grant(p, cid, b)
            except GwError as e:
                self._paudit(p, "parental_exception_grant", cid, {"target": b["target"], "error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_exception_grant", cid, {"target": b["target"], "expires_at": x["expires_at"], "id": x["id"]})
            return 201, x
        st, body = await self.idem(request, p, "par.xadd:" + cid, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def p_exception_cancel(self, request):
        p = self.authenticate(request)
        cid, xid = request.path_params["id"], request.path_params["xid"]

        async def go():
            try:
                await self._pm().exception_cancel(p, cid, xid)
            except GwError as e:
                self._paudit(p, "parental_exception_cancel", cid, {"id": xid, "error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_exception_cancel", cid, {"id": xid})
            return 204, None
        await self.idem(request, p, "par.xdel:" + xid, go)
        return Response(status_code=204)

    async def p_activity(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().activity(p, request.path_params["id"], request.query_params))

    async def p_domains_get(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().domains_get(p, request.path_params["id"]))

    async def p_domain_add(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]
        raw = await request.body()
        b = jload(raw)
        if set(b) != {"kind", "domain"} or b["kind"] not in ("allow", "deny") or not isinstance(b["domain"], str) or len(b["domain"]) > 253:
            raise GwError(400, "bad_body", "kind (allow or deny) and domain are required")

        async def go():
            try:
                js = await self._pm().domain_add(p, cid, b)
            except GwError as e:
                self._paudit(p, "parental_domain_add", cid, {"kind": b["kind"], "domain": b["domain"][:120], "error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_domain_add", cid, {"kind": b["kind"], "domain": b["domain"][:120]})
            return 200, js
        st, body = await self.idem(request, p, "par.dadd:" + cid, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def p_domain_remove(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]
        kind, domain = request.query_params.get("kind"), request.query_params.get("domain")
        if kind not in ("allow", "deny") or not domain or len(domain) > 253:
            raise GwError(400, "bad_query", "kind (allow or deny) and domain are required")

        async def go():
            try:
                js = await self._pm().domain_remove(p, cid, kind, domain)
            except GwError as e:
                self._paudit(p, "parental_domain_remove", cid, {"kind": kind, "domain": domain[:120], "error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_domain_remove", cid, {"kind": kind, "domain": domain[:120]})
            return 200, js
        st, body = await self.idem(request, p, "par.ddel:" + cid, go)
        return JSONResponse(body, status_code=st)

    async def p_domain_undo(self, request):
        p = self.authenticate(request)
        cid = request.path_params["id"]

        async def go():
            try:
                js = await self._pm().domain_undo(p, cid)
            except GwError as e:
                self._paudit(p, "parental_domain_undo", cid, {"error": e.code}, ok=False)
                raise
            self._paudit(p, "parental_domain_undo", cid)
            return 200, js
        st, body = await self.idem(request, p, "par.dundo:" + cid, go)
        return JSONResponse(body, status_code=st)

    async def p_health(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._pm().health(p))

    # filter lists: admin only (and a parent)
    def _fl(self, p):
        self._pm()._need(p, admin=True)
        if not self.filters:
            raise GwError(404, "not_available", "the filter-list module is not configured")
        return self.filters

    async def fl_list(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._fl(p).view())

    async def fl_catalogue(self, request):
        p = self.authenticate(request, csrf=False)
        return JSONResponse(await self._fl(p).catalogue())

    async def _fl_change(self, request, p, route, fn, raw=b""):
        async def go():
            return 200, await fn()
        st, body = await self.idem(request, p, route, go, body_bytes=raw)
        return JSONResponse(body, status_code=st)

    async def fl_add(self, request):
        p = self.authenticate(request)
        f = self._fl(p)
        raw = await request.body()
        b = jload(raw)
        if (set(b) - {"catalogue_id", "url", "name", "confirm_large"} or any(k in b and not isinstance(b[k], str) for k in ("catalogue_id", "url", "name"))
                or ("confirm_large" in b and not isinstance(b["confirm_large"], bool))):
            raise GwError(400, "bad_body", "catalogue_id or url (and name, confirm_large) are required")
        return await self._fl_change(request, p, "fl.add", lambda: f.add(p, b), raw)

    async def fl_patch(self, request):
        p = self.authenticate(request)
        f = self._fl(p)
        lid = self._list_id(request)
        raw = await request.body()
        b = jload(raw)
        if not b or set(b) - {"enabled", "name"} or ("enabled" in b and not isinstance(b["enabled"], bool)) or ("name" in b and not isinstance(b["name"], str)):
            raise GwError(400, "bad_body", "enabled (true/false) and/or name are required")
        return await self._fl_change(request, p, "fl.patch:%d" % lid, lambda: f.patch(p, lid, b), raw)

    async def fl_remove(self, request):
        p = self.authenticate(request)
        f = self._fl(p)
        lid = self._list_id(request)
        return await self._fl_change(request, p, "fl.del:%d" % lid, lambda: f.remove(p, lid))

    async def fl_refresh(self, request):
        p = self.authenticate(request)
        f = self._fl(p)
        lid = self._list_id(request)
        raw = await request.body()
        b = jload(raw)
        if set(b) - {"confirm"} or ("confirm" in b and not isinstance(b["confirm"], bool)):
            raise GwError(400, "bad_body", "only confirm (true/false) is accepted")
        return await self._fl_change(request, p, "fl.refresh:%d" % lid, lambda: f.refresh(p, lid, bool(b.get("confirm"))), raw)

    async def fl_revert(self, request):
        p = self.authenticate(request)
        f = self._fl(p)
        lid = self._list_id(request)
        return await self._fl_change(request, p, "fl.revert:%d" % lid, lambda: f.revert(p, lid))

    @staticmethod
    def _list_id(request):
        try:
            return int(request.path_params["id"])
        except ValueError:
            raise GwError(404, "not_found", "no such list")

    # ── events ────────────────────────────────────────────────────────────────
    async def events(self, request):
        p = self.authenticate(request, csrf=False)
        key = p.get("phone_id") or ("s:" + p.get("sid", ""))
        last = request.headers.get("last-event-id")
        sid, q = self.hub.subscribe(key)
        replay = []
        if last and last.isdigit():
            replay = self.hub.since(int(last))

        async def gen():
            try:
                yield "retry: 3000\n\n"
                for ev in replay:
                    yield Hub.format(ev)
                yield ": connected\n\n"
                next_check = time.monotonic() + 8
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=config.SSE_HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": hb\n\n"
                        ev = ...
                    if ev is None:
                        return                                     # kicked: the token was revoked
                    if ev is not ...:
                        yield Hub.format(ev)
                    if time.monotonic() >= next_check:             # a revoked/expired credential closes the stream even when no event arrives
                        next_check = time.monotonic() + 8
                        if p["via"] == "device" and not self.people.phone_principal_valid(p["phone_id"]):
                            return
                        if p["via"] == "session" and not self.people.session_principal_raw_valid(p.get("sid")):
                            return
            finally:
                self.hub.unsubscribe(sid)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def build(app_obj, extra_routes=(), lifespan=None):
    a = app_obj
    routes = [
        Route("/v1/health", a.health, methods=["GET"]),
        Route("/v1/auth/login", a.login, methods=["POST"]),
        Route("/v1/auth/logout", a.logout, methods=["POST"]),
        Route("/v1/auth/session", a.session, methods=["GET"]),
        Route("/v1/enrol", a.enrol, methods=["POST"]),
        Route("/v1/capabilities", a.capabilities, methods=["GET"]),
        Route("/v1/people", a.people_list, methods=["GET"]),
        Route("/v1/people", a.people_create, methods=["POST"]),
        Route("/v1/people/{id}", a.people_patch, methods=["PATCH"]),
        Route("/v1/people/{id}/enrolment-codes", a.enrol_code_create, methods=["POST"]),
        Route("/v1/people/{id}/setup-link", a.setup_link_create, methods=["POST"]),
        Route("/v1/phones", a.phones_list, methods=["GET"]),
        Route("/v1/phones/{id}", a.phone_revoke, methods=["DELETE"]),
        Route("/v1/devices", a.devices_list, methods=["GET"]),
        Route("/v1/devices/{id}", a.device_get, methods=["GET"]),
        Route("/v1/devices/{id}", a.device_patch, methods=["PATCH"]),
        Route("/v1/devices/{id}/run", a.device_run_start, methods=["POST"]),
        Route("/v1/devices/{id}/run", a.device_run_end, methods=["DELETE"]),
        Route("/v1/devices/{id}/run/extend", a.device_run_extend, methods=["POST"]),
        Route("/v1/devices/{id}/switch", a.device_switch, methods=["POST"]),
        Route("/v1/vacuums", a.vacuums_list, methods=["GET"]),
        Route("/v1/vacuums/{id}/scenarios/{scenario}/start", a.vac_start, methods=["POST"]),
        Route("/v1/vacuums/{id}/pause", a.vac_pause, methods=["POST"]),
        Route("/v1/vacuums/{id}/resume", a.vac_resume, methods=["POST"]),
        Route("/v1/vacuums/{id}/dock", a.vac_dock, methods=["POST"]),
        Route("/v1/schedules/{id}", a.schedule_get, methods=["GET"]),
        Route("/v1/schedules/{id}", a.schedule_put, methods=["PUT"]),
        Route("/v1/schedules/{id}/preview", a.schedule_preview, methods=["POST"]),
        Route("/v1/schedules/{id}/undo", a.schedule_undo, methods=["POST"]),
        Route("/v1/schedules/{id}/exceptions", a.exceptions_list, methods=["GET"]),
        Route("/v1/schedules/{id}/exceptions", a.exception_add, methods=["POST"]),
        Route("/v1/exceptions/{xid}", a.exception_delete, methods=["DELETE"]),
        Route("/v1/services", a.services, methods=["GET"]),
        Route("/v1/events", a.events, methods=["GET"]),
        Route("/v1/audit", a.audit, methods=["GET"]),
        Route("/v1/parental/children", a.p_children, methods=["GET"]),
        Route("/v1/parental/children/{id}", a.p_child, methods=["GET"]),
        Route("/v1/parental/children/{id}/windows", a.p_windows_get, methods=["GET"]),
        Route("/v1/parental/children/{id}/windows", a.p_windows_put, methods=["PUT"]),
        Route("/v1/parental/children/{id}/windows/preview", a.p_windows_preview, methods=["POST"]),
        Route("/v1/parental/children/{id}/windows/undo", a.p_windows_undo, methods=["POST"]),
        Route("/v1/parental/children/{id}/exceptions", a.p_exceptions_list, methods=["GET"]),
        Route("/v1/parental/children/{id}/exceptions", a.p_exception_grant, methods=["POST"]),
        Route("/v1/parental/children/{id}/exceptions/{xid}", a.p_exception_cancel, methods=["DELETE"]),
        Route("/v1/parental/children/{id}/activity", a.p_activity, methods=["GET"]),
        Route("/v1/parental/children/{id}/domains", a.p_domains_get, methods=["GET"]),
        Route("/v1/parental/children/{id}/domains", a.p_domain_add, methods=["POST"]),
        Route("/v1/parental/children/{id}/domains", a.p_domain_remove, methods=["DELETE"]),
        Route("/v1/parental/children/{id}/domains/undo", a.p_domain_undo, methods=["POST"]),
        Route("/v1/parental/health", a.p_health, methods=["GET"]),
        Route("/v1/parental/filter-lists", a.fl_list, methods=["GET"]),
        Route("/v1/parental/filter-lists", a.fl_add, methods=["POST"]),
        Route("/v1/parental/filter-lists/catalogue", a.fl_catalogue, methods=["GET"]),
        Route("/v1/parental/filter-lists/{id}", a.fl_patch, methods=["PATCH"]),
        Route("/v1/parental/filter-lists/{id}", a.fl_remove, methods=["DELETE"]),
        Route("/v1/parental/filter-lists/{id}/refresh", a.fl_refresh, methods=["POST"]),
        Route("/v1/parental/filter-lists/{id}/revert", a.fl_revert, methods=["POST"]),
    ] + list(extra_routes)

    async def gw_error(request, exc):
        return err_response(exc)

    async def not_found(request, exc):
        if request.url.path.startswith("/v1/"):
            return err_response(GwError(404, "not_found", "no such endpoint"))
        return Response("not found", status_code=404, media_type="text/plain")

    async def method_not_allowed(request, exc):
        return err_response(GwError(405, "method_not_allowed", "method not allowed"))

    async def server_error(request, exc):
        return err_response(GwError(500, "internal_error", "internal error"))        # no detail: an exception text could contain a secret

    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={GwError: gw_error, 404: not_found, 405: method_not_allowed, Exception: server_error})
    app.state.core = a
    return app
