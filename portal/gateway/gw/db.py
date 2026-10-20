"""SQLite store of the gateway: people, credentials, sessions, enrolment codes, phones, idempotency keys, audit (hash-chained), data tables.
One connection, one lock, WAL. Every secret is stored hashed (SHA-256 for random tokens, scrypt for passwords); a TOTP seed has to be
recoverable to verify a code, so it is stored as is (the data directory is mode 700 and Tier-0 backup material, encrypted in restic)."""
import os
import json
import time
import hashlib
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS people(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, parent INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
  web_login INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(
  person_id TEXT PRIMARY KEY REFERENCES people(id), pw_hash TEXT, totp_seed TEXT, totp_last_step INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS setup_links(
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), token_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), csrf TEXT NOT NULL, created REAL NOT NULL, last REAL NOT NULL, abs_exp REAL NOT NULL, ip TEXT);
CREATE TABLE IF NOT EXISTS enrol_codes(
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), code_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, created_by TEXT);
CREATE TABLE IF NOT EXISTS phones(
  id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), label TEXT NOT NULL, model TEXT, platform TEXT, app_version TEXT,
  token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, last_seen REAL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS idem(
  scope TEXT NOT NULL, key_hash TEXT NOT NULL, req_hash TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(scope, key_hash));
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, ip TEXT, account TEXT, ok INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_ts ON login_attempts(ts);
CREATE TABLE IF NOT EXISTS device_meta(device_id TEXT PRIMARY KEY, kind TEXT NOT NULL, power_sensor TEXT, household_switchable INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS icon_rules(keyword TEXT PRIMARY KEY, icon TEXT NOT NULL, priority INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS service_tiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL, url TEXT NOT NULL, probe TEXT NOT NULL, visible_to TEXT NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS vacuum_runs(vacuum_id TEXT PRIMARY KEY, scenario TEXT, started REAL, person TEXT, last_result TEXT, last_result_ts REAL);
CREATE TABLE IF NOT EXISTS filter_changes(
  id TEXT PRIMARY KEY, at REAL NOT NULL, by TEXT, action TEXT NOT NULL, list_id INTEGER, list_name TEXT, result TEXT NOT NULL, steps TEXT, reason TEXT,
  snapshot TEXT, finished REAL);
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, person TEXT, phone TEXT, action TEXT NOT NULL, target TEXT, before TEXT, after TEXT,
  ok INTEGER NOT NULL, ip TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
"""
GENESIS = "0" * 64


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha(s):
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None


class DB:
    def __init__(self, data_dir, clock=time.time):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.dir = data_dir
        self.clock = clock
        self.lock = threading.RLock()
        self.c = sqlite3.connect(os.path.join(data_dir, "gateway.db"), check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.execute("PRAGMA synchronous=FULL")
        self.c.executescript(SCHEMA)
        if "display_name" not in [r["name"] for r in self.c.execute("PRAGMA table_info(device_meta)")]:      # added after the first deployment
            self.c.execute("ALTER TABLE device_meta ADD COLUMN display_name TEXT")
        if "switch_mode" not in [r["name"] for r in self.c.execute("PRAGMA table_info(device_meta)")]:        # Task 29: timed or plain On button (NULL = take the seed default)
            self.c.execute("ALTER TABLE device_meta ADD COLUMN switch_mode TEXT")
        self.jsonl = os.path.join(data_dir, "audit.jsonl")

    def tx(self):
        db = self

        class _T:
            def __enter__(s):
                db.lock.acquire()
                db.c.execute("BEGIN IMMEDIATE")

            def __exit__(s, et, ev, tb):
                try:
                    db.c.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db.lock.release()
        return _T()

    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args)

    # ── meta ──
    def meta_get(self, key, default=None):
        r = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ── audit (hash chain; the JSONL mirror is written after commit) ──
    def audit(self, action, person=None, phone=None, target=None, before=None, after=None, ok=True, ip=None):
        """Appends one row. NEVER pass a secret in before/after/target. Returns seq."""
        with self.lock:
            last = self.c.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            ts = iso(self.clock())
            body = canon({"ts": ts, "person": person, "phone": phone, "action": action, "target": target, "before": before, "after": after, "ok": bool(ok), "ip": ip})
            h = sha(prev + body)
            cur = self.c.execute("INSERT INTO audit(ts,person,phone,action,target,before,after,ok,ip,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (ts, person, phone, action, target, canon(before), canon(after), 1 if ok else 0, ip, prev, h))
            seq = cur.lastrowid
            try:
                with open(self.jsonl, "a", encoding="utf-8") as f:
                    f.write(canon({"seq": seq, "ts": ts, "person": person, "phone": phone, "action": action, "target": target, "ok": bool(ok), "hash": h}) + "\n")
            except OSError:
                pass
            return seq

    def verify_audit(self):
        prev, n = GENESIS, 0
        for r in self.q("SELECT * FROM audit ORDER BY seq"):
            body = canon({"ts": r["ts"], "person": r["person"], "phone": r["phone"], "action": r["action"], "target": r["target"],
                          "before": json.loads(r["before"]), "after": json.loads(r["after"]), "ok": bool(r["ok"]), "ip": r["ip"]})
            if r["prev_hash"] != prev or sha(prev + body) != r["hash"]:
                return False, n, r["seq"]
            prev, n = r["hash"], n + 1
        return True, n, None

    def audit_rows(self, limit=50, before=None, action=None, person=None, device=None, about=None):
        q, a = "SELECT * FROM audit WHERE 1=1", []
        if before:
            q += " AND seq<?"
            a.append(before)
        if action:
            q += " AND action=?"
            a.append(action)
        if person:
            q += " AND person=?"
            a.append(person)
        if device:
            q += " AND (target=? OR phone=?)"
            a += [device, device]
        if about:                                   # non-admins see only entries about themselves
            q += " AND person=?"
            a.append(about)
        q += " ORDER BY seq DESC LIMIT ?"
        a.append(max(1, min(int(limit), 500)))
        return [{"seq": r["seq"], "ts": r["ts"], "action": r["action"], "person": r["person"], "device": r["phone"], "target": r["target"],
                 "before": json.loads(r["before"]), "after": json.loads(r["after"]), "ok": bool(r["ok"]), "source_ip": r["ip"]} for r in self.q(q, a)]

    def checksum(self):
        h = hashlib.sha256()
        for t in ("people", "device_meta", "icon_rules", "service_tiles"):
            for r in self.q("SELECT * FROM %s ORDER BY 1" % t):
                h.update(canon(dict(r)).encode())
        return h.hexdigest()
