"""SQLite store: entities, schedules, exceptions, tokens, an append-only hash-chained audit, and the published outputs.

One writer at a time (a process-wide lock); every change is validated by model.py BEFORE it is stored, then audited in the
same transaction. After commit the snapshot (`out/schedules.json`) and the rendered HA files (`out/render/`) are written
atomically, `out/READY` last: the host-side applier acts on that marker (ADR 2). The service never talks to HA or Docker.
"""
import os
import json
import time
import uuid
import sqlite3
import hashlib
import threading
import datetime as dt
from . import model, render

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entities(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, ha_target TEXT, managed INTEGER NOT NULL DEFAULT 0,
  flags TEXT NOT NULL, guardrails TEXT NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(
  entity_id TEXT PRIMARY KEY REFERENCES entities(id), version INTEGER NOT NULL, tz TEXT NOT NULL, rules TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exceptions(
  id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id), kind TEXT NOT NULL, date TEXT, until TEXT, state TEXT,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity_id TEXT,
  before TEXT, after TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TABLE IF NOT EXISTS tokens(
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL, sha256 TEXT NOT NULL, created TEXT NOT NULL,
  expires TEXT, last_used TEXT, revoked INTEGER NOT NULL DEFAULT 0);
"""
GENESIS = "0" * 64


class Conflict(Exception):
    """If-Match did not match the stored version."""
    def __init__(self, current):
        super().__init__("version conflict")
        self.current = current


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


def now_iso():
    return dt.datetime.now(model.TZ).isoformat(timespec="seconds")


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def atomic_write(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, data_dir, clock=now_iso):
        self.dir = data_dir
        self.out = os.path.join(data_dir, "out")
        os.makedirs(self.out, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(data_dir, "schedules.db"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        if "gateway" not in [r["name"] for r in self.db.execute("PRAGMA table_info(tokens)")]:        # Task 28: tokens of the portal gateway
            self.db.execute("ALTER TABLE tokens ADD COLUMN gateway INTEGER NOT NULL DEFAULT 0")
        if "meta" not in [r["name"] for r in self.db.execute("PRAGMA table_info(entities)")]:      # Task 27 Phase E: device facts (ip, mac)
            self.db.execute("ALTER TABLE entities ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
        self.publish_error = None

    # ── transactions and audit ────────────────────────────────────────────────
    def _tx(self):
        store = self

        class _T:
            def __enter__(s):
                store.lock.acquire()
                store.db.execute("BEGIN IMMEDIATE")
            def __exit__(s, et, ev, tb):
                try:
                    store.db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store.lock.release()
        return _T()

    def _audit(self, actor, action, entity_id, before, after):
        row = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        ts = self.clock()
        body = canon({"ts": ts, "actor": actor, "action": action, "entity": entity_id, "before": before, "after": after})
        h = hashlib.sha256((prev + body).encode()).hexdigest()
        cur = self.db.execute("INSERT INTO audit(ts, actor, action, entity_id, before, after, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
                              (ts, canon(actor), action, entity_id, canon(before), canon(after), prev, h))
        return cur.lastrowid

    def _mirror_audit(self, seq):
        r = self.db.execute("SELECT * FROM audit WHERE seq=?", (seq,)).fetchone()
        line = canon({k: r[k] for k in r.keys()}) + "\n"
        with open(os.path.join(self.dir, "audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def verify_audit(self):
        """Walks the whole chain; returns (ok, rows, first bad seq)."""
        prev, n = GENESIS, 0
        with self.lock:
            for r in self.db.execute("SELECT * FROM audit ORDER BY seq"):
                body = canon({"ts": r["ts"], "actor": json.loads(r["actor"]), "action": r["action"], "entity": r["entity_id"],
                              "before": json.loads(r["before"]), "after": json.loads(r["after"])})
                if r["prev_hash"] != prev or hashlib.sha256((prev + body).encode()).hexdigest() != r["hash"]:
                    return False, n, r["seq"]
                prev, n = r["hash"], n + 1
        return True, n, None

    def audit_seq(self):
        r = self.db.execute("SELECT MAX(seq) m FROM audit").fetchone()
        return r["m"] or 0

    def audit(self, entity=None, limit=50, before_seq=None):
        q, a = "SELECT * FROM audit WHERE 1=1", []
        if entity:
            q += " AND entity_id=?"
            a.append(entity)
        if before_seq:
            q += " AND seq<?"
            a.append(before_seq)
        q += " ORDER BY seq DESC LIMIT ?"
        a.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.db.execute(q, a).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "actor": json.loads(r["actor"]), "action": r["action"], "entity": r["entity_id"],
                 "before": json.loads(r["before"]), "after": json.loads(r["after"])} for r in rows]

    # ── reads ─────────────────────────────────────────────────────────────────
    def _entity_row(self, r):
        return {"id": r["id"], "kind": r["kind"], "name": r["name"], "ha_target": r["ha_target"], "managed": bool(r["managed"]),
                "flags": json.loads(r["flags"]), "guardrails": json.loads(r["guardrails"]), "meta": json.loads(r["meta"])}

    def entities(self):
        with self.lock:
            return [self._entity_row(r) for r in self.db.execute("SELECT * FROM entities ORDER BY position, id")]

    def entity(self, eid):
        with self.lock:
            r = self.db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if not r:
            raise NotFound(eid)
        return self._entity_row(r)

    def schedule(self, eid):
        with self.lock:
            r = self.db.execute("SELECT * FROM schedules WHERE entity_id=?", (eid,)).fetchone()
        if not r:
            raise NotFound(eid)
        return {"entity": eid, "version": r["version"], "tz": r["tz"], "rules": json.loads(r["rules"]), "updated_at": r["updated_at"]}

    def schedules(self):
        return [self.schedule(e["id"]) for e in self.entities()]

    def exceptions(self, entity=None, today=None):
        with self.lock:
            q, a = "SELECT * FROM exceptions", []
            if entity:
                q += " WHERE entity_id=?"
                a.append(entity)
            rows = self.db.execute(q + " ORDER BY created_at, id", a).fetchall()
        out = [{"id": r["id"], "entity": r["entity_id"], "kind": r["kind"], "date": r["date"], "until": r["until"], "state": r["state"],
                "created_by": json.loads(r["created_by"]), "created_at": r["created_at"]} for r in rows]
        for x in out:
            x["date"] = x["date"] if x["kind"] == "skip_date" else None
        return out

    def _exceptions_for_model(self, eid):
        return [{"id": x["id"], "kind": x["kind"], "date": x["date"], "until": x["until"], "state": x["state"], "created_at": x["created_at"]}
                for x in self.exceptions(eid)]

    # ── snapshot and publication ──────────────────────────────────────────────
    def snapshot(self, today=None):
        """Deterministic view of everything a consumer needs (renderer, ha-alerts, future UI). Past skip dates are pruned."""
        today = today or dt.datetime.now(model.TZ).date()
        ents = []
        for e in self.entities():
            s = self.schedule(e["id"])
            skips = sorted(d for d in model.skip_dates(self._exceptions_for_model(e["id"])) if dt.date.fromisoformat(d) >= today)
            ents.append({**e, "tz": s["tz"], "version": s["version"], "rules": s["rules"], "skip_dates": skips})
        return {"schema": 1, "audit_seq": self.audit_seq(), "entities": ents}

    def publish(self):
        """Write out/schedules.json + out/render/** + out/render.json atomically, READY last. Never raises: failure is health-visible."""
        try:
            with self.lock:
                snap = self.snapshot()
                body = canon(snap)
                snap["checksum"] = hashlib.sha256(body.encode()).hexdigest()
                rd = render.render(snap)
                rdir = os.path.join(self.out, "render")
                files_meta = {}
                for rel, text in rd["files"].items():
                    p = os.path.join(rdir, rel)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    atomic_write(p, text)
                    files_meta[rel] = hashlib.sha256(text.encode()).hexdigest()
                # drop rendered files that no longer exist
                for root, _d, names in os.walk(rdir):
                    for n in names:
                        rel = os.path.relpath(os.path.join(root, n), rdir).replace(os.sep, "/")
                        if rel not in files_meta and not n.endswith(".tmp") and ".tmp." not in n:
                            os.remove(os.path.join(root, n))
                atomic_write(os.path.join(self.out, "schedules.json"), json.dumps(snap, indent=1, sort_keys=True) + "\n")
                meta = {"audit_seq": snap["audit_seq"], "set_hash": rd["set_hash"], "files": files_meta, "expected": rd["expected"],
                        "data_checksum": snap["checksum"]}
                atomic_write(os.path.join(self.out, "render.json"), json.dumps(meta, indent=1, sort_keys=True) + "\n")
                atomic_write(os.path.join(self.out, "READY"), "%d %s\n" % (snap["audit_seq"], rd["set_hash"]))
            self.publish_error = None
        except Exception as ex:             # pragma: no cover - exercised by a test with an unwritable dir
            self.publish_error = "%s: %s" % (type(ex).__name__, ex)
        return self.publish_error is None

    # ── writes ────────────────────────────────────────────────────────────────
    def seed(self, path, actor):
        """Import the initial data once (empty database only). Rules are validated like any other write."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        with self._tx():
            if self.db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]:
                raise Forbidden("the database already holds entities; seed is for an empty database only")
            for pos, e in enumerate(data["entities"]):
                g, gp = model.validate_guardrails(e["kind"], e.get("guardrails", {}))
                fl, fp = model.validate_flags(e.get("flags", {}))
                ent = {"id": e["id"], "kind": e["kind"], "guardrails": g}
                rules, rp = model.validate_rules(ent, e.get("rules", []))
                probs = gp + fp + rp
                if not model.ENTITY_ID_RE.match(e["id"]) or e["kind"] not in model.KINDS:
                    probs.append(model.problem("format", "id/kind", "bad entity id or kind"))
                if probs:
                    raise model.ValidationError([{**p, "message": "%s: %s" % (e["id"], p["message"])} for p in probs])
                self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position) VALUES (?,?,?,?,?,?,?,?)",
                                (e["id"], e["kind"], e["name"], e.get("ha_target"), 1 if e.get("managed") else 0, canon(fl), canon(g), pos))
                self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)",
                                (e["id"], 1, model.TZ_NAME, canon(rules), self.clock()))
                seq = self._audit(actor, "import", e["id"], None, {"entity": {k: e.get(k) for k in ("kind", "name", "ha_target")},
                                                                    "guardrails": g, "flags": fl, "rules": rules})
        self._mirror_all()
        self.publish()
        return len(data["entities"])

    def _mirror_all(self):
        p = os.path.join(self.dir, "audit.jsonl")
        have = 0
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                have = sum(1 for _ in f)
        for r in self.db.execute("SELECT seq FROM audit ORDER BY seq").fetchall()[have:]:
            self._mirror_audit(r["seq"])

    def _check_editor(self, actor, ent):
        if ent["kind"] == "child_window":
            raise Forbidden("the child policy is imported read-only in step (a); it is edited in step (c)")
        if actor["role"] == "admin":
            return
        if actor["role"] == "household" and ent["flags"].get("household_editable"):
            return
        raise Forbidden("this role may not edit '%s'" % ent["id"])

    def put_schedule(self, actor, eid, rules, if_match, action="update"):
        """if_match: the version the editor saw (int). Raises Conflict / ValidationError / Forbidden / NotFound."""
        with self._tx():
            ent = self.entity(eid)
            self._check_editor(actor, ent)
            cur = self.schedule(eid)
            if if_match != cur["version"]:
                raise Conflict(cur)
            new_rules, probs = model.validate_rules(ent, rules)
            if probs:
                raise model.ValidationError(probs)
            if new_rules == cur["rules"]:
                return cur, False
            ver = cur["version"] + 1
            self.db.execute("UPDATE schedules SET version=?, rules=?, updated_at=? WHERE entity_id=?", (ver, canon(new_rules), self.clock(), eid))
            seq = self._audit(actor, action, eid, {"version": cur["version"], "rules": cur["rules"]}, {"version": ver, "rules": new_rules})
        self._mirror_audit(seq)
        self.publish()
        return self.schedule(eid), True

    def undo(self, actor, eid):
        """Reverts the last rule change of an entity as a NEW change (guardrails apply to it like to any edit)."""
        with self.lock:
            ent = self.entity(eid)
            self._check_editor(actor, ent)
            rows = [r for r in self.audit(entity=eid, limit=200) if r["action"] in ("update", "undo", "import")]
            if not rows or rows[0]["action"] == "import":
                raise NotFound("nothing to undo for '%s'" % eid)
            last = rows[0]
            if actor["role"] != "admin" and last["actor"].get("token") != actor.get("token"):
                raise Forbidden("a household token may undo only its own changes")
            cur = self.schedule(eid)
            return self.put_schedule(actor, eid, last["before"]["rules"], cur["version"], action="undo")

    def set_guardrails(self, actor, eid, guardrails):
        with self._tx():
            if actor["role"] != "admin":
                raise Forbidden("guardrails are admin-only")
            ent = self.entity(eid)
            g, probs = model.validate_guardrails(ent["kind"], guardrails)
            if probs:
                raise model.ValidationError(probs)
            _r, rp = model.validate_rules({**ent, "guardrails": g}, self.schedule(eid)["rules"])
            if rp:
                raise model.ValidationError([{**p, "message": "the current schedule would violate the new guardrails: " + p["message"]} for p in rp])
            if g == ent["guardrails"]:
                return ent, False
            self.db.execute("UPDATE entities SET guardrails=? WHERE id=?", (canon(g), eid))
            seq = self._audit(actor, "guardrails", eid, ent["guardrails"], g)
        self._mirror_audit(seq)
        self.publish()
        return self.entity(eid), True

    def patch_entity(self, actor, eid, patch):
        """Admin only: name, flags, managed (the cutover switch)."""
        with self._tx():
            if actor["role"] != "admin":
                raise Forbidden("entity settings are admin-only")
            ent = self.entity(eid)
            probs = []
            for k in patch:
                if k not in ("name", "flags", "managed"):
                    probs.append(model.problem("unknown_key", k, "unknown field '%s'" % k))
            new = dict(ent)
            if "name" in patch:
                if not isinstance(patch["name"], str) or not 1 <= len(patch["name"]) <= 80:
                    probs.append(model.problem("format", "name", "name must be 1..80 characters"))
                else:
                    new["name"] = patch["name"]
            if "flags" in patch:
                fl, fp = model.validate_flags(patch["flags"])
                probs += fp
                new["flags"] = {**ent["flags"], **fl}
            if "managed" in patch:
                if not isinstance(patch["managed"], bool):
                    probs.append(model.problem("type", "managed", "managed must be true or false"))
                elif patch["managed"] and ent["kind"] not in model.RENDERABLE_KINDS:
                    probs.append(model.problem("kind", "managed", "this kind of entity is modelled only, it cannot be rendered in this step"))
                elif patch["managed"] and not ent["ha_target"]:
                    probs.append(model.problem("required", "managed", "the entity has no ha_target"))
                else:
                    new["managed"] = patch["managed"]
            if probs:
                raise model.ValidationError(probs)
            if new == ent:
                return ent, False
            self.db.execute("UPDATE entities SET name=?, flags=?, managed=? WHERE id=?",
                            (new["name"], canon(new["flags"]), 1 if new["managed"] else 0, eid))
            seq = self._audit(actor, "entity", eid, {k: ent[k] for k in ("name", "flags", "managed")},
                              {k: new[k] for k in ("name", "flags", "managed")})
        self._mirror_audit(seq)
        self.publish()
        return self.entity(eid), True

    def add_exception(self, actor, eid, body, today=None):
        today = today or dt.datetime.now(model.TZ).date()
        with self._tx():
            ent = self.entity(eid)
            self._check_editor(actor, ent)
            ex, probs = model.validate_exception(ent, body, today)
            if probs:
                raise model.ValidationError(probs)
            xid = "x" + uuid.uuid4().hex[:10]
            self.db.execute("INSERT INTO exceptions(id,entity_id,kind,date,until,state,created_by,created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (xid, eid, ex["kind"], ex.get("date"), ex.get("until"), ex.get("state"), canon(actor), self.clock()))
            seq = self._audit(actor, "exception_add", eid, None, {"id": xid, **ex})
        self._mirror_audit(seq)
        self.publish()
        return xid

    def delete_exception(self, actor, xid):
        with self._tx():
            r = self.db.execute("SELECT * FROM exceptions WHERE id=?", (xid,)).fetchone()
            if not r:
                raise NotFound(xid)
            ent = self.entity(r["entity_id"])
            self._check_editor(actor, ent)
            self.db.execute("DELETE FROM exceptions WHERE id=?", (xid,))
            seq = self._audit(actor, "exception_delete", ent["id"], {"id": xid, "kind": r["kind"], "date": r["date"], "until": r["until"], "state": r["state"]}, None)
        self._mirror_audit(seq)
        self.publish()

    # ── tokens ────────────────────────────────────────────────────────────────
    def create_token(self, actor, name, role, expires=None, gateway=False):
        """Returns the secret ONCE; only its SHA-256 is stored. gateway=True marks a token of the portal gateway: only such a token may
        carry an X-Acting-User header (Task 28), which is then written into the audit rows of its calls."""
        import base64, secrets
        if role not in ("admin", "household", "readonly"):
            raise model.ValidationError([model.problem("value", "role", "role must be admin, household or readonly")])
        if not isinstance(name, str) or not model.VAR_KEY_RE.match(name.replace("-", "_")) :
            raise model.ValidationError([model.problem("format", "name", "token name: lowercase letters, digits, - and _")])
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        tid = "t" + uuid.uuid4().hex[:8]
        with self._tx():
            try:
                self.db.execute("INSERT INTO tokens(id,name,role,sha256,created,expires,gateway) VALUES (?,?,?,?,?,?,?)",
                                (tid, name, role, hashlib.sha256(secret.encode()).hexdigest(), self.clock(), expires, 1 if gateway else 0))
            except sqlite3.IntegrityError:
                raise model.ValidationError([model.problem("duplicate", "name", "a token with this name exists (revoke it first)")])
            seq = self._audit(actor, "token_create", None, None, {"id": tid, "name": name, "role": role, "expires": expires, "gateway": bool(gateway)})
        self._mirror_audit(seq)
        return tid, secret

    def revoke_token(self, actor, tid):
        with self._tx():
            r = self.db.execute("SELECT * FROM tokens WHERE id=? OR name=?", (tid, tid)).fetchone()
            if not r:
                raise NotFound(tid)
            self.db.execute("UPDATE tokens SET revoked=1 WHERE id=?", (r["id"],))
            seq = self._audit(actor, "token_revoke", None, {"id": r["id"], "name": r["name"], "role": r["role"]}, None)
        self._mirror_audit(seq)

    def tokens(self):
        with self.lock:
            return [{"id": r["id"], "name": r["name"], "role": r["role"], "created": r["created"], "expires": r["expires"],
                     "last_used": r["last_used"], "revoked": bool(r["revoked"]), "gateway": bool(r["gateway"])} for r in self.db.execute("SELECT * FROM tokens ORDER BY created")]

    def find_token(self, secret):
        h = hashlib.sha256(secret.encode()).hexdigest()
        with self.lock:
            rows = self.db.execute("SELECT * FROM tokens WHERE revoked=0").fetchall()
        import hmac
        found = None
        for r in rows:                      # constant-time compare against every stored hash
            if hmac.compare_digest(r["sha256"], h):
                found = r
        if not found:
            return None
        if found["expires"] and found["expires"] < self.clock():
            return None
        return {"token": found["id"], "name": found["name"], "role": found["role"], "gateway": bool(found["gateway"])}

    def touch_token(self, tid):
        try:
            with self.lock:
                self.db.execute("UPDATE tokens SET last_used=? WHERE id=?", (self.clock(), tid))
        except sqlite3.Error:               # pragma: no cover
            pass

    # ── applier results (written by the host-side applier, audited here) ─────
    def last_apply_audited(self):
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key='last_apply_audited'").fetchone()
        return json.loads(r["value"]) if r else None

    def record_apply(self, actor, status):
        """Appends 'applied' / 'not_applied' to the audit so a rejected apply is visible in the history of the change."""
        key = [status.get("seq"), status.get("result"), status.get("at")]
        with self._tx():
            seq = self._audit(actor, "applied" if status.get("result") == "applied" else "not_applied", None, None,
                              {"audit_seq": status.get("seq"), "set_hash": status.get("set_hash"), "detail": status.get("detail")})
            self.db.execute("INSERT INTO meta(key,value) VALUES('last_apply_audited',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canon(key),))
        self._mirror_audit(seq)

    # ── child policy: modelled and imported READ-ONLY (Phase E; AdGuard is never touched from here) ──
    def import_child_policy(self, actor, payload):
        """Replaces the modelled child policy with `payload` (admin only): devices (+ their windows), domain lists, always-on services.
        Nothing is written to AdGuard; the running policy job is untouched. Cutover is step (c)."""
        from . import child_render
        if actor["role"] != "admin":
            raise Forbidden("importing the child policy is admin-only")
        probs = []
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list) or not payload["devices"]:
            raise model.ValidationError([model.problem("type", "devices", "devices must be a non-empty list")])
        lists, lp = child_render.validate_lists(payload.get("lists", {}))
        probs += lp
        svc = payload.get("always_services", [])
        if not isinstance(svc, list) or any(s not in ("telegram",) for s in svc):
            probs.append(model.problem("value", "always_services", "always_services may only name 'telegram'"))
        devices, seen = [], set()
        for d in payload["devices"]:
            dp = child_render.validate_device(d)
            probs += dp
            if dp:
                continue
            eid = d["id"]
            if not model.ENTITY_ID_RE.match(eid) or eid in seen:
                probs.append(model.problem("format", "devices", "bad or duplicate device id %r" % eid))
                continue
            seen.add(eid)
            rules, rp = model.validate_rules({"id": eid, "kind": "child_window", "guardrails": {}}, d.get("rules", []))
            probs += [{**p, "message": "%s: %s" % (eid, p["message"])} for p in rp]
            devices.append({"id": eid, "name": d["name"], "ip": d["ip"], "mac": (d.get("mac") or "").lower(), "rules": rules})
        if probs:
            raise model.ValidationError(probs)
        with self._tx():
            before = {"devices": [e["id"] for e in self.entities() if e["kind"] == "child_window"]}
            self.db.execute("DELETE FROM exceptions WHERE entity_id IN (SELECT id FROM entities WHERE kind='child_window')")
            self.db.execute("DELETE FROM schedules WHERE entity_id IN (SELECT id FROM entities WHERE kind='child_window')")
            self.db.execute("DELETE FROM entities WHERE kind='child_window'")
            pos = (self.db.execute("SELECT COALESCE(MAX(position), 0) m FROM entities").fetchone()["m"]) + 1
            for i, d in enumerate(devices):
                if self.db.execute("SELECT 1 FROM entities WHERE id=?", (d["id"],)).fetchone():
                    raise model.ValidationError([model.problem("duplicate", "devices", "id %s clashes with a non-child entity" % d["id"])])
                self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                                (d["id"], "child_window", d["name"], None, 0, canon({}), canon({}), pos + i, canon({"ip": d["ip"], "mac": d["mac"]})))
                self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)",
                                (d["id"], 1, model.TZ_NAME, canon(d["rules"]), self.clock()))
            self.db.execute("INSERT INTO meta(key,value) VALUES('child_lists',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (canon({"lists": lists, "always_services": sorted(svc)}),))
            summary = {"devices": [d["id"] for d in devices], "lists": {k: len(v) for k, v in lists.items()},
                       "always_services": sorted(svc), "sha256": hashlib.sha256(canon({"d": devices, "l": lists, "s": svc}).encode()).hexdigest()[:16]}
            seq = self._audit(actor, "import_child_policy", None, before, summary)
        self._mirror_audit(seq)
        self.publish()
        return summary

    def child_policy(self, with_lists=True):
        """The modelled policy in the shape child_render.render_block takes."""
        devices = []
        for e in self.entities():
            if e["kind"] == "child_window":
                devices.append({"id": e["id"], "name": e["name"], "ip": e["meta"].get("ip"), "mac": e["meta"].get("mac"),
                                "rules": self.schedule(e["id"])["rules"]})
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key='child_lists'").fetchone()
        stored = json.loads(r["value"]) if r else {"lists": {n: [] for n in ("allow_always", "allow_weekdays", "allow_extra", "deny_extra")}, "always_services": []}
        out = {"devices": devices, "always_services": stored["always_services"]}
        if with_lists:
            out["lists"] = stored["lists"]
        return out
