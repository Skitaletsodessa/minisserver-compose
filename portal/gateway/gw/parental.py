"""The parental-control module of the gateway (Task 29 Part 2, Phase G). It owns NO policy data: windows, exceptions and the per-child domain lists are data of the
schedule service (guardrails, audit, undo, backup); the host job enforces them from the service's snapshot. The gateway adds: authorisation by role, the
identity of each device, the live view from AdGuard (activity on demand - NOTHING is stored here -, whether the rules block in AdGuard really is the one the model
says it should be) and the admin-only filter lists (filterlists.py).

Roles (ADR 0008, proposed): a PARENT is required for everything. admin: all, incl. filter lists. household (Dasha): status, activity, windows within the
guardrails (when the device is household-editable), exceptions, per-domain allow/deny. readonly: status only. Children: nothing (they have no account here).
"""
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.parse
from zoneinfo import ZoneInfo

from . import agh as aghmod
from .auth import GwError
from .strings import S

TZ = ZoneInfo("Europe/Prague")
BEGIN = "! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)"
END = "! END child-policy"
PAGE = 500
MAX_PAGES = 20                    # 10 000 log entries at most per view
ACTIVITY_PAGE = 100
CHILD_KEYS = ("id", "target", "created_at", "expires_at", "created_by", "remaining_s")
THREAT_HINTS = ("urlhaus", "phishing", "malware", "threat", "scam")
LIST_STALE_S = 72 * 3600
POLICY_TTL_S = 15
TODAY_TTL_S = 30
ACTIVITY_TTL_S = 30


