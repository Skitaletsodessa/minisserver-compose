"""People, web login (password + TOTP), sessions, setup links, enrolment codes, phones (device tokens). All rules of ADR 0007.
Nothing in this module returns or logs a secret except the one value each function documents as 'shown once'."""
import re
import time
from . import auth, config
from .auth import GwError
from .db import sha, iso

ROLES = ("admin", "household", "readonly")
NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,60}$")
FAIL_WINDOW = 900
ACCOUNT_FREE_FAILS = 3          # the first three wrong passwords of an account cost nothing, then the delay doubles
IP_FAIL_LIMIT = 20
ENROL_FAIL_LIMIT = 10


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeezzijklmnoprstufhccss_y_eua"))).strip("-")
    return (s or "person")[:30]


class People:
    def __init__(self, db, hub=None, clock=time.time):
        self.db, self.hub, self.clock = db, hub, clock

    # ── people ────────────────────────────────────────────────────────────────
    def _row(self, r):
        n = self.db.q1("SELECT COUNT(*) c FROM phones WHERE person_id=? AND revoked=0 AND expires>?", (r["id"], self.clock()))["c"]
        cred = self.db.q1("SELECT pw_hash, totp_seed FROM credentials WHERE person_id=?", (r["id"],))
        return {"id": r["id"], "name": r["name"], "role": r["role"], "parent": bool(r["parent"]), "active": bool(r["active"]),
                "web_login_enabled": bool(r["web_login"]), "has_web_credentials": bool(cred and cred["pw_hash"] and cred["totp_seed"]), "phones": n}

    def list(self):
        return [self._row(r) for r in self.db.q("SELECT * FROM people ORDER BY created, id")]

    def get(self, pid):
        r = self.db.q1("SELECT * FROM people WHERE id=?", (pid,))
        if not r:
            raise GwError(404, "not_found", "no such person", {"id": pid})
        return self._row(r)

    def create(self, actor, name, role, parent=False, web_login=None):
        if not isinstance(name, str) or not NAME_RE.match(name.strip()):
            raise GwError(422, "invalid_name", "the name must be 1-60 printable characters")
        if role not in ROLES:
            raise GwError(422, "invalid_role", "role must be admin, household or readonly", {"allowed": list(ROLES)})
        pid = slug(name)
        with self.db.tx():
            if self.db.q1("SELECT 1 FROM people WHERE id=?", (pid,)):
                raise GwError(409, "person_exists", "a person with this id exists", {"id": pid})
            web = (1 if role == "admin" else 0) if web_login is None else (1 if web_login else 0)
            self.db.x("INSERT INTO people(id,name,role,parent,active,web_login,created) VALUES (?,?,?,?,1,?,?)", (pid, name.strip(), role, 1 if parent else 0, web, self.clock()))
        self.db.audit("person_create", person=actor.get("person_id"), phone=actor.get("phone_id"), target=pid, after={"role": role, "parent": bool(parent)}, ip=actor.get("ip"))
        return self.get(pid)

    def patch(self, actor, pid, fields):
        cur = self.get(pid)
        allowed = {"name", "role", "parent", "active", "web_login_enabled"}
        unknown = set(fields) - allowed
        if unknown:
            raise GwError(422, "unknown_field", "unknown field(s)", {"fields": sorted(unknown)})
        new = dict(cur)
        if "name" in fields:
            if not isinstance(fields["name"], str) or not NAME_RE.match(fields["name"].strip()):
                raise GwError(422, "invalid_name", "the name must be 1-60 printable characters")
            new["name"] = fields["name"].strip()
        if "role" in fields:
            if fields["role"] not in ROLES:
                raise GwError(422, "invalid_role", "role must be admin, household or readonly")
            new["role"] = fields["role"]
        for k in ("parent", "active", "web_login_enabled"):
            if k in fields:
                if not isinstance(fields[k], bool):
                    raise GwError(422, "invalid_value", "%s must be true or false" % k)
                new[k] = fields[k]
        # the last active admin can neither be demoted nor deactivated: nobody could manage the system any more
        if cur["role"] == "admin" and cur["active"] and (new["role"] != "admin" or not new["active"]):
            if self.db.q1("SELECT COUNT(*) c FROM people WHERE role='admin' AND active=1 AND id<>?", (pid,))["c"] == 0:
                raise GwError(409, "last_admin", "this is the last active admin")
        with self.db.tx():
            self.db.x("UPDATE people SET name=?, role=?, parent=?, active=?, web_login=? WHERE id=?",
                      (new["name"], new["role"], 1 if new["parent"] else 0, 1 if new["active"] else 0, 1 if new["web_login_enabled"] else 0, pid))
            if not new["active"] or not new["web_login_enabled"]:
                self.db.x("DELETE FROM sessions WHERE person_id=?", (pid,))
            if not new["active"]:                                            # a deactivated person's phones stop working at once
                for r in self.db.q("SELECT id FROM phones WHERE person_id=? AND revoked=0", (pid,)):
                    self.db.x("UPDATE phones SET revoked=1 WHERE id=?", (r["id"],))
                    if self.hub:
                        self.hub.kick(r["id"])
        self.db.audit("person_patch", person=actor.get("person_id"), phone=actor.get("phone_id"), target=pid,
                      before={k: cur[k] for k in ("role", "parent", "active", "web_login_enabled")}, after={k: new[k] for k in ("role", "parent", "active", "web_login_enabled")}, ip=actor.get("ip"))
        return self.get(pid)

    # ── setup links (Ivan sets his OWN password and TOTP; nobody else sees them) ──
    def create_setup_link(self, actor, pid):
        p = self.get(pid)
        if not p["active"]:
            raise GwError(409, "person_inactive", "the person is not active")
        if not p["web_login_enabled"]:
            raise GwError(409, "web_login_disabled", "web login is not enabled for this person")
        raw = auth.token(24)
        with self.db.tx():
            self.db.x("UPDATE setup_links SET used=1 WHERE person_id=? AND used=0", (pid,))           # an older unused link dies
            self.db.x("INSERT INTO setup_links(id,person_id,token_hash,expires,used,created) VALUES (?,?,?,?,0,?)",
                      ("s" + auth.token(6), pid, sha(raw), self.clock() + config.SETUP_TTL_S, self.clock()))
        self.db.audit("setup_link_create", person=actor.get("person_id"), phone=actor.get("phone_id"), target=pid, ip=actor.get("ip"))
        return raw, self.clock() + config.SETUP_TTL_S

    def _link(self, raw):
        r = self.db.q1("SELECT * FROM setup_links WHERE token_hash=?", (sha(raw or ""),))
        if not r or r["used"] or r["expires"] < self.clock():
            raise GwError(410, "setup_link_invalid", "this link is used up or expired")
        return r

    def setup_begin(self, raw):
        """GET of the setup page: returns (person, pending seed). The seed is created once per link and kept server-side."""
        r = self._link(raw)
        seed = self.db.meta_get("setupseed:" + r["id"])
        if not seed:
            seed = auth.new_totp_seed()
            self.db.meta_set("setupseed:" + r["id"], seed)
        return self.get(r["person_id"]), seed

    def setup_complete(self, raw, password, code, ip=None):
        r = self._link(raw)
        problem = auth.password_problem(password)
        if problem:
            raise GwError(422, "weak_password", problem)
        seed = self.db.meta_get("setupseed:" + r["id"])
        if not seed:
            raise GwError(409, "setup_not_started", "open the setup page first")
        step = auth.verify_totp(seed, (code or "").strip(), 0, now=self.clock())
        if step is None:
            self.db.audit("setup_failed", person=r["person_id"], ok=False, ip=ip)
            raise GwError(422, "bad_totp", "the code from the authenticator app does not match")
        with self.db.tx():
            self.db.x("INSERT INTO credentials(person_id,pw_hash,totp_seed,totp_last_step,updated) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(person_id) DO UPDATE SET pw_hash=excluded.pw_hash, totp_seed=excluded.totp_seed, totp_last_step=excluded.totp_last_step, updated=excluded.updated",
                      (r["person_id"], auth.hash_password(password), seed, step, self.clock()))
            self.db.x("UPDATE setup_links SET used=1 WHERE id=?", (r["id"],))
            self.db.x("DELETE FROM meta WHERE key=?", ("setupseed:" + r["id"],))
            self.db.x("DELETE FROM sessions WHERE person_id=?", (r["person_id"],))
        self.db.audit("setup_complete", person=r["person_id"], target=r["person_id"], ip=ip)

    def reset_credentials(self, actor, pid):
        """Break-glass (gwctl): forget the password/TOTP and all sessions of a person; they must use a new setup link."""
        self.get(pid)
        with self.db.tx():
            self.db.x("DELETE FROM credentials WHERE person_id=?", (pid,))
            self.db.x("DELETE FROM sessions WHERE person_id=?", (pid,))
        self.db.audit("credentials_reset", person=actor.get("person_id"), target=pid, ip=actor.get("ip"))

    # ── login / sessions ──────────────────────────────────────────────────────
    def _fails(self, account=None, ip=None):
        since = self.clock() - FAIL_WINDOW
        if account is not None:
            return self.db.q("SELECT ts FROM login_attempts WHERE ok=0 AND account=? AND ts>? ORDER BY ts", (account, since))
        return self.db.q("SELECT ts FROM login_attempts WHERE ok=0 AND ip=? AND ts>? ORDER BY ts", (ip, since))

    def _throttle(self, account, ip):
        now = self.clock()
        f = self._fails(ip=ip)
        if len(f) >= IP_FAIL_LIMIT:
            raise GwError(429, "too_many_attempts", "too many failed attempts from this address", {"retry_after_s": int(f[0]["ts"] + FAIL_WINDOW - now) + 1},
                          {"Retry-After": str(int(f[0]["ts"] + FAIL_WINDOW - now) + 1)})
        fa = self._fails(account=account)
        if len(fa) > ACCOUNT_FREE_FAILS:
            wait = min(2 ** (len(fa) - ACCOUNT_FREE_FAILS), 900)
            ready = fa[-1]["ts"] + wait
            if now < ready:
                raise GwError(429, "account_delay", "wait before trying this account again", {"retry_after_s": int(ready - now) + 1}, {"Retry-After": str(int(ready - now) + 1)})

    def login(self, username, password, totp, ip):
        """Returns (session cookie value, session dict). Unknown account, wrong password and wrong code all answer the same 401."""
        account = (username or "").strip().lower()[:60]
        self._throttle(account, ip)
        r = self.db.q1("SELECT * FROM people WHERE id=? OR lower(name)=?", (account, account))
        cred = self.db.q1("SELECT * FROM credentials WHERE person_id=?", (r["id"],)) if r else None
        ok = False
        step = None
        if r and cred and cred["pw_hash"]:
            ok = auth.verify_password(password or "", cred["pw_hash"])
        else:
            auth.burn_password_time(password or "")
        if ok:
            need_totp = r["role"] == "admin" or bool(cred["totp_seed"])
            if need_totp:
                step = auth.verify_totp(cred["totp_seed"] or "", (totp or "").strip(), cred["totp_last_step"], now=self.clock()) if cred["totp_seed"] else None
                ok = step is not None
        if ok and not (r["active"] and r["web_login"]):
            ok = False
        self.db.x("INSERT INTO login_attempts(ts,ip,account,ok) VALUES (?,?,?,?)", (self.clock(), ip, account, 1 if ok else 0))
        if not ok:
            self.db.audit("login_failed", person=r["id"] if r else None, target=account[:60], ok=False, ip=ip)
            raise GwError(401, "invalid_credentials", "invalid username, password or code")
        if step:
            self.db.x("UPDATE credentials SET totp_last_step=? WHERE person_id=?", (step, r["id"]))
        raw = auth.token(32)
        now = self.clock()
        csrf = auth.token(16)
        self.db.x("INSERT INTO sessions(id,person_id,csrf,created,last,abs_exp,ip) VALUES (?,?,?,?,?,?,?)", (sha(raw), r["id"], csrf, now, now, now + config.SESSION_ABS_S, ip))
        self.db.audit("login", person=r["id"], ip=ip)
        return raw, self.session_view(sha(raw))

    def session_view(self, sid):
        s = self.db.q1("SELECT * FROM sessions WHERE id=?", (sid,))
        p = self.db.q1("SELECT * FROM people WHERE id=?", (s["person_id"],))
        return {"person": {"id": p["id"], "name": p["name"], "role": p["role"], "parent": bool(p["parent"])}, "csrf": s["csrf"],
                "expires_at": iso(s["abs_exp"]), "idle_expires_at": iso(min(s["abs_exp"], s["last"] + config.SESSION_IDLE_S))}

    def session_principal(self, raw):
        """Cookie value -> principal dict or None. Applies the idle and absolute timeouts and slides the idle clock."""
        if not raw:
            return None
        sid = sha(raw)
        s = self.db.q1("SELECT * FROM sessions WHERE id=?", (sid,))
        now = self.clock()
        if not s:
            return None
        if now > s["abs_exp"] or now > s["last"] + config.SESSION_IDLE_S:
            self.db.x("DELETE FROM sessions WHERE id=?", (sid,))
            return None
        p = self.db.q1("SELECT * FROM people WHERE id=?", (s["person_id"],))
        if not p or not p["active"] or not p["web_login"]:
            self.db.x("DELETE FROM sessions WHERE id=?", (sid,))
            return None
        if now - s["last"] > 60:
            self.db.x("UPDATE sessions SET last=? WHERE id=?", (now, sid))
        return {"person_id": p["id"], "name": p["name"], "role": p["role"], "parent": bool(p["parent"]), "phone_id": None, "via": "session", "csrf": s["csrf"], "sid": sid}

    def logout(self, principal):
        if principal.get("sid"):
            self.db.x("DELETE FROM sessions WHERE id=?", (principal["sid"],))
            self.db.audit("logout", person=principal["person_id"])

    # ── enrolment ─────────────────────────────────────────────────────────────
    def _active_phones(self, pid=None):
        if pid:
            return self.db.q1("SELECT COUNT(*) c FROM phones WHERE person_id=? AND revoked=0 AND expires>?", (pid, self.clock()))["c"]
        return self.db.q1("SELECT COUNT(*) c FROM phones WHERE revoked=0 AND expires>?", (self.clock(),))["c"]

    def create_enrolment_code(self, actor, pid):
        p = self.get(pid)
        if not p["active"]:
            raise GwError(409, "person_inactive", "the person is not active")
        if self._active_phones(pid) >= config.MAX_PHONES_PER_PERSON or self._active_phones() >= config.MAX_PHONES_TOTAL:
            raise GwError(409, "device_limit", "the device limit is reached: revoke a phone first",
                          {"per_person": config.MAX_PHONES_PER_PERSON, "total": config.MAX_PHONES_TOTAL})
        raw = auth.new_code()
        exp = self.clock() + config.ENROL_TTL_S
        with self.db.tx():
            self.db.x("UPDATE enrol_codes SET used=1 WHERE person_id=? AND used=0", (pid,))             # one live code per person
            self.db.x("INSERT INTO enrol_codes(id,person_id,code_hash,expires,used,created,created_by) VALUES (?,?,?,?,0,?,?)",
                      ("c" + auth.token(6), pid, sha(raw), exp, self.clock(), actor.get("person_id")))
        self.db.audit("enrol_code_create", person=actor.get("person_id"), phone=actor.get("phone_id"), target=pid, ip=actor.get("ip"))
        return raw, exp

    def enrol(self, code, device, ip):
        """Trades a code for a device token (returned ONCE). The same refusal for unknown, used and expired codes."""
        now = self.clock()
        bad = self.db.q1("SELECT COUNT(*) c FROM login_attempts WHERE ok=0 AND account='enrol' AND ip=? AND ts>?", (ip, now - FAIL_WINDOW))["c"]
        if bad >= ENROL_FAIL_LIMIT:
            raise GwError(429, "too_many_attempts", "too many failed enrolment attempts from this address", {"retry_after_s": FAIL_WINDOW}, {"Retry-After": str(FAIL_WINDOW)})
        label = (device or {}).get("label")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
            raise GwError(400, "invalid_device", "device.label (1-60 characters) is required")
        norm = auth.norm_code(code)
        r = self.db.q1("SELECT * FROM enrol_codes WHERE code_hash=?", (sha(norm),)) if len(norm) == 12 else None
        if not r or r["used"] or r["expires"] < now:
            self.db.x("INSERT INTO login_attempts(ts,ip,account,ok) VALUES (?,?,?,0)", (now, ip, "enrol"))
            self.db.audit("enrol_failed", ok=False, ip=ip)
            raise GwError(403, "invalid_code", "the enrolment code is invalid, used or expired")
        p = self.db.q1("SELECT * FROM people WHERE id=?", (r["person_id"],))
        if not p or not p["active"]:
            raise GwError(403, "invalid_code", "the enrolment code is invalid, used or expired")
        if self._active_phones(p["id"]) >= config.MAX_PHONES_PER_PERSON or self._active_phones() >= config.MAX_PHONES_TOTAL:
            raise GwError(409, "device_limit", "the device limit is reached", {"per_person": config.MAX_PHONES_PER_PERSON, "total": config.MAX_PHONES_TOTAL})
        raw = auth.token(32)
        phone_id = "p-" + auth.token(5).lower().replace("_", "x").replace("-", "y")[:8]
        exp = now + config.PHONE_SLIDING_S
        with self.db.tx():
            if not self.db.q1("SELECT 1 FROM enrol_codes WHERE id=? AND used=0", (r["id"],)):                # lost a race: single use
                raise GwError(403, "invalid_code", "the enrolment code is invalid, used or expired")
            self.db.x("UPDATE enrol_codes SET used=1 WHERE id=?", (r["id"],))
            self.db.x("INSERT INTO phones(id,person_id,label,model,platform,app_version,token_hash,created,last_seen,expires,revoked) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                      (phone_id, p["id"], label.strip(), str(device.get("model") or "")[:60] or None, str(device.get("platform") or "")[:30] or None,
                       str(device.get("app_version") or "")[:30] or None, sha(raw), now, now, exp))
        self.db.audit("enrol", person=p["id"], phone=phone_id, target=label.strip()[:60], ip=ip)
        return {"phone_id": phone_id, "token": raw, "person": {"id": p["id"], "name": p["name"], "role": p["role"], "parent": bool(p["parent"])}, "expires_at": iso(exp)}

    # ── phones ────────────────────────────────────────────────────────────────
    def phone_principal(self, raw):
        """Bearer token -> principal or None. Revocation, expiry and a deactivated person all take effect on the NEXT request."""
        if not raw:
            return None
        r = self.db.q1("SELECT * FROM phones WHERE token_hash=?", (sha(raw),))
        now = self.clock()
        if not r or r["revoked"] or r["expires"] < now:
            return None
        p = self.db.q1("SELECT * FROM people WHERE id=?", (r["person_id"],))
        if not p or not p["active"]:
            return None
        if not r["last_seen"] or now - r["last_seen"] > 60:                      # slide the 90 days, but write at most once a minute
            self.db.x("UPDATE phones SET last_seen=?, expires=? WHERE id=?", (now, now + config.PHONE_SLIDING_S, r["id"]))
        return {"person_id": p["id"], "name": p["name"], "role": p["role"], "parent": bool(p["parent"]), "phone_id": r["id"], "via": "device"}

    def phone_principal_valid(self, phone_id):
        """Used by open event streams: is this phone's token still good? (revocation, expiry, a deactivated person)"""
        r = self.db.q1("SELECT ph.revoked, ph.expires, p.active FROM phones ph JOIN people p ON p.id=ph.person_id WHERE ph.id=?", (phone_id,))
        return bool(r and not r["revoked"] and r["expires"] >= self.clock() and r["active"])

    def session_principal_raw_valid(self, sid):
        s = self.db.q1("SELECT * FROM sessions WHERE id=?", (sid,))
        now = self.clock()
        return bool(s and now <= s["abs_exp"] and now <= s["last"] + config.SESSION_IDLE_S)

    def phones(self, only_person=None):
        q = "SELECT ph.*, p.name AS pname FROM phones ph JOIN people p ON p.id=ph.person_id"
        a = ()
        if only_person:
            q += " WHERE ph.person_id=?"
            a = (only_person,)
        q += " ORDER BY ph.created"
        return [{"id": r["id"], "person_id": r["person_id"], "person_name": r["pname"], "label": r["label"], "model": r["model"], "platform": r["platform"],
                 "created_at": iso(r["created"]), "last_seen": iso(r["last_seen"]), "expires_at": iso(r["expires"]), "revoked": bool(r["revoked"])} for r in self.db.q(q, a)]

    def revoke_phone(self, actor, phone_id):
        r = self.db.q1("SELECT * FROM phones WHERE id=?", (phone_id,))
        if not r:
            raise GwError(404, "not_found", "no such phone", {"id": phone_id})
        if actor["role"] != "admin" and r["person_id"] != actor["person_id"]:
            raise GwError(403, "forbidden", "you may revoke only your own phones")
        self.db.x("UPDATE phones SET revoked=1 WHERE id=?", (phone_id,))
        if self.hub:
            self.hub.kick(phone_id)
        self.db.audit("phone_revoke", person=actor.get("person_id"), phone=actor.get("phone_id"), target=phone_id, before={"person": r["person_id"], "label": r["label"]}, ip=actor.get("ip"))
