"""The children's policy in the schedule service (Task 29 Part 2): windows (as `child_window` entities and their schedules), time-boxed exceptions, per-child
domain lists, the import/migration, and the SNAPSHOT FILE the host policy job executes from (ADR 0008).

The service never talks to AdGuard. It validates (guardrails, invariants: rejected, never clamped), audits (hash chain) and publishes `out/child-policy.json`
atomically with a checksum; the host job renders AdGuard's rules from that file, evaluates time itself (windows, exception expiry) and keeps working from its
last good copy when the service is down.
"""
import json
import uuid
import hashlib
import datetime as dt

from . import model, child_render, child_invariants

# Guardrails = TODAY'S behaviour (Ivan, 2026-10-21: "keep the current values"): windows only inside 08:00-21:00 (today's 16:00-19:00 Mon-Sat is inside), at most 240 min a day
# of open YouTube/Roblox time (today 180). NO cap and NO ceiling on temporary exceptions: they run from a minute to the end of the day (23:59), "everything"
# included, exactly as the old child-ui/child-override allowed. exception_latest_end / all_latest_end / max_exception_minutes still exist as optional guardrails
# (schedctl guardrails, admin-only, audited) but are not set. The earlier proposal (360 min, 22:00, all <= 21:00) was STRICTER than today and was dropped.
PROPOSED_GUARDRAILS = {"allowed_from": "08:00", "allowed_to": "21:00", "max_open_minutes_per_day": 240}
CHILD_FLAGS = {"household_editable": True, "guardrails_proposed": False}
EXCEPTION_GRACE_S = 120          # an expired exception stays in the data this long (the host job drops it by its own clock first)


def _s():
    from . import store
    return store


