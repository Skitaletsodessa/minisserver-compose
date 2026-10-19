"""The /v1 HTTP API (Starlette). Handlers are thin: authentication, parsing, then the store/model do the work.

Every error is {"error": {"code", "message", "problems"?}}. Guardrail violations are 422 with the reasons - nothing is clamped.
"""
import os
import json
import time
import asyncio
import contextlib
import datetime as dt
from collections import defaultdict, deque

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import model, store as storemod

VERSION = "1.0.0"
RANK = {"readonly": 1, "household": 2, "admin": 3}
FAIL_WINDOW, FAIL_LIMIT = 300, 10
MAX_BODY = 64 * 1024


def err(status, code, message, problems=None, headers=None):
    body = {"error": {"code": code, "message": message}}
    if problems:
        body["error"]["problems"] = problems
    return JSONResponse(body, status_code=status, headers=headers)


class App:
    def __init__(self, store, apply_status_path=None, clock=time.time):
        self.store = store
        self.apply_status_path = apply_status_path or os.path.join(store.dir, "apply-status.json")
        self.clock = clock
        self.fails = defaultdict(deque)

    # ── auth ──────────────────────────────────────────────────────────────────
    def _ip(self, request):
        return request.client.host if request.client else "?"

    def authenticate(self, request, need):
        """Returns (actor, None) or (None, error response)."""
        ip = self._ip(request)
        q = self.fails[ip]
        now = self.clock()
        while q and q[0] < now - FAIL_WINDOW:
            q.popleft()
        if len(q) >= FAIL_LIMIT:
            return None, err(429, "rate_limited", "too many failed attempts from this address", headers={"Retry-After": str(FAIL_WINDOW)})
        h = request.headers.get("authorization", "")
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        who = self.store.find_token(tok) if tok else None
        if not who:
            q.append(now)
            return None, err(401, "unauthorized", "a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})
        if RANK[who["role"]] < RANK[need]:
            return None, err(403, "forbidden", "role '%s' may not do this" % who["role"])
        self.store.touch_token(who["token"])
        return {**who, "ip": ip}, None

    async def body(self, request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return None, err(413, "too_large", "request body too large")
        try:
            data = json.loads(raw or b"null")
        except ValueError:
            return None, err(400, "bad_json", "the body is not valid JSON")
        return data, None

    # ── helpers ───────────────────────────────────────────────────────────────
    def _guard(self, fn):
        try:
            return fn()
        except model.ValidationError as e:
            return err(422, "validation_failed", "the change was rejected by the guardrails", e.problems)
        except storemod.NotFound as e:
            return err(404, "not_found", "not found: %s" % e)
        except storemod.Forbidden as e:
            return err(403, "forbidden", str(e))
        except storemod.Conflict as e:
            return err(412, "version_conflict", "the schedule changed since you read it; reload and retry",
                       headers={"ETag": '"%d"' % e.current["version"]})

    @staticmethod
    def _etag(version):
        return '"%d"' % version

    @staticmethod
    def _if_match(request):
        h = request.headers.get("if-match")
        if h is None:
            return None
        h = h.strip().strip('"')
        return int(h) if h.isdigit() else -1

    def _sched_view(self, eid):
        s = self.store.schedule(eid)
        return {"entity": eid, "version": s["version"], "tz": s["tz"], "updated_at": s["updated_at"], "rules": s["rules"]}

    @staticmethod
    def _parse_at(v):
        if not v:
            return dt.datetime.now(model.TZ)
        try:
            d = dt.datetime.fromisoformat(v)
        except ValueError:
            return None
        return d.replace(tzinfo=model.TZ) if d.tzinfo is None else d

    # ── handlers ──────────────────────────────────────────────────────────────
    async def health(self, request):
        h = request.headers.get("authorization", "")
        if not h:
            return JSONResponse({"status": "ok"})
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        ok, rows, bad = self.store.verify_audit()
        status = self.read_apply_status()
        ready = self._read_json(os.path.join(self.store.out, "render.json")) or {}
        rendered, applied = ready.get("set_hash"), status.get("set_hash")
        in_sync = bool(rendered) and rendered == applied and status.get("result") == "applied" and status.get("loaded_ok") is True
        return JSONResponse({
            "status": "ok" if ok and not self.store.publish_error else "degraded", "version": VERSION,
            "entities": len(self.store.entities()), "audit_seq": self.store.audit_seq(),
            "audit_chain": {"ok": ok, "rows": rows, "first_bad_seq": bad},
            "data_checksum": ready.get("data_checksum"), "publish_error": self.store.publish_error,
            "render": {"set_hash": rendered, "audit_seq": ready.get("audit_seq")},
            "apply": {"result": status.get("result"), "set_hash": applied, "audit_seq": status.get("seq"), "at": status.get("at"),
                      "detail": status.get("detail"), "loaded_ok": status.get("loaded_ok"), "verified_at": status.get("verified_at")},
            "in_sync": in_sync})

    def read_apply_status(self):
        return self._read_json(self.apply_status_path) or {}

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    async def entities(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse({"entities": self.store.entities()})

    async def entity_patch(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict):
            return err(400, "bad_body", "an object is required")
        eid = request.path_params["id"]
        return self._guard(lambda: JSONResponse(self.store.patch_entity(who, eid, data)[0]))

    async def guardrails_put(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict):
            return err(400, "bad_body", "an object is required")
        eid = request.path_params["id"]
        return self._guard(lambda: JSONResponse(self.store.set_guardrails(who, eid, data)[0]))

    async def schedules(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse({"schedules": [self._sched_view(x["id"]) for x in self.store.entities()]})

    async def schedule_get(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        eid = request.path_params["id"]

        def go():
            v = self._sched_view(eid)
            return JSONResponse({**v, "guardrails": self.store.entity(eid)["guardrails"]}, headers={"ETag": self._etag(v["version"])})
        return self._guard(go)

    async def schedule_put(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        im = self._if_match(request)
        if im is None:
            return err(428, "precondition_required", "send If-Match with the ETag you read")
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or "rules" not in data:
            return err(400, "bad_body", "an object with 'rules' is required")
        eid = request.path_params["id"]

        def go():
            s, changed = self.store.put_schedule(who, eid, data["rules"], im)
            v = self._sched_view(eid)
            return JSONResponse({**v, "changed": changed}, headers={"ETag": self._etag(v["version"])})
        return self._guard(go)

    async def schedule_preview(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or "rules" not in data:
            return err(400, "bad_body", "an object with 'rules' is required")
        eid = request.path_params["id"]

        def go():
            ent, cur = self.store.entity(eid), self.store.schedule(eid)
            new, probs = model.validate_rules(ent, data["rules"])
            out = {"entity": eid, "current_version": cur["version"], "valid": not probs, "problems": probs}
            if not probs:
                old_ids, new_ids = {r["id"]: r for r in cur["rules"]}, {r["id"]: r for r in new}
                out["diff"] = {"added": [r for i, r in new_ids.items() if i not in old_ids],
                               "removed": [r for i, r in old_ids.items() if i not in new_ids],
                               "changed": [{"before": old_ids[i], "after": r} for i, r in new_ids.items() if i in old_ids and old_ids[i] != r]}
                skips = model.skip_dates(self.store._exceptions_for_model(eid))
                start = dt.datetime.now(model.TZ)
                out["next_7_days"] = [{"at": t.isoformat(), "action": a, "rule": rid}
                                      for (t, a, rid) in model.edges_between(ent["kind"], new, start, start + dt.timedelta(days=7), skips)]
            return JSONResponse(out)
        return self._guard(go)

    async def schedule_undo(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        eid = request.path_params["id"]

        def go():
            s, changed = self.store.undo(who, eid)
            v = self._sched_view(eid)
            return JSONResponse({**v, "changed": changed}, headers={"ETag": self._etag(v["version"])})
        return self._guard(go)

    async def exceptions_list(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse({"exceptions": self.store.exceptions(request.query_params.get("entity"))})

    async def exception_add(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str):
            return err(400, "bad_body", "an object with 'entity_id' is required")

        def go():
            xid = self.store.add_exception(who, data["entity_id"], {k: v for k, v in data.items() if k != "entity_id"})
            return JSONResponse({"id": xid}, status_code=201)
        return self._guard(go)

    async def exception_delete(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        xid = request.path_params["id"]

        def go():
            self.store.delete_exception(who, xid)
            return Response(status_code=204)
        return self._guard(go)

    async def effective(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        eid = request.query_params.get("entity")
        if not eid:
            return err(400, "bad_query", "entity is required")
        at = self._parse_at(request.query_params.get("at"))
        if at is None:
            return err(400, "bad_query", "at must be an ISO 8601 date-time")

        def go():
            ent, s = self.store.entity(eid), self.store.schedule(eid)
            return JSONResponse(model.effective(ent, s["rules"], self.store._exceptions_for_model(eid), at))
        return self._guard(go)

    async def audit(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        try:
            limit = int(request.query_params.get("limit", "50"))
            before = int(request.query_params["before"]) if "before" in request.query_params else None
        except ValueError:
            return err(400, "bad_query", "limit and before must be integers")
        return JSONResponse({"audit": self.store.audit(request.query_params.get("entity"), limit, before)})

    async def tokens_list(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        return JSONResponse({"tokens": self.store.tokens()})

    async def token_create(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict):
            return err(400, "bad_body", "an object is required")

        def go():
            tid, secret = self.store.create_token(who, data.get("name"), data.get("role"), data.get("expires"))
            return JSONResponse({"id": tid, "name": data["name"], "role": data["role"], "token": secret}, status_code=201,
                                headers={"Cache-Control": "no-store"})
        return self._guard(go)

    async def token_revoke(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        tid = request.path_params["id"]

        def go():
            self.store.revoke_token(who, tid)
            return Response(status_code=204)
        return self._guard(go)

    # ── background: audit the applier's results, retry a failed publication ──
    async def background(self):
        last_key = None
        while True:
            try:
                st = self.read_apply_status()
                key = (st.get("seq"), st.get("result"), st.get("at"))
                if st.get("result") in ("applied", "failed") and key != last_key:
                    stored = self.store.last_apply_audited()
                    if stored != list(key):
                        self.store.record_apply({"token": "-", "name": "applier", "role": "admin"}, st)
                    last_key = key
                if self.store.publish_error:
                    self.store.publish()
            except Exception:           # pragma: no cover - the loop must not die
                pass
            await asyncio.sleep(30)


def build(store, apply_status_path=None, clock=time.time, background=True):
    a = App(store, apply_status_path, clock)
    routes = [
        Route("/v1/health", a.health, methods=["GET"]),
        Route("/v1/entities", a.entities, methods=["GET"]),
        Route("/v1/entities/{id}", a.entity_patch, methods=["PATCH"]),
        Route("/v1/entities/{id}/guardrails", a.guardrails_put, methods=["PUT"]),
        Route("/v1/schedules", a.schedules, methods=["GET"]),
        Route("/v1/schedules/{id}", a.schedule_get, methods=["GET"]),
        Route("/v1/schedules/{id}", a.schedule_put, methods=["PUT"]),
        Route("/v1/schedules/{id}/preview", a.schedule_preview, methods=["POST"]),
        Route("/v1/schedules/{id}/undo", a.schedule_undo, methods=["POST"]),
        Route("/v1/exceptions", a.exceptions_list, methods=["GET"]),
        Route("/v1/exceptions", a.exception_add, methods=["POST"]),
        Route("/v1/exceptions/{id}", a.exception_delete, methods=["DELETE"]),
        Route("/v1/effective", a.effective, methods=["GET"]),
        Route("/v1/audit", a.audit, methods=["GET"]),
        Route("/v1/tokens", a.tokens_list, methods=["GET"]),
        Route("/v1/tokens", a.token_create, methods=["POST"]),
        Route("/v1/tokens/{id}", a.token_revoke, methods=["DELETE"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(a.background()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.core = a
    return app
