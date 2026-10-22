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
        return {"id": eid, "name": ent["name"], "ip": ent["meta"].get("ip"), "mac": ent["meta"].get("mac"), "label": ent["meta"].get("label") or "", "icon": ent["meta"].get("icon") or "",
                "child": ent["meta"].get("child") or "", "version": sch["version"], "tz": sch["tz"],
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

    # ── Task 31 (Ivan, 2026-10-22): Maps moved to the always-open set - one audited data migration, run when the service starts ────────────────────────────
    MAPS_DOMAINS = ("maps.google.com", "maps.googleapis.com", "maps.gstatic.com")
    MAPS_ACTION = "child_maps_always_open"
    MAPS_ROLLBACK_ACTION = "child_maps_always_open_rollback"
    SYSTEM_ACTOR = {"token": "-", "name": "migration", "role": "admin"}

    def _maps_state(self):
        stored = self.child_house_lists()
        rules = {}
        for e in self.child_devices():
            rs = self.schedule(e["id"])["rules"]
            if any("maps" in r["variables"]["opens"].split(",") for r in rs):
                rules[e["id"]] = rs
        exs = [x for x in self.child_exceptions(active_only=False) if x["target"] == "maps" and x["status"] == "active"]
        return stored, rules, exs

    def maps_migration_pending(self):
        """True while the data still carries the old Maps arrangement (a `maps` rule or exception, domains in allow_weekdays) or lacks the Maps domains in allow_always."""
        with self.lock:
            if not self.db.execute("SELECT 1 FROM meta WHERE key='child_lists'").fetchone():
                return False                                                            # no child policy imported yet: nothing to migrate
            stored, rules, exs = self._maps_state()
        lists = stored["lists"]
        return bool(rules or exs or lists.get("allow_weekdays") or not set(self.MAPS_DOMAINS) <= set(lists.get("allow_always") or []))

    def migrate_maps_always_open(self, actor=None):
        """The one-time, idempotent data migration. Removes the `maps` target from every window (a rule that opened only Maps is deleted), cancels active `maps`
        exceptions (they are no-ops now), moves the `allow_weekdays` domains and the three Maps domains into `allow_always`, leaves `allow_weekdays` EMPTY. One
        audit row holds the complete before-state (the rollback reads it). Returns the summary, or None when there was nothing to do."""
        s = _s()
        actor = actor or self.SYSTEM_ACTOR
        with self._tx():
            if not self.db.execute("SELECT 1 FROM meta WHERE key='child_lists'").fetchone():
                return None
            stored, with_maps, exs = self._maps_state()
            lists = dict(stored["lists"])
            always, weekdays = set(lists.get("allow_always") or []), set(lists.get("allow_weekdays") or [])
            want = always | weekdays | set(self.MAPS_DOMAINS)
            if not with_maps and not exs and not weekdays and want == always:
                return None
            now = self._now().isoformat(timespec="seconds")
            before = {"allow_always": sorted(always), "allow_weekdays": sorted(weekdays), "rules": {eid: rs for eid, rs in with_maps.items()},
                      "versions": {eid: self.schedule(eid)["version"] for eid in with_maps},
                      "exceptions": [{"id": x["id"], "entity": x["entity"], "target": x["target"], "created_at": x["created_at"], "expires_at": x["expires_at"]} for x in exs]}
            new_rules_by = {}
            for eid, rs in with_maps.items():
                new = []
                for r in rs:
                    opens = r["variables"]["opens"].split(",")
                    kept = [t for t in opens if t != "maps"]
                    if kept == opens:
                        new.append(r)
                    elif kept:
                        new.append({**r, "variables": {**r["variables"], "opens": ",".join(kept)}})
                ent = self.entity(eid)
                good, probs = model.validate_rules(ent, new)
                if probs:
                    raise model.ValidationError(probs)
                ver = self.schedule(eid)["version"] + 1
                self.db.execute("UPDATE schedules SET version=?, rules=?, updated_at=? WHERE entity_id=?", (ver, s.canon(good), self.clock(), eid))
                new_rules_by[eid] = good
            for x in exs:
                self.db.execute("UPDATE child_exceptions SET status='cancelled', ended_at=? WHERE id=?", (now, x["id"]))
            lists["allow_always"], lists["allow_weekdays"] = sorted(want), []
            self.db.execute("UPDATE meta SET value=? WHERE key='child_lists'", (s.canon({"lists": lists, "always_services": stored["always_services"]}),))
            after = {"allow_always": sorted(want), "allow_weekdays": [], "rules": new_rules_by, "exceptions_cancelled": [x["id"] for x in exs],
                     "maps_domains": list(self.MAPS_DOMAINS)}
            seq = self._audit(actor, self.MAPS_ACTION, None, before, after)
        self._mirror_audit(seq)
        self.publish()
        return {"rules_changed": sorted(with_maps), "exceptions_cancelled": len(exs), "moved_from_weekdays": sorted(weekdays), "allow_always_added": sorted(want - always), "audit_seq": seq}

    def rollback_maps_always_open(self, actor=None):
        """Restores the data to the state before the LAST migration (rules and their versions' content, the lists, still-active `maps` exceptions) as a new audited change.
        It does NOT publish: this code refuses a `maps` rule, so the data is meant to be read by the PREVIOUS code (deploy the old image, then start it - it publishes).
        Used by the live-apply script's auto-revert and by the rehearsal. Returns None when no migration is on record."""
        s = _s()
        actor = actor or self.SYSTEM_ACTOR
        with self._tx():
            row = next((r for r in self.audit(limit=500) if r["action"] == self.MAPS_ACTION), None)
            if not row:
                return None
            b = row["before"]
            now = self._now()
            for eid, rs in b["rules"].items():
                ver = self.schedule(eid)["version"] + 1
                self.db.execute("UPDATE schedules SET version=?, rules=?, updated_at=? WHERE entity_id=?", (ver, s.canon(rs), self.clock(), eid))
            stored = self.child_house_lists()
            lists = dict(stored["lists"])
            lists["allow_always"], lists["allow_weekdays"] = sorted(b["allow_always"]), sorted(b["allow_weekdays"])
            self.db.execute("UPDATE meta SET value=? WHERE key='child_lists'", (s.canon({"lists": lists, "always_services": stored["always_services"]}),))
            revived = 0
            for x in b["exceptions"]:
                if dt.datetime.fromisoformat(x["expires_at"]) > now:
                    self.db.execute("UPDATE child_exceptions SET status='active', ended_at=NULL WHERE id=?", (x["id"],))
                    revived += 1
            seq = self._audit(actor, self.MAPS_ROLLBACK_ACTION, None, {"migration_seq": row["seq"]}, {"rules_restored": sorted(b["rules"]), "exceptions_revived": revived,
                                                                                                    "allow_weekdays": sorted(b["allow_weekdays"])})
        self._mirror_audit(seq)
        return {"migration_seq": row["seq"], "rules_restored": sorted(b["rules"]), "exceptions_revived": revived, "audit_seq": seq}

    # ── admin: the set of child DEVICES (after the cutover a re-import is refused, so this is how a device is added or retired) ──────────────────────────────
    REMOVAL_UNDO_HOURS = 24
    ICONS = ("phone", "tablet", "laptop", "console", "tv", "other")
    TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "h", "ts",
                                                           "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))

    @classmethod
    def _slug(cls, text):
        import re
        t = "".join(cls.TRANSLIT.get(c, c) for c in (text or "").lower())
        return re.sub(r"[^a-z0-9]+", "_", t).strip("_")

    def add_child_device(self, actor, name, ip, mac="", like=None, label="", icon="other", closed=False):
        """Admin only. `name` is the CHILD, `label` the device ("телефон"); the entity is called "<name> <label>". A new device starts with the windows and the per-child
        lists of `like` (another device), else with the standard window (16:00-19:00 Mon-Sat YouTube/Roblox; Maps is always open, it has no window), else - `closed=True` - with NO windows at all
        (catch-all + the always-on list only: nothing opens until a parent sets a window). Proposed guardrails, no exceptions. Refused: house infrastructure as a device,
        a duplicate name/ip/mac. The AdGuard side (client entry, rules) is the host job's, from the next snapshot."""
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("adding a child device is admin-only")
        name = name.strip() if isinstance(name, str) else ""
        label = label.strip() if isinstance(label, str) else ""
        full = ("%s %s" % (name, label)).strip()
        mac = (mac or "").strip().lower() if isinstance(mac, str) else ""
        probs = []
        if not 1 <= len(name) <= 40 or len(label) > 40 or not any(ch.isalnum() for ch in name):
            probs.append(model.problem("name", "name", "the child's name must be 1-40 characters with a letter or digit, the device label at most 40"))
        if icon not in self.ICONS:
            probs.append(model.problem("icon", "icon", "icon must be one of %s" % ", ".join(self.ICONS)))
        slug = self._slug(full)
        if not slug:
            slug = "dev_" + (ip or "").replace(".", "_")
        eid = ("child_" + slug)[:40].rstrip("_")
        dev = {"id": eid, "name": full, "ip": ip if isinstance(ip, str) else "", "mac": mac}
        probs += child_render.validate_device(dev) if name else []
        if not model.ENTITY_ID_RE.match(eid):
            probs.append(model.problem("name", "name", "the name must contain letters or digits"))
        with self._tx():
            existing = self.child_devices()
            for e in existing:
                if e["id"] == eid or e["name"].lower() == full.lower():
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
            if like and closed:
                probs.append(model.problem("template", "like", "give either a template to copy or closed, not both"))
            guard = dict(src["guardrails"]) if src and src["guardrails"] else dict(PROPOSED_GUARDRAILS)
            flags = dict(src["flags"]) if src and src["flags"] else dict(CHILD_FLAGS)
            if src:
                rules = self.schedule(like)["rules"]
            elif closed:
                rules = []
            else:
                rules = [{"id": "yt_roblox", "days": [1, 2, 3, 4, 5, 6], "start": "16:00", "end": "19:00", "variables": {"opens": "youtube,roblox"}}]
            rules, rp = model.validate_rules({"id": eid, "kind": "child_window", "guardrails": guard}, rules)
            probs += rp
            if probs:
                raise model.ValidationError(probs)
            pos = (self.db.execute("SELECT COALESCE(MAX(position), 0) m FROM entities").fetchone()["m"]) + 1
            self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                            (eid, "child_window", full, None, 0, s.canon(flags), s.canon(guard), pos, s.canon({"ip": dev["ip"], "mac": mac, "label": label, "icon": icon, "child": name})))
            self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)", (eid, 1, model.TZ_NAME, s.canon(rules), self.clock()))
            if src:
                for kind, doms in self.child_domains(like).items():
                    for d in doms:
                        self.db.execute("INSERT INTO child_domains(entity_id,kind,domain,added_by,added_at) VALUES (?,?,?,?,?)", (eid, kind, d, s.canon(actor), self.clock()))
            seq = self._audit(actor, "child_device_add", eid, None, {"name": full, "ip": dev["ip"], "mac": mac, "label": label, "icon": icon, "like": like, "closed": bool(closed),
                                                                   "rules": rules})
        self._mirror_audit(seq)
        self.publish()
        return self.child_detail(eid)

    def _definition(self, eid, ent):
        return {"id": eid, "name": ent["name"], "meta": ent["meta"], "guardrails": ent["guardrails"], "flags": ent["flags"], "rules": self.schedule(eid)["rules"],
                "domains": self.child_domains(eid), "exceptions": [{k: x[k] for k in ("target", "created_at", "expires_at")} for x in self.child_exceptions(eid)]}

    def remove_child_device(self, actor, eid, keep=True):
        """Admin only. The device stops being filtered by this policy (its catch-all leaves the snapshot and so AdGuard at the next job run). The definition (windows,
        guardrails, lists, active exceptions) is kept for 24 h in `child_removed` so the removal can be UNDONE; the audit row keeps it as well. Returns the removal
        record (keep=False, used to roll back a FAILED add, forgets the record: nothing to undo). The last device cannot be removed."""
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("removing a child device is admin-only")
        with self._tx():
            ent = self._child_entity(eid)
            if len(self.child_devices()) <= 1:
                raise model.ValidationError([model.problem("last_device", "id", "the last child device cannot be removed: a snapshot without a device would lift every restriction")])
            before = self._definition(eid, ent)
            rid = "r" + uuid.uuid4().hex[:10]
            now = self._now()
            if keep:
                self.db.execute("INSERT INTO child_removed(id,entity_id,name,removed_at,removed_by,definition) VALUES (?,?,?,?,?,?)",
                               (rid, eid, ent["name"], now.isoformat(timespec="seconds"), s.canon(actor), s.canon(before)))
            for t in ("exceptions", "child_exceptions", "child_domains"):
                self.db.execute("DELETE FROM %s WHERE entity_id=?" % t, (eid,))
            self.db.execute("DELETE FROM schedules WHERE entity_id=?", (eid,))
            self.db.execute("DELETE FROM entities WHERE id=?", (eid,))
            seq = self._audit(actor, "child_device_remove", eid, before, {"removal": rid})
        self._mirror_audit(seq)
        self.publish()
        return {"removal_id": rid, "child_id": eid, "name": ent["name"], "removed_at": now.isoformat(timespec="seconds"),
                "restorable_until": (now + dt.timedelta(hours=self.REMOVAL_UNDO_HOURS)).isoformat(timespec="seconds")}

    def child_removed(self, now=None):
        """Removals that can still be undone (younger than 24 h); older records are dropped here."""
        now = now or self._now()
        cutoff = (now - dt.timedelta(hours=self.REMOVAL_UNDO_HOURS)).isoformat(timespec="seconds")
        with self.lock:
            self.db.execute("DELETE FROM child_removed WHERE removed_at < ?", (cutoff,))
            rows = self.db.execute("SELECT * FROM child_removed ORDER BY removed_at DESC").fetchall()
        out = []
        for r in rows:
            at = dt.datetime.fromisoformat(r["removed_at"])
            d = json.loads(r["definition"])
            out.append({"removal_id": r["id"], "child_id": r["entity_id"], "name": r["name"], "ip": d["meta"].get("ip"), "removed_at": r["removed_at"],
                        "restorable_until": (at + dt.timedelta(hours=self.REMOVAL_UNDO_HOURS)).isoformat(timespec="seconds"), "removed_by": (json.loads(r["removed_by"]).get("acting") or {}).get("person")
                        or json.loads(r["removed_by"]).get("name")})
        return out

    def restore_child_device(self, actor, rid):
        """Admin only. Re-creates a removed device from its stored definition (same id, windows, guardrails, flags, lists, still-active exceptions) - within 24 h of the
        removal, and only if its id, name, ip and mac are still free."""
        s = _s()
        if actor["role"] != "admin":
            raise s.Forbidden("restoring a child device is admin-only")
        now = self._now()
        if not any(x["removal_id"] == rid for x in self.child_removed(now)):
            raise s.NotFound(rid)
        with self._tx():
            row = self.db.execute("SELECT * FROM child_removed WHERE id=?", (rid,)).fetchone()
            d = json.loads(row["definition"])
            eid, meta = d["id"], d["meta"]
            probs = []
            for e in self.child_devices():
                if e["id"] == eid or e["name"].lower() == d["name"].lower():
                    probs.append(model.problem("duplicate", "name", "a device named %r exists now" % e["name"]))
                if e["meta"].get("ip") == meta.get("ip"):
                    probs.append(model.problem("duplicate", "ip", "%s belongs to %s now" % (meta.get("ip"), e["name"])))
                if meta.get("mac") and (e["meta"].get("mac") or "") == meta["mac"]:
                    probs.append(model.problem("duplicate", "mac", "%s belongs to %s now" % (meta["mac"], e["name"])))
            if self.db.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone():
                probs.append(model.problem("duplicate", "id", "the id %s is taken by another entity" % eid))
            ip_prob = child_invariants.device_ip_problem(meta.get("ip"))
            if ip_prob:
                probs.append(model.problem("protected_ip", "ip", ip_prob))
            rules, rp = model.validate_rules({"id": eid, "kind": "child_window", "guardrails": d["guardrails"]}, d["rules"])
            probs += rp
            if probs:
                raise model.ValidationError(probs)
            pos = (self.db.execute("SELECT COALESCE(MAX(position), 0) m FROM entities").fetchone()["m"]) + 1
            self.db.execute("INSERT INTO entities(id,kind,name,ha_target,managed,flags,guardrails,position,meta) VALUES (?,?,?,?,?,?,?,?,?)",
                            (eid, "child_window", d["name"], None, 0, s.canon(d["flags"]), s.canon(d["guardrails"]), pos, s.canon(meta)))
            self.db.execute("INSERT INTO schedules(entity_id,version,tz,rules,updated_at) VALUES (?,?,?,?,?)", (eid, 1, model.TZ_NAME, s.canon(rules), self.clock()))
            for kind, doms in (d.get("domains") or {}).items():
                for dom in doms:
                    self.db.execute("INSERT INTO child_domains(entity_id,kind,domain,added_by,added_at) VALUES (?,?,?,?,?)", (eid, kind, dom, s.canon(actor), self.clock()))
            kept = 0
            for x in d.get("exceptions") or []:
                if dt.datetime.fromisoformat(x["expires_at"]) > now:
                    self.db.execute("INSERT INTO child_exceptions(id,entity_id,target,created_at,expires_at,created_by,status,ended_at) VALUES (?,?,?,?,?,?,?,?)",
                                    ("c" + uuid.uuid4().hex[:10], eid, x["target"], x["created_at"], x["expires_at"], s.canon({"token": "-", "name": "restored", "role": "admin"}), "active", None))
                    kept += 1
            self.db.execute("DELETE FROM child_removed WHERE id=?", (rid,))
            seq = self._audit(actor, "child_device_restore", eid, {"removal": rid}, {"name": d["name"], "ip": meta.get("ip"), "exceptions_kept": kept})
        self._mirror_audit(seq)
        self.publish()
        return self.child_detail(eid)