class ChildMixin:
    # ── reads ─────────────────────────────────────────────────────────────────────────────────────────────────────────
    def child_devices(self):
        return [e for e in self.entities() if e["kind"] == "child_window"]

    def _child_entity(self, eid):
        s = _s()
        ent = self.entity(eid)
        if ent["kind"] != "child_window":
            raise s.NotFound(eid)
        return ent

    def child_exceptions(self, eid=None, active_only=True, now=None):
        now = now or self._now()
        q, a = "SELECT * FROM child_exceptions WHERE 1=1", []
        if eid:
            q += " AND entity_id=?"
            a.append(eid)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY created_at, id", a).fetchall()
        out = []
        for r in rows:
            exp = dt.datetime.fromisoformat(r["expires_at"])
            if active_only and (r["status"] != "active" or exp <= now):
                continue
            by = json.loads(r["created_by"])
            out.append({"id": r["id"], "entity": r["entity_id"], "target": r["target"], "created_at": r["created_at"], "expires_at": r["expires_at"],
                        "expires_ts": int(exp.timestamp()), "status": r["status"], "created_by": (by.get("acting") or {}).get("person") or by.get("name"),
                        "remaining_s": max(0, int((exp - now).total_seconds()))})
        return out

    def child_domains(self, eid):
        with self.lock:
            rows = self.db.execute("SELECT kind, domain FROM child_domains WHERE entity_id=? ORDER BY domain", (eid,)).fetchall()
        out = {"allow": [], "deny": []}
        for r in rows:
            out[r["kind"]].append(r["domain"])
        return out

    def child_status(self, eid, now=None):
        now = now or self._now()
        ent = self._child_entity(eid)
        sch = self.schedule(eid)
        ex = self.child_exceptions(eid, now=now)
        st = model.child_status(sch["rules"], ex, now)
        st["exceptions"] = ex
        return st

    def child_detail(self, eid, now=None):
        now = now or self._now()
        ent = self._child_entity(eid)
        sch = self.schedule(eid)
        return {"id": eid, "name": ent["name"], "ip": ent["meta"].get("ip"), "mac": ent["meta"].get("mac"), "version": sch["version"], "tz": sch["tz"],
                "updated_at": sch["updated_at"], "rules": sch["rules"], "guardrails": ent["guardrails"], "flags": ent["flags"],
                "exceptions": self.child_exceptions(eid, now=now), "domains": self.child_domains(eid), "status": self.child_status(eid, now)}

    def child_house_lists(self):
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key='child_lists'").fetchone()
        return json.loads(r["value"]) if r else {"lists": {n: [] for n in ("allow_always", "allow_weekdays", "allow_extra", "deny_extra")}, "always_services": []}

    def child_policy(self, with_lists=True):
        """The modelled policy in the shape child_render.render_block takes (legacy house-wide extra lists included, per-device lists added)."""
        devices = []
        for e in self.child_devices():
            dom = self.child_domains(e["id"])
            devices.append({"id": e["id"], "name": e["name"], "ip": e["meta"].get("ip"), "mac": e["meta"].get("mac"), "rules": self.schedule(e["id"])["rules"],
                            "allow_extra": dom["allow"], "deny_extra": dom["deny"]})
        stored = self.child_house_lists()
        out = {"devices": devices, "always_services": stored["always_services"]}
        if with_lists:
            out["lists"] = stored["lists"]
        return out

    # ── the snapshot the host job executes from ───────────────────────────────────────────────────────────────────────
    def child_snapshot(self, now=None):
        now = now or self._now()
        stored = self.child_house_lists()
        devs = []
        for e in self.child_devices():
            dom = self.child_domains(e["id"])
            sch = self.schedule(e["id"])
            devs.append({"id": e["id"], "name": e["name"], "ip": e["meta"].get("ip"), "mac": e["meta"].get("mac"), "rules": sch["rules"], "version": sch["version"],
                         "allow_extra": dom["allow"], "deny_extra": dom["deny"], "guardrails": e["guardrails"],
                         "exceptions": [{k: x[k] for k in ("id", "target", "created_at", "expires_at", "expires_ts", "created_by")} for x in self.child_exceptions(e["id"], now=now)]})
        snap = {"schema": 1, "audit_seq": self.audit_seq(), "generated_at": now.isoformat(timespec="seconds"), "always_services": sorted(stored["always_services"]),
                "guardrails_proposed": any(e["flags"].get("guardrails_proposed") for e in self.child_devices()),
                "lists": {k: sorted(v) for k, v in stored["lists"].items()}, "devices": devs}
        # the checksum covers the POLICY, not the moment: generated_at and audit_seq change on every unrelated audit row and must not move it
        core = {k: v for k, v in snap.items() if k not in ("generated_at", "audit_seq")}
        snap["checksum"] = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        return snap

    def _publish_child(self):
        """Called by publish(): out/child-policy.json (atomic). Not written when the model has no child device yet; never publishes a snapshot that breaks
        an invariant (the previous file stays, the problem is health-visible)."""
        from .store import atomic_write
        import os
        self.child_publish_error = None
        if not self.child_devices():
            return
        snap = self.child_snapshot()
        probs = child_invariants.verify_snapshot(snap)
        if probs:
            self.child_publish_error = "; ".join(p["message"] for p in probs[:3])
            return
        path = os.path.join(self.out, "child-policy.json")
        try:
            if json.load(open(path)).get("checksum") == snap["checksum"]:
                return                                              # nothing changed in the policy itself
        except (OSError, ValueError):
            pass
        atomic_write(path, json.dumps(snap, indent=1, sort_keys=True, ensure_ascii=False) + "\n")

    # ── writes: exceptions ────────────────────────────────────────────────────────────────────────────────────────────
    def add_child_exception(self, actor, eid, target, minutes=None, until_eod=False):
        s = _s()
        with self._tx():
            ent = self._child_entity(eid)
            self._check_editor(actor, ent)
            now = self._now()
            expires, probs = model.validate_child_exception(ent, target, minutes, until_eod, now)
            if probs:
                raise model.ValidationError(probs)
            for x in self.child_exceptions(eid, now=now):           # one exception per target: the new one replaces the old
                if x["target"] == target:
                    self.db.execute("UPDATE child_exceptions SET status='replaced', ended_at=? WHERE id=?", (now.isoformat(timespec="seconds"), x["id"]))
            xid = "c" + uuid.uuid4().hex[:10]
            self.db.execute("INSERT INTO child_exceptions(id,entity_id,target,created_at,expires_at,created_by,status,ended_at) VALUES (?,?,?,?,?,?,?,?)",
                            (xid, eid, target, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds"), s.canon(actor), "active", None))
            seq = self._audit(actor, "child_exception_add", eid, None, {"id": xid, "target": target, "expires_at": expires.isoformat(timespec="seconds"),
                                                                        "minutes": minutes, "until_end_of_day": bool(until_eod)})
        self._mirror_audit(seq)
        self.publish()
        return [x for x in self.child_exceptions(eid) if x["id"] == xid][0]

    def cancel_child_exception(self, actor, eid, xid):
        s = _s()
        with self._tx():
            ent = self._child_entity(eid)
            self._check_editor(actor, ent)
            now = self._now()
            row = self.db.execute("SELECT * FROM child_exceptions WHERE id=? AND entity_id=?", (xid, eid)).fetchone()
            if not row or row["status"] != "active" or dt.datetime.fromisoformat(row["expires_at"]) <= now:
                raise s.NotFound(xid)
            self.db.execute("UPDATE child_exceptions SET status='cancelled', ended_at=? WHERE id=?", (now.isoformat(timespec="seconds"), xid))
            seq = self._audit(actor, "child_exception_cancel", eid, {"id": xid, "target": row["target"], "expires_at": row["expires_at"]}, {"id": xid, "ended_at": now.isoformat(timespec="seconds")})
        self._mirror_audit(seq)
        self.publish()

    def expire_child_exceptions(self):
        """Marks exceptions that ended more than the grace ago as expired (they are already ignored everywhere: the host job drops them by its own clock)."""
        seqs = []
        with self._tx():
            now = self._now()
            for x in self.child_exceptions(active_only=False, now=now):
                if x["status"] == "active" and dt.datetime.fromisoformat(x["expires_at"]) + dt.timedelta(seconds=EXCEPTION_GRACE_S) < now:
                    seq = self._audit({"token": "-", "name": "system", "role": "admin"}, "child_exception_expire", x["entity"], {"id": x["id"], "target": x["target"]}, {"id": x["id"]})
                    self.db.execute("UPDATE child_exceptions SET status='expired', ended_at=expires_at WHERE id=?", (x["id"],))
                    seqs.append(seq)
        for q in seqs:
            self._mirror_audit(q)
        if seqs:
            self.publish()
        return len(seqs)

    # ── writes: per-child domain lists ────────────────────────────────────────────────────────────────────────────────
    def change_child_domain(self, actor, eid, kind, domain, add, action=None):
        """add/remove one plain domain in this child's allow or deny list. Deny beats allow and a domain cannot be in both (adding to one removes it from the
        other). A deny that would block an always-on host is rejected (invariant 3). Audited with the full lists before and after (the undo restores them)."""
        s = _s()
        if kind not in ("allow", "deny"):
            raise model.ValidationError([model.problem("kind", "kind", "kind must be allow or deny")])
        domain = (domain or "").strip().lower().rstrip(".") if isinstance(domain, str) else ""
        if not child_invariants.DOMAIN_RE.match(domain):
            raise model.ValidationError([model.problem("domain", "domain", "not a plain domain name: %r" % domain)])
        if kind == "deny" and add:
            h = child_invariants.deny_breaks_baseline(domain)
            if h:
                raise model.ValidationError([model.problem("invariant", "domain", "blocking %s would block the always-on host %s: the always-on list cannot be removed" % (domain, h))])
        with self._tx():
            ent = self._child_entity(eid)
            self._check_editor(actor, ent)
            before = self.child_domains(eid)
            now = self._now().isoformat(timespec="seconds")
            other = "deny" if kind == "allow" else "allow"
            if add:
                if domain in before[kind]:
                    return before
                self.db.execute("DELETE FROM child_domains WHERE entity_id=? AND kind=? AND domain=?", (eid, other, domain))
                self.db.execute("INSERT INTO child_domains(entity_id,kind,domain,added_by,added_at) VALUES (?,?,?,?,?)", (eid, kind, domain, s.canon(actor), now))
            else:
                if domain not in before[kind]:
                    raise s.NotFound(domain)
                self.db.execute("DELETE FROM child_domains WHERE entity_id=? AND kind=? AND domain=?", (eid, kind, domain))
            after = self.child_domains(eid)
            seq = self._audit(actor, action or ("child_domain_add" if add else "child_domain_remove"), eid, {"lists": before, "change": {"kind": kind, "domain": domain, "add": add}}, {"lists": after})
        self._mirror_audit(seq)
        self.publish()
        return after

    def undo_child_domain(self, actor, eid):
        """Restores the lists as they were before the last domain change of this child, as a NEW change."""
        s = _s()
        with self.lock:
            ent = self._child_entity(eid)
            self._check_editor(actor, ent)
            rows = [r for r in self.audit(entity=eid, limit=200) if r["action"] in ("child_domain_add", "child_domain_remove", "child_domain_undo")]
            if not rows:
                raise s.NotFound("nothing to undo for '%s'" % eid)
            last = rows[0]
            want = last["before"]["lists"]
        with self._tx():
            self.db.execute("DELETE FROM child_domains WHERE entity_id=?", (eid,))
            now = self._now().isoformat(timespec="seconds")
            for kind in ("allow", "deny"):
                for d in want[kind]:
                    self.db.execute("INSERT INTO child_domains(entity_id,kind,domain,added_by,added_at) VALUES (?,?,?,?,?)", (eid, kind, d, s.canon(actor), now))
            seq = self._audit(actor, "child_domain_undo", eid, {"lists": last["after"]["lists"]}, {"lists": want})
        self._mirror_audit(seq)
        self.publish()
        return self.child_domains(eid)

    # ── import / migration (the running policy -> the model). Before the cutover only; afterwards the model is the source and a re-import would overwrite it ──
    def child_cutover_done(self):
        with self.lock:
            r = self.db.execute("SELECT value FROM meta WHERE key='child_cutover'").fetchone()
        return json.loads(r["value"]) if r else None

    def set_child_cutover(self, actor, done, note=""):
        s = _s()
        with self._tx():
            if actor["role"] != "admin":
                raise s.Forbidden("the cutover switch is admin-only")
            if done:
                self.db.execute("INSERT INTO meta(key,value) VALUES('child_cutover',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (s.canon({"at": self._now().isoformat(timespec="seconds"), "note": note}),))
            else:
                self.db.execute("DELETE FROM meta WHERE key='child_cutover'")
            seq = self._audit(actor, "child_cutover" if done else "child_cutover_rollback", None, None, {"note": note})
        self._mirror_audit(seq)

    def import_child_policy(self, actor, payload, dry_run=False):
        """Replaces the modelled child policy with `payload` (admin only): devices (+ their windows), house-wide lists, always-on services and the still-active
        exceptions of the old runtime file. Nothing is written to AdGuard. Refused after the cutover (it would overwrite the parents' edits); dry_run=True
        validates and reports what would be stored without storing anything."""
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("importing the child policy is admin-only")
        if self.child_cutover_done() and not dry_run:
            raise s.Forbidden("the child policy has been cut over: the model is the source now and a re-import would overwrite the parents' edits (use dry_run)")
        probs = []
        if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list) or not payload["devices"]:
            raise model.ValidationError([model.problem("type", "devices", "devices must be a non-empty list")])
        lists, lp = child_render.validate_lists(payload.get("lists", {}))
        probs += lp
        svc = payload.get("always_services", [])
        if not isinstance(svc, list) or any(x not in ("telegram",) for x in svc):
            probs.append(model.problem("value", "always_services", "always_services may only name 'telegram'"))
        elif "telegram" not in svc:
            probs.append(model.problem("invariant", "always_services", "Telegram must stay in the always-on services (invariant)"))
        missing = sorted(child_invariants.REQUIRED_ALWAYS - set(lists.get("allow_always", [])))
        if missing:
            probs.append(model.problem("invariant", "lists.allow_always", "the always-on list lost required hosts: %s" % ", ".join(missing[:5])))
        devices, seen, by_ip = [], set(), {}
        prior = {e["id"]: e for e in self.child_devices()}
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
            old = prior.get(eid)
            guard = (old["guardrails"] if old and old["guardrails"] else dict(PROPOSED_GUARDRAILS))
            flags = (old["flags"] if old and old["flags"] else dict(CHILD_FLAGS))
            rules, rp = model.validate_rules({"id": eid, "kind": "child_window", "guardrails": guard}, d.get("rules", []))
            probs += [{**p, "message": "%s: %s" % (eid, p["message"])} for p in rp]
            dom = {}
            for kind in ("allow_extra", "deny_extra"):
                vals = sorted(set((d.get(kind) or []) + lists.get(kind, [])))
                if any(not child_invariants.DOMAIN_RE.match(x) for x in vals):
                    probs.append(model.problem("domains", "devices.%s" % kind, "%s: %s must be plain domain names" % (eid, kind)))
                dom[kind] = vals
            for x in dom["deny_extra"]:
                if child_invariants.deny_breaks_baseline(x):
                    probs.append(model.problem("invariant", "devices.deny_extra", "%s: blocking %s would block an always-on host" % (eid, x)))
            devices.append({"id": eid, "name": d["name"], "ip": d["ip"], "mac": (d.get("mac") or "").lower(), "rules": rules, "guard": guard, "flags": flags, "dom": dom})
            by_ip[d["ip"]] = eid
        now = self._now()
        exs = []
        for x in payload.get("exceptions", []) or []:
            try:
                exp = dt.datetime.fromisoformat(x["expires"])
                exp = exp if exp.tzinfo else exp.replace(tzinfo=model.TZ)
            except (KeyError, ValueError, TypeError):
                probs.append(model.problem("exception", "exceptions", "an exception needs client, target and an ISO expires"))
                continue
            if exp <= now:
                continue                                              # expired entries are dropped (three from 2026-10-17 are already dead)
            if x.get("client") not in by_ip or x.get("target") not in model.EXCEPTION_TARGETS:
                probs.append(model.problem("exception", "exceptions", "unknown client or target: %r" % (x,)))
                continue
            exs.append({"entity": by_ip[x["client"]], "target": x["target"], "created": x.get("created") or now.isoformat(timespec="seconds"), "expires": exp.isoformat(timespec="seconds")})
        if probs:
            raise model.ValidationError(probs)
        summary = {"devices": [d["id"] for d in devices], "lists": {k: len(v) for k, v in lists.items()},
                   "per_child_lists": {d["id"]: {k: len(v) for k, v in d["dom"].items()} for d in devices}, "always_services": sorted(svc),
                   "exceptions_migrated": len(exs), "dry_run": bool(dry_run),
                   "sha256": hashlib.sha256(s.canon({"d": [{k: v for k, v in x.items() if k not in ("flags",)} for x in devices], "l": lists, "s": svc, "e": exs}).encode()).hexdigest()[:16]}
        if dry_run:
            return summary
        with self._tx():
            before = {"devices": [e["id"] for e in self.child_devices()]}
            ids = "SELECT id FROM entities WHERE kind='child_window'"
            for t in ("exceptions", "child_exceptions", "child_domains"):
                self.db.execute("DELETE FROM %s WHERE entity_id IN (%s)" % (t, ids))
            self.db.execute("DELETE FROM schedules WHERE entity_id IN (%s)" % ids)
            self.db.execute("DELETE FROM entities WHERE kind='child_window'")
            pos = (self.db.execute("SELECT COALESCE(MAX(position), 0) m FROM entities").fetchone()["m"]) + 1
            for i, d in enumerate(devices):
                if self.db.execute("SELECT 1 FROM entities WHERE id=?", (d["id"],)).fetchone():
                    raise model.ValidationError([model.problem("duplicate", "devices", "id %s clashes with a non-child entity" % d["id"])])
                self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                                (d["id"], "child_window", d["name"], None, 0, s.canon(d["flags"]), s.canon(d["guard"]), pos + i, s.canon({"ip": d["ip"], "mac": d["mac"]})))
                self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)", (d["id"], 1, model.TZ_NAME, s.canon(d["rules"]), self.clock()))
                for kind, key in (("allow", "allow_extra"), ("deny", "deny_extra")):
                    for dom in d["dom"][key]:
                        self.db.execute("INSERT INTO child_domains(entity_id,kind,domain,added_by,added_at) VALUES (?,?,?,?,?)", (d["id"], kind, dom, s.canon(actor), self.clock()))
            for x in exs:
                self.db.execute("INSERT INTO child_exceptions(id,entity_id,target,created_at,expires_at,created_by,status,ended_at) VALUES (?,?,?,?,?,?,?,?)",
                                ("c" + uuid.uuid4().hex[:10], x["entity"], x["target"], x["created"], x["expires"], s.canon({"token": "-", "name": "migrated-overrides", "role": "admin"}), "active", None))
            house = {k: v for k, v in lists.items() if k in ("allow_always", "allow_weekdays")}
            house.update({"allow_extra": [], "deny_extra": []})        # the extra lists are per child now (copied into each device above)
            self.db.execute("INSERT INTO meta(key,value) VALUES('child_lists',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (s.canon({"lists": house, "always_services": sorted(svc)}),))
            seq = self._audit(actor, "import_child_policy", None, before, summary)
        self._mirror_audit(seq)
        self.publish()
        return summary

    # ── admin: the set of child DEVICES (after the cutover a re-import is refused, so this is how a device is added or retired) ──────────────────────────────
    def add_child_device(self, actor, name, ip, mac="", like=None):
        """Admin only. A new device starts with the windows of `like` (another child device) or the standard ones (16:00-19:00 Mon-Sat YouTube/Roblox, Maps all
        day), the proposed guardrails, no exceptions and empty own lists. Refused: house infrastructure as a device, a duplicate name/ip/mac."""
        import re
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("adding a child device is admin-only")
        name = name.strip() if isinstance(name, str) else ""
        mac = (mac or "").strip().lower() if isinstance(mac, str) else ""
        probs = []
        if not 1 <= len(name) <= 60:
            probs.append(model.problem("name", "name", "the name must be 1-60 characters"))
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        eid = "child_" + slug
        eid = eid[:40].rstrip("_")
        dev = {"id": eid, "name": name, "ip": ip if isinstance(ip, str) else "", "mac": mac}
        probs += child_render.validate_device(dev) if name else []
        if not slug or not model.ENTITY_ID_RE.match(eid):
            probs.append(model.problem("name", "name", "the name must contain letters or digits"))
        with self._tx():
            existing = self.child_devices()
            for e in existing:
                if e["id"] == eid or e["name"].lower() == name.lower():
                    probs.append(model.problem("duplicate", "name", "a child device named %r already exists" % e["name"]))
                if e["meta"].get("ip") == dev["ip"]:
                    probs.append(model.problem("duplicate", "ip", "%s already belongs to %s" % (dev["ip"], e["name"])))
                if mac and (e["meta"].get("mac") or "") == mac:
                    probs.append(model.problem("duplicate", "mac", "%s already belongs to %s" % (mac, e["name"])))
            if self.db.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone() and not any(p["code"] == "duplicate" for p in probs):
                probs.append(model.problem("duplicate", "name", "the id %s clashes with another entity" % eid))
            src = None
            if like:
                src = next((e for e in existing if e["id"] == like), None)
                if not src:
                    probs.append(model.problem("like", "like", "no child device %r to copy the windows from" % like))
            guard = dict(src["guardrails"]) if src and src["guardrails"] else dict(PROPOSED_GUARDRAILS)
            flags = dict(src["flags"]) if src and src["flags"] else dict(CHILD_FLAGS)
            rules = self.schedule(like)["rules"] if src else [
                {"id": "maps", "days": [1, 2, 3, 4, 5, 6], "start": "00:00", "end": "24:00", "variables": {"opens": "maps"}},
                {"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}]
            rules, rp = model.validate_rules({"id": eid, "kind": "child_window", "guardrails": guard}, rules)
            probs += rp
            if probs:
                raise model.ValidationError(probs)
            pos = (self.db.execute("SELECT COALESCE(MAX(position), 0) m FROM entities").fetchone()["m"]) + 1
            self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                            (eid, "child_window", name, None, 0, s.canon(flags), s.canon(guard), pos, s.canon({"ip": dev["ip"], "mac": mac})))
            self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)", (eid, 1, model.TZ_NAME, s.canon(rules), self.clock()))
            seq = self._audit(actor, "child_device_add", eid, None, {"name": name, "ip": dev["ip"], "mac": mac, "like": like, "rules": rules})
        self._mirror_audit(seq)
        self.publish()
        return self.child_detail(eid)

    def remove_child_device(self, actor, eid):
        """Admin only. The device stops being filtered by this policy (its catch-all leaves the snapshot and so AdGuard at the next job run). The audit row keeps everything
        needed to re-create it by hand: windows, guardrails, lists and exceptions."""
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("removing a child device is admin-only")
        with self._tx():
            ent = self._child_entity(eid)
            if len(self.child_devices()) <= 1:
                raise model.ValidationError([model.problem("last_device", "id", "the last child device cannot be removed: a snapshot without a device would lift every restriction")])
            before = {"name": ent["name"], "meta": ent["meta"], "guardrails": ent["guardrails"], "flags": ent["flags"], "rules": self.schedule(eid)["rules"],
                      "domains": self.child_domains(eid), "exceptions": [{k: x[k] for k in ("target", "expires_at")} for x in self.child_exceptions(eid)]}
            for t in ("exceptions", "child_exceptions", "child_domains"):
                self.db.execute("DELETE FROM %s WHERE entity_id=?" % t, (eid,))
            self.db.execute("DELETE FROM schedules WHERE entity_id=?", (eid,))
            self.db.execute("DELETE FROM entities WHERE id=?", (eid,))
            seq = self._audit(actor, "child_device_remove", eid, before, None)
        self._mirror_audit(seq)
        self.publish()