def rules_hash(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def to_service_rules(rules):
    return [{"id": r.get("id"), "days": r.get("days"), "start": r.get("start"), "end": r.get("end"), "variables": {"opens": ",".join(r.get("opens") or [])}} for r in rules or []]


def from_service_rules(rules):
    return [{"id": r["id"], "days": r["days"], "start": r["start"], "end": r["end"], "opens": [x for x in (r.get("variables") or {}).get("opens", "").split(",") if x]} for r in rules or []]


def device_kind(name):
    n = (name or "").lower()
    if "tablet" in n or "pad" in n:
        return "tablet"
    if any(k in n for k in ("phone", "motorola", "pixel", "iphone", "redmi", "galaxy", "xiaomi", "edge")):
        return "phone"
    return "other"


def _vendor_of(mac, oui_file):
    mac = (mac or "").lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        return None, False
    if int(mac[:2], 16) & 0x02:
        return None, True
    key = mac.replace(":", "")[:6].upper()
    try:
        with open(oui_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^([0-9A-F]{2})-([0-9A-F]{2})-([0-9A-F]{2})\s+\(hex\)\s+(.+)$", line)
                if m and "".join(m.groups()[:3]) == key:
                    return m.group(4).strip(), False
    except OSError:
        pass
    return None, False


class Parental:
    def __init__(self, sched, agh, filters, db, clock=time.time, *, parity_state=None, oui_file="/ro/ieee-data/oui.txt", sleep=None):
        self.sched, self.agh, self.filters, self.db, self.clock = sched, agh, filters, db, clock
        self.parity_state = parity_state
        self.oui_file = oui_file
        self._cache = {}                                    # key -> (ts, value): short-lived, in memory, never persisted

    # ── roles ─────────────────────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _need(p, *, view_only=False, admin=False):
        if not p.get("parent"):
            raise GwError(403, "forbidden", "parental control is for parents only", {"role": p["role"]})
        if admin and p["role"] != "admin":
            raise GwError(403, "forbidden", "only the administrator may do this", {"role": p["role"]})
        if admin and p.get("app"):                                       # Task 30: a stolen unlocked phone must not open the filters or add devices (ADR 0009)
            raise GwError(403, "web_login_required", "this action needs a web sign-in with password and code")
        if p["role"] not in (("admin", "household", "readonly") if view_only else ("admin", "household")):
            raise GwError(403, "forbidden", "your role may not do this", {"role": p["role"]})

    @staticmethod
    def _srole(p):
        return "admin" if p["role"] == "admin" else "household"

    @staticmethod
    def _acting(p):
        return {"person": p["person_id"], "phone": p.get("phone_id") or "-"}

    def _cached(self, key, ttl):
        hit = self._cache.get(key)
        return hit[1] if hit and self.clock() - hit[0] < ttl else None

    def _put(self, key, value):
        self._cache[key] = (self.clock(), value)
        return value

    # ── the schedule service ──────────────────────────────────────────────────────────────────────────────────────
    async def _svc(self, p, method, path, body=None, headers=None, expect=(200,), role=None):
        st, js, hd = await self.sched.call(method, path, role or self._srole(p), self._acting(p) if p else None, body, headers)
        if st not in expect:
            raise self.sched.translate_error(st, js)
        return js, hd

    async def _children_raw(self, p):
        js, _ = await self._svc(p, "GET", "/v1/child")
        return js["children"]

    async def _child_raw(self, p, cid):
        js, _ = await self._svc(p, "GET", "/v1/child/" + urllib.parse.quote(cid, safe=""))
        return js

    async def child_ips(self):
        try:
            st, js, _ = await self.sched.call("GET", "/v1/child", "household")
            return [c["ip"] for c in (js or {}).get("children", [])] if st == 200 else []
        except GwError:
            return []

    # ── AdGuard-derived facts (every one degrades to None when AdGuard does not answer) ────────────────────────────
    async def _agh_clients(self):
        hit = self._cached("clients", 60)
        if hit is not None:
            return hit
        try:
            d = await self.agh.call("GET", "/clients")
        except aghmod.AghError:
            return {}
        names = {}
        for c in (d.get("auto_clients") or []):
            if c.get("name") and c.get("ip"):
                names[c["ip"]] = c["name"]
        return self._put("clients", names)

    async def _today(self, ip):
        """(last_query_at, queries_today, blocked_today) from AdGuard's log, A queries since local midnight; None triple when unreachable."""
        hit = self._cached("today:" + ip, TODAY_TTL_S)
        if hit is not None:
            return hit
        midnight = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        last, q, b, older = None, 0, 0, None
        try:
            for _ in range(4):
                path = "/querylog?limit=%d&search=%s" % (PAGE, urllib.parse.quote(ip))
                if older:
                    path += "&older_than=" + urllib.parse.quote(older)
                res = await self.agh.call("GET", path)
                data = res.get("data") or []
                stop = False
                for e in data:
                    if e.get("client") != ip:
                        continue
                    t = dt.datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(TZ)
                    if t < midnight:
                        stop = True
                        continue
                    last = last or t
                    if (e.get("question") or {}).get("type") == "A":
                        q += 1
                        if str(e.get("reason", "")).startswith("Filtered"):
                            b += 1
                older = res.get("oldest")
                if stop or len(data) < PAGE or not older:
                    break
        except (aghmod.AghError, ValueError, KeyError):
            return (None, None, None)
        return self._put("today:" + ip, (last.isoformat(timespec="seconds") if last else None, q, b))

    async def _service_domains(self):
        hit = self._cached("sd", 600)
        if hit is not None:
            return hit
        d = await self.agh.call("GET", "/blocked_services/all")
        out = {t: [] for t in ("telegram", "youtube", "roblox")}
        for s in d.get("blocked_services") or []:
            if s["id"] in out:
                out[s["id"]] = [r[2:-1] for r in s["rules"] if r.startswith("||") and r.endswith("^")]
        return self._put("sd", out)

    async def _policy(self, children):
        """{"in_sync", "expected_hash", "applied_hash", "per_ip": {ip: bool}} - the rules block in AdGuard against the one the model says it should be now. A block
        that equals the one of 75 s ago also counts (the job runs once a minute and the gateway can ask a second before it has)."""
        hit = self._cached("policy", POLICY_TTL_S)
        if hit is not None:
            return hit
        try:
            st = await self.agh.call("GET", "/filtering/status")
            rules = st.get("user_rules") or []
            live = rules[rules.index(BEGIN):rules.index(END) + 1] if BEGIN in rules and END in rules else []
            sd = await self._service_domains()
            now = dt.datetime.now(TZ)
            ov = [{"client": c["ip"], "target": x["target"]} for c in children for x in c.get("exceptions") or []]
            exp = []
            for at in (now, now - dt.timedelta(seconds=75)):
                js, _ = await self._svc(None, "POST", "/v1/child-policy/render", {"service_domains": sd, "at": at.isoformat(), "overrides": ov}, role="admin")
                exp.append(js["lines"])
        except (aghmod.AghError, GwError, ValueError):
            return self._put("policy", {"in_sync": None, "expected_hash": None, "applied_hash": None, "per_ip": {}})
        match = next((e for e in exp if e == live), None)
        per_ip = {}
        for c in children:
            tag = "$client=" + c["ip"]
            lines = [x for x in live if x.endswith(tag)]
            per_ip[c["ip"]] = any(lines == [x for x in e if x.endswith(tag)] for e in exp)
        return self._put("policy", {"in_sync": match is not None, "expected_hash": rules_hash(exp[0]), "applied_hash": rules_hash(live) if live else None, "per_ip": per_ip})

    # ── children ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _identity(self, c, hostnames):
        vendor, randomised = _vendor_of(c.get("mac"), self.oui_file)
        return {"ip": c["ip"], "mac": c.get("mac") or None, "vendor": vendor, "randomised_mac": randomised, "hostname": hostnames.get(c["ip"])}

    @staticmethod
    def _exc(x):
        return {k: x.get(k) for k in CHILD_KEYS}

    async def _status(self, c, policy, hostnames):
        st = dict(c.get("status") or {})
        last, q, b = await self._today(c["ip"])
        return {"state": st.get("state", "closed"), "open_services": st.get("open_services", []), "open_until": st.get("open_until"), "opens_at": st.get("opens_at"),
                "exceptions": [self._exc(x) for x in st.get("exceptions") or c.get("exceptions") or []],
                "last_query_at": last, "queries_today": q, "blocked_today": b,
                "policy_ok": bool(policy["per_ip"].get(c["ip"])) if policy["in_sync"] is not None else False,
                "dns_seen": bool(q) if q is not None else False}

    def _summary(self, c, status, hostnames):
        icon = c.get("icon") or ""                                   # chosen when the device was added; older devices: guessed from the name
        kind = {"phone": "phone", "tablet": "tablet"}.get(icon) or (device_kind(c["name"]) if not icon else "other")
        return {"id": c["id"], "name": c["name"], "icon": icon or kind, "device_kind": kind, "identity": self._identity(c, hostnames), "status": status}

    async def children(self, p):
        self._need(p, view_only=True)
        kids = await self._children_raw(p)
        policy, host = await asyncio.gather(self._policy(kids), self._agh_clients())
        out = []
        for c in kids:
            out.append(self._summary(c, await self._status(c, policy, host), host))
        return {"children": out}

    def _can(self, p, c):
        edit = p["role"] == "admin" or (p["role"] == "household" and bool((c.get("flags") or {}).get("household_editable")))
        act = p["role"] in ("admin", "household")
        return {"edit_windows": edit, "grant_exceptions": act, "edit_domains": act, "edit_guardrails": False, "edit_filter_lists": p["role"] == "admin"}

    def _windows(self, p, c, changed=None):
        g = dict(c.get("guardrails") or {})
        g["proposed"] = bool((c.get("flags") or {}).get("guardrails_proposed"))
        out = {"child": c["id"], "version": c["version"], "tz": c["tz"], "updated_at": c.get("updated_at"), "rules": from_service_rules(c["rules"]),
               "guardrails": g, "can_edit": self._can(p, c)["edit_windows"]}
        if changed is not None:
            out["changed"] = bool(changed)
        return out

    async def child(self, p, cid):
        self._need(p, view_only=True)
        c = await self._child_raw(p, cid)
        policy, host = await asyncio.gather(self._policy([c]), self._agh_clients())
        out = self._summary(c, await self._status(c, policy, host), host)
        out.update({"windows": self._windows(p, c), "exceptions": [self._exc(x) for x in c.get("exceptions") or []], "domains": c.get("domains") or {"allow": [], "deny": []},
                    "can": self._can(p, c) if p["role"] != "readonly" else {k: False for k in ("edit_windows", "grant_exceptions", "edit_domains", "edit_guardrails", "edit_filter_lists")},
                    "note": S["parental_bedtime_note"]})
        return out

    # ── windows ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def windows_get(self, p, cid):
        self._need(p)
        c = await self._child_raw(p, cid)
        return self._windows(p, c), '"%d"' % c["version"]

    def _check_edit(self, p, c):
        if not self._can(p, c)["edit_windows"]:
            raise GwError(403, "forbidden", "your role may not edit the windows of this device", {"child": c["id"]})

    async def windows_put(self, p, cid, body, if_match):
        self._need(p)
        c = await self._child_raw(p, cid)
        self._check_edit(p, c)
        js, hd = await self._svc(p, "PUT", "/v1/schedules/" + urllib.parse.quote(cid, safe=""), {"rules": to_service_rules(body["rules"])}, {"If-Match": if_match})
        c2 = await self._child_raw(p, cid)
        self._cache.pop("policy", None)
        return self._windows(p, c2, js.get("changed")), '"%d"' % c2["version"]

    async def windows_preview(self, p, cid, body):
        self._need(p)
        c = await self._child_raw(p, cid)
        self._check_edit(p, c)
        js, _ = await self._svc(p, "POST", "/v1/schedules/%s/preview" % urllib.parse.quote(cid, safe=""), {"rules": to_service_rules(body["rules"])})
        out = {"child": cid, "current_version": js.get("current_version"), "valid": bool(js.get("valid")), "problems": js.get("problems") or []}
        if "diff" in js:
            out["diff"] = js["diff"]
        if "open_next_7_days" in js:
            out["open_next_7_days"] = [{"from": x["from"], "to": x["to"], "opens": list(x["opens"])} for x in js["open_next_7_days"]]
        return out

    async def windows_undo(self, p, cid):
        self._need(p)
        c = await self._child_raw(p, cid)
        self._check_edit(p, c)
        js, _ = await self._svc(p, "POST", "/v1/schedules/%s/undo" % urllib.parse.quote(cid, safe=""))
        c2 = await self._child_raw(p, cid)
        self._cache.pop("policy", None)
        return self._windows(p, c2, js.get("changed")), '"%d"' % c2["version"]

    # ── exceptions ────────────────────────────────────────────────────────────────────────────────────────────────
    async def exceptions_list(self, p, cid):
        self._need(p)
        c = await self._child_raw(p, cid)
        return {"exceptions": [self._exc(x) for x in c.get("exceptions") or []]}

    async def exception_grant(self, p, cid, body):
        self._need(p)
        await self._child_raw(p, cid)
        js, _ = await self._svc(p, "POST", "/v1/child/%s/exceptions" % urllib.parse.quote(cid, safe=""), body, expect=(201,))
        self._cache.pop("policy", None)
        return self._exc(js)

    async def exception_cancel(self, p, cid, xid):
        self._need(p)
        await self._svc(p, "DELETE", "/v1/child/%s/exceptions/%s" % (urllib.parse.quote(cid, safe=""), urllib.parse.quote(xid, safe="")), expect=(204,))
        self._cache.pop("policy", None)

    # ── per-child domain lists ────────────────────────────────────────────────────────────────────────────────────
    async def domains_get(self, p, cid):
        self._need(p)
        return (await self._child_raw(p, cid)).get("domains") or {"allow": [], "deny": []}

    async def domain_add(self, p, cid, body):
        self._need(p)
        js, _ = await self._svc(p, "POST", "/v1/child/%s/domains" % urllib.parse.quote(cid, safe=""), body)
        self._cache.pop("policy", None)
        return js

    async def domain_remove(self, p, cid, kind, domain):
        self._need(p)
        js, _ = await self._svc(p, "DELETE", "/v1/child/%s/domains?kind=%s&domain=%s" % (urllib.parse.quote(cid, safe=""), urllib.parse.quote(kind), urllib.parse.quote(domain)))
        self._cache.pop("policy", None)
        return js

    async def domain_undo(self, p, cid):
        self._need(p)
        js, _ = await self._svc(p, "POST", "/v1/child/%s/domains/undo" % urllib.parse.quote(cid, safe=""))
        self._cache.pop("policy", None)
        return js

    # ── activity: read from AdGuard on demand, aggregated in memory for this request (and a 30 s paging cache), never stored ──
    async def _collect(self, ip, hours):
        key = "act:%s:%d" % (ip, hours)
        hit = self._cached(key, ACTIVITY_TTL_S)
        if hit is not None:
            return hit
        cutoff = dt.datetime.now(TZ) - dt.timedelta(hours=hours)
        events, older, truncated = {}, None, False
        for page in range(MAX_PAGES):
            path = "/querylog?limit=%d&search=%s" % (PAGE, urllib.parse.quote(ip))
            if older:
                path += "&older_than=" + urllib.parse.quote(older)
            try:
                res = await self.agh.call("GET", path)
            except aghmod.AghError as e:
                raise GwError(503 if e.status is None else 502, "adguard_unavailable", "AdGuard did not answer", {"detail": e.message})
            data = res.get("data") or []
            past = False
            for e in data:
                if e.get("client") != ip:
                    continue
                t = dt.datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(TZ)
                if t < cutoff:
                    past = True
                    continue
                name = e["question"]["name"].lower().rstrip(".")
                events.setdefault(name, []).append((t, str(e.get("reason", "")).startswith("Filtered"), (e.get("question") or {}).get("type", "")))
            older = res.get("oldest")
            if past or len(data) < PAGE or not older:
                break
            if page == MAX_PAGES - 1:
                truncated = True
        rows = []
        for name, evs in events.items():
            use = [x for x in evs if x[2] == "A"] or evs               # a lookup is normally an A plus an AAAA/HTTPS: count the A only
            rows.append({"name": name, "allowed": sum(1 for x in use if not x[1]), "blocked": sum(1 for x in use if x[1]),
                         "first": min(x[0] for x in evs).isoformat(timespec="seconds"), "last": max(x[0] for x in evs).isoformat(timespec="seconds")})
        rows.sort(key=lambda r: r["last"], reverse=True)
        return self._put(key, (rows, truncated))

    async def _list_label(self, name, filters_by_id):
        """(label of the AdGuard list that already flags this name or None, is it a threat feed). Unknown to every list means nothing either way."""
        hit = self._cached("label:" + name, 3600)
        if hit is not None:
            return hit
        res = (None, False)
        try:
            c = await self.agh.check_host(name)
            if aghmod.filtered(c):
                names = [filters_by_id.get(r.get("filter_list_id")) for r in c.get("rules") or [] if r.get("filter_list_id")]
                label = next((n for n in names if n), None) or "AdGuard rule"
                res = (label, any(h in label.lower() for h in THREAT_HINTS))
        except aghmod.AghError:
            pass
        return self._put("label:" + name, res)

    async def activity(self, p, cid, qp):
        self._need(p)
        c = await self._child_raw(p, cid)
        rng = qp.get("range", "24h")
        if rng not in ("24h", "7d"):
            raise GwError(400, "bad_query", "range must be 24h or 7d")
        blocked = qp.get("blocked")
        if blocked not in (None, "0", "1"):
            raise GwError(400, "bad_query", "blocked must be 0 or 1")
        q = (qp.get("q") or "").strip().lower()
        if len(q) > 80:
            raise GwError(400, "bad_query", "q is too long")
        offset = 0
        if qp.get("cursor"):
            m = re.fullmatch(r"o:(\d{1,6})", qp["cursor"])
            if not m:
                raise GwError(400, "bad_query", "bad cursor")
            offset = int(m.group(1))
        rows, truncated = await self._collect(c["ip"], 24 if rng == "24h" else 168)
        if q:
            rows = [r for r in rows if q in r["name"]]
        if blocked == "1":
            rows = [r for r in rows if r["blocked"] > 0]
        elif blocked == "0":
            rows = [r for r in rows if r["blocked"] == 0]
        page = rows[offset:offset + ACTIVITY_PAGE]
        fil = {}
        try:
            fil = {f["id"]: f["name"] for f in (await self.agh.call("GET", "/filtering/status")).get("filters") or []}
        except aghmod.AghError:
            pass
        labels = await asyncio.gather(*[self._list_label(r["name"], fil) for r in page])
        allow, deny = set((c.get("domains") or {}).get("allow", [])), set((c.get("domains") or {}).get("deny", []))

        def listed(name, s):
            return any(name == d or name.endswith("." + d) for d in s)
        out = [{**r, "listed_by": lab[0], "threat": lab[1], "in_allow": listed(r["name"], allow), "in_deny": listed(r["name"], deny)} for r, lab in zip(page, labels)]
        more = offset + ACTIVITY_PAGE < len(rows)
        return {"child": cid, "range": rng, "rows": out, "next_cursor": ("o:%d" % (offset + ACTIVITY_PAGE)) if more else None, "truncated": truncated,
                "note": S["parental_dns_note"], "retention": {"server": S["parental_retention_server"], "replica": S["parental_retention_replica"]}}

    # ── health ────────────────────────────────────────────────────────────────────────────────────────────────────
    def _parity(self):
        try:
            mtime = os.path.getmtime(self.parity_state)
            st = json.load(open(self.parity_state, encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return "unknown", None
        age = max(0, int(self.clock() - mtime))
        if age > 900:
            return "unknown", age
        return ("mismatch" if st.get("streak") else "ok"), age

    async def health(self, p):
        self._need(p, view_only=True)
        out = {"status": "ok", "agh": {"reachable": True}}
        kids = []
        try:
            kids = await self._children_raw(p)
        except GwError:
            pass
        try:
            sh, _ = await self._svc(None, "GET", "/v1/health", role="admin")
        except GwError:
            sh = {}
        cp = (sh or {}).get("child_policy") or {}
        applied = cp.get("applied") or {}
        policy = await self._policy(kids)
        age = None
        if cp.get("generated_at"):
            try:
                age = max(0, int((dt.datetime.now(TZ) - dt.datetime.fromisoformat(cp["generated_at"])).total_seconds()))
            except ValueError:
                pass
        parity, parity_age = self._parity()
        oldest, count, stale = None, 0, False
        try:
            fl = (await self.agh.call("GET", "/filtering/status")).get("filters") or []
            ts = [f["last_updated"] for f in fl if f.get("enabled") and f.get("last_updated") and not f["last_updated"].startswith("0001")]
            count, oldest = len(fl), (min(ts) if ts else None)
            if oldest:
                stale = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds() > LIST_STALE_S
        except (aghmod.AghError, ValueError):
            out["agh"]["reachable"] = False
        in_sync = policy["in_sync"]
        out.update({"policy": {"expected_hash": policy["expected_hash"], "applied_hash": policy["applied_hash"], "in_sync": bool(in_sync),
                               "last_apply_at": applied.get("at"), "source": applied.get("source"), "last_apply_ok": applied.get("ok")},
                    "snapshot": {"checksum": cp.get("checksum"), "age_s": age},
                    "replica": {"sync_status": None, "sync_age_s": parity_age, "parity": parity},
                    "lists": {"count": count, "oldest_updated": oldest, "stale": stale}})
        if not out["agh"]["reachable"] or in_sync is not True or applied.get("ok") is False or parity == "mismatch":
            out["status"] = "degraded"
        if self.filters is not None:
            out["filter_reverts"] = self.filters.recent_reverts()
        return out
