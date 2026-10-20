"""The /v1 HTTP API (Starlette). Handlers are thin: authentication, parsing, then the store/model do the work.

Every error is {"error": {"code", "message", "problems"?}}. Guardrail violations are 422 with the reasons - nothing is clamped.
"""
import os
import re
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
ACTING_RE = re.compile(r"^person=([a-z0-9_-]{1,40});phone=([a-z0-9_-]{1,40})$")


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
        actor = {**who, "ip": ip}
        hdr = request.headers.get("x-acting-user")
        if hdr is not None and who.get("gateway"):
            # Task 28: only a token flagged as the gateway's may say on whose behalf it acts; the audit rows then carry it.
            m = ACTING_RE.match(hdr)
            if not m:
                return None, err(400, "bad_acting_user", "X-Acting-User must look like person=<id>;phone=<id|->")
            actor["acting"] = {"person": m.group(1), "phone": m.group(2)}
        # from every other caller the header is ignored: a plain token cannot claim to be somebody else
        return actor, None

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
        except storemod.RunConflict as e:
            return err(409, e.code, str(e), problems=[{"code": e.code, "field": "run", "message": e.run["id"] if e.run else ""}] if e.run else None)
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
            "status": "ok" if ok and not self.store.publish_error and not self.store.child_publish_error else "degraded", "version": VERSION,
            "entities": len(self.store.entities()), "audit_seq": self.store.audit_seq(),
            "audit_chain": {"ok": ok, "rows": rows, "first_bad_seq": bad},
            "data_checksum": ready.get("data_checksum"), "publish_error": self.store.publish_error,
            "render": {"set_hash": rendered, "audit_seq": ready.get("audit_seq")},
            "apply": {"result": status.get("result"), "set_hash": applied, "audit_seq": status.get("seq"), "at": status.get("at"),
                      "detail": status.get("detail"), "loaded_ok": status.get("loaded_ok"), "verified_at": status.get("verified_at")},
            "child_policy": self._child_health(),
            "in_sync": in_sync})

    def _child_health(self):
        """The snapshot the host policy job executes from, and what the job last reported (child-apply-status.json, written by the job)."""
        snap = self._read_json(os.path.join(self.store.out, "child-policy.json")) or {}
        return {"checksum": snap.get("checksum"), "generated_at": snap.get("generated_at"), "publish_error": self.store.child_publish_error,
                "devices": len(snap.get("devices") or []), "cutover": self.store.child_cutover_done(),
                "applied": self._read_json(os.path.join(self.store.dir, "child-apply-status.json"))}

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
                if ent["kind"] == "child_window":
                    out["open_next_7_days"] = [{"from": f.isoformat(timespec="seconds"), "to": t.isoformat(timespec="seconds"), "opens": o}
                                               for (f, t, o) in model.child_open_intervals(new, start, start + dt.timedelta(days=7))]
                else:
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

    # ── timed runs (Task 29 Part 1) ───────────────────────────────────────────
    def _run_view(self, r):
        out = {k: r[k] for k in ("id", "entity", "status", "started_at", "ends_at", "ended_at", "minutes_total", "started_by")}
        over = r["status"] == "active" and r["end_ts"] <= self.store._now().timestamp()
        if over:                                       # past its end (waiting out its grace in HA): to the user it has ended
            out["status"], out["ended_at"] = "ended", r["ends_at"]
        out["remaining_s"] = max(0, int(r["end_ts"] - self.store._now().timestamp())) if r["status"] == "active" else 0
        out["armed"] = self.store.run_armed(r, self.read_apply_status())
        return out

    async def runs_list(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        active = request.query_params.get("active") in ("1", "true")
        return JSONResponse({"runs": [self._run_view(r) for r in self.store.runs(request.query_params.get("entity"), active_only=active,
                                                                                    since_ts=None if active else int(self.store._now().timestamp()) - 36 * 3600)]})

    async def run_get(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return self._guard(lambda: JSONResponse(self._run_view(self.store.run(request.path_params["id"]))))

    async def run_create(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str) or set(data) - {"entity_id", "minutes"}:
            return err(400, "bad_body", "an object with entity_id and minutes is required")
        return self._guard(lambda: JSONResponse(self._run_view(self.store.start_run(who, data["entity_id"], data.get("minutes"))), status_code=201))

    async def run_extend(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or set(data) - {"minutes"}:
            return err(400, "bad_body", "an object with minutes is required")
        return self._guard(lambda: JSONResponse(self._run_view(self.store.extend_run(who, request.path_params["id"], data.get("minutes")))))

    async def run_cancel(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        wd = request.query_params.get("withdraw") in ("1", "true")
        return self._guard(lambda: JSONResponse(self._run_view(self.store.cancel_run(who, request.path_params["id"], withdraw=wd))))

    # ── the children's policy (Task 29 Part 2): per-device windows are edited through /v1/schedules/{id}; exceptions, domain lists and the snapshot are here ──
    async def child_list(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse({"children": [self.store.child_detail(d["id"]) for d in self.store.child_devices()]})

    async def child_get(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return self._guard(lambda: JSONResponse(self.store.child_detail(request.path_params["id"])))

    async def child_snapshot_get(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse(self.store.child_snapshot())

    async def child_device_add(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if (not isinstance(data, dict) or set(data) - {"name", "ip", "mac", "like"} or not isinstance(data.get("name"), str) or not isinstance(data.get("ip"), str)
                or any(k in data and data[k] is not None and not isinstance(data[k], str) for k in ("mac", "like"))):
            return err(400, "bad_body", "an object with name and ip (and optional mac, like) is required")
        return self._guard(lambda: JSONResponse(self.store.add_child_device(who, data["name"], data["ip"], data.get("mac") or "", data.get("like")), status_code=201))

    async def child_device_remove(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e

        def go():
            self.store.remove_child_device(who, request.path_params["id"])
            return Response(status_code=204)
        return self._guard(go)

    async def child_exception_add(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or set(data) - {"target", "minutes", "until_end_of_day"} or not isinstance(data.get("target"), str):
            return err(400, "bad_body", "an object with target and minutes OR until_end_of_day is required")
        if data.get("until_end_of_day") is not None and not isinstance(data["until_end_of_day"], bool):
            return err(400, "bad_body", "until_end_of_day must be true or false")
        return self._guard(lambda: JSONResponse(self.store.add_child_exception(who, request.path_params["id"], data["target"], data.get("minutes"),
                                                                                  bool(data.get("until_end_of_day"))), status_code=201))

    async def child_exception_cancel(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e

        def go():
            self.store.cancel_child_exception(who, request.path_params["id"], request.path_params["xid"])
            return Response(status_code=204)
        return self._guard(go)

    async def child_domain_add(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or set(data) - {"kind", "domain"} or not isinstance(data.get("kind"), str):
            return err(400, "bad_body", "an object with kind (allow|deny) and domain is required")
        return self._guard(lambda: JSONResponse(self.store.change_child_domain(who, request.path_params["id"], data["kind"], data.get("domain"), True)))

    async def child_domain_remove(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        kind, domain = request.query_params.get("kind"), request.query_params.get("domain")
        if kind not in ("allow", "deny") or not domain:
            return err(400, "bad_query", "kind (allow|deny) and domain are required")
        return self._guard(lambda: JSONResponse(self.store.change_child_domain(who, request.path_params["id"], kind, domain, False)))

    async def child_domain_undo(self, request):
        who, e = self.authenticate(request, "household")
        if e:
            return e
        return self._guard(lambda: JSONResponse(self.store.undo_child_domain(who, request.path_params["id"])))

    async def child_cutover(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        if not isinstance(data, dict) or not isinstance(data.get("done"), bool) or set(data) - {"done", "note"}:
            return err(400, "bad_body", "an object with done (true|false) and an optional note is required")

        def go():
            self.store.set_child_cutover(who, data["done"], str(data.get("note") or "")[:200])
            return JSONResponse({"cutover": self.store.child_cutover_done()})
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

    # ── child policy (Phase E): modelled + imported read-only; rendering is a pure preview, AdGuard is never contacted ──
    async def child_policy_get(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        return JSONResponse(self.store.child_policy(with_lists=who["role"] != "household"))

    async def child_policy_import(self, request):
        who, e = self.authenticate(request, "admin")
        if e:
            return e
        data, e = await self.body(request)
        if e:
            return e
        dry = request.query_params.get("dry_run") in ("1", "true")
        return self._guard(lambda: JSONResponse(self.store.import_child_policy(who, data, dry_run=dry)))

    async def child_policy_render(self, request):
        who, e = self.authenticate(request, "readonly")
        if e:
            return e
        if who["role"] == "household":
            return err(403, "forbidden", "role 'household' may not read the domain lists")
        data, e = await self.body(request)
        if e:
            return e
        from . import child_render
        try:
            at = self._parse_at((data or {}).get("at"))
            sd = data["service_domains"]
            ov = data.get("overrides", [])
            if at is None or any(not isinstance(sd.get(k), list) for k in ("telegram", "youtube", "roblox")) or not isinstance(ov, list):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            return err(400, "bad_body", "an object with service_domains {telegram, youtube, roblox: [domains]} and optional at, overrides is required")
        lines = child_render.render_block(self.store.child_policy(), sd, at.astimezone(model.TZ), ov)
        return JSONResponse({"at": at.astimezone(model.TZ).isoformat(), "lines": lines})

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
                self.store.expire_runs()            # a finished run stops being rendered, so its end-of-run automation does not pile up in HA
                self.store.expire_child_exceptions()
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
        Route("/v1/runs", a.runs_list, methods=["GET"]),
        Route("/v1/runs", a.run_create, methods=["POST"]),
        Route("/v1/runs/{id}", a.run_get, methods=["GET"]),
        Route("/v1/runs/{id}", a.run_cancel, methods=["DELETE"]),
        Route("/v1/runs/{id}/extend", a.run_extend, methods=["POST"]),
        Route("/v1/child", a.child_list, methods=["GET"]),
        Route("/v1/child", a.child_device_add, methods=["POST"]),
        Route("/v1/child/snapshot", a.child_snapshot_get, methods=["GET"]),
        Route("/v1/child/cutover", a.child_cutover, methods=["PUT"]),
        Route("/v1/child/{id}", a.child_get, methods=["GET"]),
        Route("/v1/child/{id}", a.child_device_remove, methods=["DELETE"]),
        Route("/v1/child/{id}/exceptions", a.child_exception_add, methods=["POST"]),
        Route("/v1/child/{id}/exceptions/{xid}", a.child_exception_cancel, methods=["DELETE"]),
        Route("/v1/child/{id}/domains", a.child_domain_add, methods=["POST"]),
        Route("/v1/child/{id}/domains", a.child_domain_remove, methods=["DELETE"]),
        Route("/v1/child/{id}/domains/undo", a.child_domain_undo, methods=["POST"]),
        Route("/v1/effective", a.effective, methods=["GET"]),
        Route("/v1/audit", a.audit, methods=["GET"]),
        Route("/v1/child-policy", a.child_policy_get, methods=["GET"]),
        Route("/v1/child-policy/import", a.child_policy_import, methods=["PUT"]),
        Route("/v1/child-policy/render", a.child_policy_render, methods=["POST"]),
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
