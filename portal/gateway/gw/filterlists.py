"""AdGuard block lists with a safety net (Task 29, Phase G; admin only).

Every change - add, remove, enable, disable, rename, refresh, revert - runs the same pipeline:

  validate -> snapshot the previous state -> apply on the origin -> probe -> replica follows -> parity agrees -> (ANY failure) revert to the snapshot and probe again

  validate   URL (https; a fetch address must be public unless an operator wrote it into data/filter-fetch-allow.txt), reachable, size and rule count within the
             limits (above `max_rules` the admin must pass confirm_large, above `hard_cap_rules` never), no duplicate. A refresh whose list changed by more than
             `change_percent` is HELD (nothing applied) until the admin confirms.
  probe      names the house depends on still resolve (check_host, the same engine that answers queries) and a few real UDP lookups against the origin resolver;
             sentinels stay blocked; each child device still has its catch-all and its always-on host. "Still" = same as the baseline taken before the change,
             so a name that was already blocked never fails a change by itself.
  replica    witnesses taken from the list's own content are looked up on the replica's DNS until it blocks (add/enable) or stops blocking (remove/disable) them.
  parity     the parity checker (every 5 min) must have run AFTER the change and report no mismatch.

Only ONE change runs at a time (409 otherwise). A change interrupted by a restart is reverted at start-up. The history keeps what was done and why, including every
automatic revert; the gateway raises it in /v1/health so the host watcher can tell Ivan once (tier B).
"""
import asyncio
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import agh as aghmod
from .auth import GwError
from .db import canon, iso

LIMITS = {"max_rules": 300_000, "hard_cap_rules": 1_000_000, "change_percent": 30}
MAX_BYTES = 48 * 1024 * 1024
FETCH_TIMEOUT_S = 30
REGISTRY = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_%d.txt"
# a short curated subset of AdGuard's own registry (checked against filters.json on 2026-10-20); the ones the house already runs are marked "installed" in the view
CATALOGUE = [
    ("adguard_dns_filter", "AdGuard DNS filter", "Ads and trackers, AdGuard's own base list", 1),
    ("adaway", "AdAway Default Blocklist", "Mobile ad servers", 2),
    ("peter_lowe_list", "Peter Lowe's Blocklist", "Ad and tracking servers", 3),
    ("dan_pollocks_list", "Dan Pollock's List", "Hosts-file list of ad, tracking and malware servers", 4),
    ("oisd_basic", "OISD Blocklist Small", "A compact, low-false-positive list", 5),
    ("hagezi_multinormal", "HaGeZi's Normal Blocklist", "Balanced ads, trackers and malware", 34),
    ("hagezi_pro", "HaGeZi's Pro Blocklist", "Stricter than Normal", 48),
    ("urlhaus_filter_online", "Malicious URL Blocklist (URLHaus)", "Malware hosts (threat feed)", 11),
    ("phishing_army", "Phishing Army", "Phishing domains (threat feed)", 18),
    ("scam_blocklist_by_durablenapkin", "Scam Blocklist by DurableNapkin", "Scam domains", 10),
    ("curben_phishing_filter", "Phishing URL Blocklist (PhishTank and OpenPhish)", "Phishing (threat feed)", 30),
    ("hagezi_threat_intelligence_feeds", "HaGeZi's Threat Intelligence Feeds", "Threat intelligence feeds", 44),
    ("hagezi_gambling_blocklist", "HaGeZi's Gambling Blocklist", "Gambling sites", 47),
]
_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_ADBLOCK = re.compile(r"^\|\|([a-z0-9.-]+)\^$")
_HOSTS = re.compile(r"^(?:0\.0\.0\.0|127\.0\.0\.1)\s+([a-z0-9.-]+)$")
PROBE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_probe.json")


class Refused(Exception):
    """A validation failure: nothing was touched. carries (status, code, message, details)."""
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def norm_url(u):
    return (u or "").strip()


def count_rules(text):
    """(rule count, [plain domains found]) of an adblock/hosts/domain-list text."""
    n, doms = 0, []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "!#[":
            continue
        n += 1
        if len(doms) < 5000:
            low = s.lower()
            m = _ADBLOCK.match(low) or _HOSTS.match(low)
            d = m.group(1) if m else (low if _DOMAIN.match(low) else None)
            if d and _DOMAIN.match(d):
                doms.append(d)
    return n, doms


def public_address(host, port, allowed):
    """True when every address the host resolves to is public - or the operator listed host[:port] in the allow file (the proof with a scratch list uses that)."""
    if host in allowed or "%s:%s" % (host, port) in allowed:
        return True
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _NoPrivateRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, check):
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url, allowed=(), max_bytes=MAX_BYTES, timeout=FETCH_TIMEOUT_S):
    """Downloads a list. Raises Refused. https only (http only for an operator-allowed address); no credentials in the URL; public addresses only; size capped."""
    p = urllib.parse.urlsplit(url)

    def check(u):
        q = urllib.parse.urlsplit(u)
        port = q.port or (443 if q.scheme == "https" else 80)
        listed = q.hostname in allowed or "%s:%s" % (q.hostname, port) in allowed
        if q.scheme != "https" and not (q.scheme == "http" and listed):
            raise Refused(422, "bad_url", "only https lists are accepted")
        if q.username or q.password:
            raise Refused(422, "bad_url", "the URL must not contain credentials")
        if not q.hostname or not public_address(q.hostname, port, allowed):
            raise Refused(422, "bad_url", "the address of this list is not a public one")
    if len(url) > 500 or re.search(r"\s", url):
        raise Refused(422, "bad_url", "not a usable URL")
    check(url)
    opener = urllib.request.build_opener(_NoPrivateRedirect(check))
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "minisserver-portal/1"}), timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except Refused:
        raise
    except urllib.error.HTTPError as e:
        raise Refused(422, "unreachable", "the list answered HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, ValueError):
        raise Refused(422, "unreachable", "the list could not be downloaded")
    if len(data) > max_bytes:
        raise Refused(422, "too_large", "the list is larger than %d MiB" % (max_bytes // 1048576))
    return data.decode("utf-8", "replace")


async def _no_children():
    return []


class FilterLists:
    def __init__(self, agh, db, clock=time.time, *, fetch=None, dns=None, children=None, allow_file=None, parity_state=None, origin_dns="192.168.31.2",
                 replica_dns="192.168.31.5", replica_wait_s=120.0, parity_wait_s=340.0, poll_s=3.0, settle_s=8.0, settle_gap=1.0, limits=None, probe_file=PROBE_FILE,
                 sleep=None):
        self.agh, self.db, self.clock = agh, db, clock
        self._fetch = fetch
        self.dns = dns or aghmod.dns_query
        self.children = children or _no_children              # async -> [ip, ...]
        self.allow_file = allow_file
        self.parity_state = parity_state
        self.origin_dns, self.replica_dns = origin_dns, replica_dns
        self.replica_wait_s, self.parity_wait_s, self.poll_s = replica_wait_s, parity_wait_s, poll_s
        self.settle_s, self.settle_gap = settle_s, settle_gap         # AdGuard reloads its engine about a second AFTER a list call returns (measured live, 2026-10-20)
        self.block_mode = "default"                                   # AdGuard's blocking mode, read at every baseline
        self.limits = dict(limits or LIMITS)
        self.sleep = sleep or asyncio.sleep
        with open(probe_file, encoding="utf-8") as f:
            self.probe = json.load(f)
        self._busy = False

    # ── plumbing ──────────────────────────────────────────────────────────────────────────────────────────────────
    def _allowed(self):
        try:
            with open(self.allow_file, encoding="utf-8") as f:
                return {ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()}
        except (OSError, TypeError):
            return set()

    async def fetch(self, url):
        if self._fetch:
            return await asyncio.to_thread(self._fetch, url)
        return await asyncio.to_thread(fetch_url, url, self._allowed())

    async def lists(self):
        try:
            st = await self.agh.call("GET", "/filtering/status")
        except aghmod.AghError as e:
            raise GwError(502, "adguard_unreachable", "AdGuard did not answer", {"detail": e.message})
        return [{"id": f["id"], "name": f["name"], "url": f["url"], "enabled": bool(f["enabled"]), "rules_count": int(f.get("rules_count") or 0),
                 "last_updated": f.get("last_updated") or None} for f in st.get("filters") or []]

    @staticmethod
    def _by(actor):
        return actor.get("name") or actor.get("person_id") or "?"

    # ── read views ────────────────────────────────────────────────────────────────────────────────────────────────
    def history(self, limit=20):
        rows = self.db.q("SELECT * FROM filter_changes ORDER BY at DESC, rowid DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r):
        return {"id": r["id"], "at": iso(r["at"]), "by": r["by"], "action": r["action"], "list_id": r["list_id"], "list_name": r["list_name"],
                "result": r["result"], "steps": json.loads(r["steps"] or "[]"), "reason": r["reason"]}

    async def view(self):
        return {"lists": await self.lists(), "limits": dict(self.limits), "history": self.history()}

    async def catalogue(self):
        have = {l["url"] for l in await self.lists()}
        return {"items": [{"catalogue_id": cid, "name": name, "description": desc, "url": REGISTRY % n, "installed": (REGISTRY % n) in have} for cid, name, desc, n in CATALOGUE]}

    def recent_reverts(self, hours=48):
        rows = self.db.q("SELECT * FROM filter_changes WHERE result IN ('reverted','failed') AND at>? ORDER BY at DESC", (self.clock() - hours * 3600,))
        return [{"id": r["id"], "at": iso(r["at"]), "action": r["action"], "list_name": r["list_name"], "reason": r["reason"]} for r in rows]

    # ── validation ────────────────────────────────────────────────────────────────────────────────────────────────
    async def _validate_list(self, url, confirm_large, existing, steps, exclude_id=None):
        url = norm_url(url)
        if any(l["url"] == url and l["id"] != exclude_id for l in existing):
            raise Refused(409, "duplicate", "this list is already installed")
        text = await self.fetch(url)
        n, doms = count_rules(text)
        if n == 0:
            raise Refused(422, "empty_list", "the list contains no rules")
        if n > self.limits["hard_cap_rules"]:
            raise Refused(422, "over_hard_cap", "the list has %d rules; the hard cap is %d" % (n, self.limits["hard_cap_rules"]), rules=n)
        if n > self.limits["max_rules"] and not confirm_large:
            raise Refused(422, "confirm_large_required", "the list has %d rules (above %d): send confirm_large to accept" % (n, self.limits["max_rules"]), rules=n)
        steps.append({"name": "validate", "ok": True, "detail": "%d rules" % n})
        return n, doms

    # ── the probe ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def _dynamic_names(self):
        try:
            st = await self.agh.call("GET", "/stats")
        except aghmod.AghError:
            return []
        blocked = {k for x in st.get("top_blocked_domains") or [] for k in x}
        skip = self.probe.get("skip_dynamic", [])
        out = []
        for x in st.get("top_queried_domains") or []:
            name = next(iter(x), "")
            if not name or name in blocked or any(s in name for s in skip):
                continue
            out.append(name)
            if len(out) >= 40:
                break
        return out

    async def _checks(self, names, client=None):
        sem = asyncio.Semaphore(8)

        async def one(n):
            async with sem:
                try:
                    return n, await self.agh.check_host(n, client)
                except aghmod.AghError:
                    return n, None
        return dict(await asyncio.gather(*[one(n) for n in names]))

    async def baseline(self):
        try:
            self.block_mode = (await self.agh.call("GET", "/dns_info")).get("blocking_mode") or "default"
        except aghmod.AghError:
            self.block_mode = "default"
        names = list(dict.fromkeys(self.probe["must_resolve"] + await self._dynamic_names()))
        res = await self._checks(names + self.probe["sentinels"])
        must = [n for n in names if res.get(n) is not None and not aghmod.filtered(res[n])]
        sentinels = {n: sorted(aghmod.blocking_list_ids(res[n])) for n in self.probe["sentinels"] if res.get(n) is not None and aghmod.filtered(res[n])}
        kids = {}
        cc = self.probe["child_check"]
        for ip in await self.children():
            r = await self._checks([cc["always_on"], cc["never_allowed"]], ip)
            kids[ip] = {k: (None if r.get(v) is None else aghmod.filtered(r[v])) for k, v in (("always_on", cc["always_on"]), ("never_allowed", cc["never_allowed"]))}
        return {"must": must, "sentinels": sentinels, "children": kids}

    async def _probe(self, base, steps, name, removed_list_ids=(), attempts=1):
        """Compares the situation after a change with the baseline. Returns True when every check passes; appends one step. attempts > 1 (after a revert) repeats it
        once per settle_gap until it passes: AdGuard's engine needs about a second to take a change in."""
        for attempt in range(max(1, attempts)):
            problems, real = await self._problems(base, removed_list_ids)
            if not problems or attempt == max(1, attempts) - 1:
                break
            await self.sleep(self.settle_gap)
        steps.append({"name": name, "ok": not problems, "detail": ("; ".join(problems[:4]) + (" (+%d)" % (len(problems) - 4) if len(problems) > 4 else "")) if problems else
                      "%d names resolve, %d sentinels blocked, %d children unchanged, %d real lookups" % (len(base["must"]), len(base["sentinels"]), len(base["children"]), len(real))})
        return not problems

    async def _settle(self, witnesses, want_blocked):
        """Waits (bounded) until AdGuard's engine shows the change: a witness name of the list is blocked (add/enable) or no longer blocked (remove/disable). Without a
        witness: a short fixed pause. Without this the probe could look at the OLD engine and pass vacuously."""
        if not witnesses:
            await self.sleep(min(1.0, self.settle_s))
            return
        deadline = self.clock() + self.settle_s
        while True:
            seen = True
            for w in witnesses:
                try:
                    seen = aghmod.filtered(await self.agh.check_host(w)) == want_blocked
                except aghmod.AghError:
                    seen = False
                if not seen:
                    break
            if seen or self.clock() >= deadline:
                return
            await self.sleep(min(0.25, self.settle_gap))

    async def _problems(self, base, removed_list_ids=()):
        problems = []
        res = await self._checks(base["must"])
        for n in base["must"]:
            if res.get(n) is None:
                problems.append("%s: no answer from AdGuard" % n)
            elif aghmod.filtered(res[n]):
                problems.append("%s is blocked now" % n)
        if base["sentinels"]:
            sres = await self._checks(list(base["sentinels"]))
            for n, ids in base["sentinels"].items():
                still = [i for i in ids if i not in removed_list_ids]
                if still and not (sres.get(n) and aghmod.filtered(sres[n])):
                    problems.append("sentinel %s is not blocked any more" % n)
        cc = self.probe["child_check"]
        for ip, was in base["children"].items():
            r = await self._checks([cc["always_on"], cc["never_allowed"]], ip)
            now = {"always_on": None if r.get(cc["always_on"]) is None else aghmod.filtered(r[cc["always_on"]]),
                   "never_allowed": None if r.get(cc["never_allowed"]) is None else aghmod.filtered(r[cc["never_allowed"]])}
            if now != was:
                problems.append("child %s: policy answers changed (%s -> %s)" % (ip, was, now))
        # real lookups against the origin resolver: DNS itself still answers, ordinary names resolve, a sentinel is sinkholed
        real = [n for n in base["must"][:3]]
        for n in real:
            try:
                a = await asyncio.to_thread(self.dns, self.origin_dns, n)
                if a["rcode"] != 0 or not a["addrs"] or aghmod.dns_blocked(a, self.block_mode):
                    problems.append("lookup of %s on the origin resolver: rcode %s, %s" % (n, a["rcode"], a["addrs"][:2]))
            except OSError:
                problems.append("lookup of %s on the origin resolver timed out" % n)
        return problems, real

    # ── replica and parity ────────────────────────────────────────────────────────────────────────────────────────
    async def _wait_replica(self, witnesses, want_blocked, steps):
        if not witnesses:
            steps.append({"name": "replica", "ok": True, "detail": "no witness domains for this change; the parity checker decides"})
            return True
        deadline = self.clock() + self.replica_wait_s
        last = ""
        while True:
            ok = 0
            for w in witnesses:
                try:
                    a = await asyncio.to_thread(self.dns, self.replica_dns, w)
                    if aghmod.dns_blocked(a, self.block_mode) == want_blocked:
                        ok += 1
                    last = "%s answers %s" % (w, a["addrs"][:1] or a["rcode"])
                except OSError:
                    last = "%s: the replica did not answer" % w
            if ok == len(witnesses):
                steps.append({"name": "replica", "ok": True, "detail": "%d witnesses %s on the replica" % (ok, "blocked" if want_blocked else "unblocked")})
                return True
            if self.clock() >= deadline:
                steps.append({"name": "replica", "ok": False, "detail": "the replica did not follow within %d s (%s)" % (self.replica_wait_s, last)})
                return False
            await self.sleep(self.poll_s)

    def _read_parity(self):
        try:
            p = self.parity_state
            return os.path.getmtime(p), json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None, None

    async def _wait_parity(self, since, steps):
        deadline = self.clock() + self.parity_wait_s
        while True:
            mtime, st = self._read_parity()
            if st is not None and mtime >= since:
                bad = sorted((st.get("streak") or {}).keys())
                steps.append({"name": "parity", "ok": not bad, "detail": ("mismatch: " + ", ".join(bad[:4])) if bad else "the parity checker ran after the change and found no difference"})
                return not bad
            if self.clock() >= deadline:
                steps.append({"name": "parity", "ok": False, "detail": "the parity checker did not run within %d s" % self.parity_wait_s})
                return False
            await self.sleep(self.poll_s)

    # ── applying and restoring ────────────────────────────────────────────────────────────────────────────────────
    async def _do(self, op):
        kind, p = op
        if kind == "add":
            await self.agh.call("POST", "/filtering/add_url", {"name": p["name"], "url": p["url"], "whitelist": False})
        elif kind == "remove":
            await self.agh.call("POST", "/filtering/remove_url", {"url": p["url"], "whitelist": False})
        elif kind == "set":
            await self.agh.call("POST", "/filtering/set_url", {"url": p["url"], "whitelist": False, "data": {"name": p["name"], "url": p["url"], "enabled": p["enabled"]}})
        elif kind == "refresh":
            await self.agh.call("POST", "/filtering/refresh", {"whitelist": False}, timeout=120)

    @staticmethod
    def diff_ops(current, desired, only_url=None):
        """Operations that turn `current` into `desired` (both: list of {url,name,enabled}); only_url limits it to one list."""
        cur = {l["url"]: l for l in current}
        want = {l["url"]: l for l in desired}
        ops = []
        for url, l in want.items():
            if only_url and url != only_url:
                continue
            if url not in cur:
                ops.append(("add", {"name": l["name"], "url": url}))
                if not l["enabled"]:
                    ops.append(("set", {"name": l["name"], "url": url, "enabled": False}))
            elif (cur[url]["name"], cur[url]["enabled"]) != (l["name"], l["enabled"]):
                ops.append(("set", {"name": l["name"], "url": url, "enabled": l["enabled"]}))
        for url in cur:
            if (not only_url or url == only_url) and url not in want:
                ops.append(("remove", {"url": url}))
        return ops

    async def _apply(self, ops, steps):
        try:
            for op in ops:
                await self._do(op)
            steps.append({"name": "apply", "ok": True, "detail": ", ".join("%s %s" % (k, (p.get("name") or p.get("url"))) for k, p in ops)[:200]})
            return True
        except aghmod.AghError as e:
            steps.append({"name": "apply", "ok": False, "detail": "AdGuard refused: %s" % e.message[:160]})
            return False

    # ── the pipeline ──────────────────────────────────────────────────────────────────────────────────────────────
    def _begin(self, actor, action, list_id, list_name, snapshot):
        cid = "f" + uuid.uuid4().hex[:10]
        self.db.x("INSERT INTO filter_changes(id,at,by,action,list_id,list_name,result,steps,reason,snapshot) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (cid, self.clock(), self._by(actor), action, list_id, list_name, "running", "[]", None, canon(snapshot)))
        return cid

    def _finish(self, cid, result, steps, reason, actor, action, list_name):
        self.db.x("UPDATE filter_changes SET result=?, steps=?, reason=?, finished=? WHERE id=?", (result, canon(steps), reason, self.clock(), cid))
        self.db.audit("filter_list_" + action, person=actor.get("person_id"), phone=actor.get("phone_id"), target=list_name,
                      after={"change": cid, "result": result, "failed": [s["name"] for s in steps if not s["ok"]]}, ok=result in ("applied", "held"), ip=actor.get("ip"))
        row = self.db.q1("SELECT * FROM filter_changes WHERE id=?", (cid,))
        return self._row(row)

    async def _pipeline(self, actor, action, list_id, list_name, ops, *, steps, witnesses=(), witnesses_blocked=True, removed_ids=(), wait_parity=True):
        """validate has already run (its step is in `steps`). Returns the stored change; result applied|reverted."""
        if self._busy:
            raise Refused(409, "filter_busy", "another filter-list change is running")
        self._busy = True
        try:
            snapshot = await self.lists()
            cid = self._begin(actor, action, list_id, list_name, snapshot)
            base = {"must": [], "sentinels": {}, "children": {}}
            try:
                base = await self.baseline()
                started = self.clock()
                ok = await self._apply(ops, steps)
                if ok:
                    await self._settle(list(witnesses), witnesses_blocked)
                    ok = await self._probe(base, steps, "probe", removed_ids)
                    if ok:
                        ok = await self._wait_replica(list(witnesses), witnesses_blocked, steps)
                    if ok and wait_parity:
                        ok = await self._wait_parity(started, steps)
            except Exception as e:                       # noqa: BLE001 - whatever went wrong, the lists go back to the snapshot (no secret in the text: class name only)
                steps.append({"name": "internal", "ok": False, "detail": "unexpected %s" % e.__class__.__name__})
                ok = False
            if ok:
                return self._finish(cid, "applied", steps, None, actor, action, list_name)
            failed = next(s for s in steps if not s["ok"])
            reason = "%s failed: %s" % (failed["name"], failed["detail"])
            await self._restore(snapshot, base, steps)
            return self._finish(cid, "reverted", steps, reason, actor, action, list_name)
        finally:
            self._busy = False

    async def _restore(self, snapshot, base, steps):
        try:
            cur = await self.lists()
            ops = self.diff_ops(cur, snapshot)
            for op in ops:
                await self._do(op)
            steps.append({"name": "revert", "ok": True, "detail": "%d operation(s) back to the snapshot" % len(ops)})
        except (aghmod.AghError, GwError) as e:
            steps.append({"name": "revert", "ok": False, "detail": "could not restore: %s" % getattr(e, "message", str(e))[:160]})
            return
        await self._probe(base, steps, "probe_after_revert", attempts=max(1, round(self.settle_s / self.settle_gap)))

    async def recover(self):
        """At start: a change that was running when the gateway stopped is put back to its snapshot."""
        rows = self.db.q("SELECT * FROM filter_changes WHERE result='running'")
        for r in rows:
            steps = json.loads(r["steps"] or "[]")
            try:
                snap = json.loads(r["snapshot"])
                cur = await self.lists()
                for op in self.diff_ops(cur, snap):
                    await self._do(op)
                steps.append({"name": "revert", "ok": True, "detail": "the gateway restarted during this change; restored the snapshot"})
                self._finish(r["id"], "reverted", steps, "interrupted by a restart", {"person_id": None}, r["action"], r["list_name"])
            except Exception as e:                       # noqa: BLE001
                steps.append({"name": "revert", "ok": False, "detail": "restart recovery failed: %s" % str(e)[:120]})
                self._finish(r["id"], "failed", steps, "interrupted by a restart; could not restore", {"person_id": None}, r["action"], r["list_name"])

    # ── public operations (each returns {"result", "change", "list", "hold_reason"}) ──────────────────────────────
    def _result(self, change, lst=None, hold=None):
        return {"result": change["result"], "change": change, "list": lst, "hold_reason": hold}

    def _refuse(self, e):
        raise GwError(e.status, e.code, e.message, e.details)

    async def add(self, actor, body):
        steps = []
        try:
            existing = await self.lists()
            cat_id, url, name = body.get("catalogue_id"), norm_url(body.get("url")), (body.get("name") or "").strip()
            if bool(cat_id) == bool(url):
                raise Refused(400, "bad_body", "send either catalogue_id or url")
            if cat_id:
                item = next((c for c in CATALOGUE if c[0] == cat_id), None)
                if not item:
                    raise Refused(404, "not_found", "no such catalogue entry")
                url, name = REGISTRY % item[3], name or item[1]
            if not name:
                raise Refused(400, "bad_body", "a name is required for a list added by URL")
            if len(name) > 80:
                raise Refused(400, "bad_body", "the name is too long")
            n, doms = await self._validate_list(url, bool(body.get("confirm_large")), existing, steps)
        except Refused as e:
            self._refuse(e)
        before = {l["id"] for l in existing}
        witnesses = await self._witnesses(doms, want_blocked=True)
        ch = await self._run(actor, "add", None, name, [("add", {"name": name, "url": url})], steps, witnesses, True, ())
        new = next((l for l in await self.lists() if l["url"] == url and l["id"] not in before), None) if ch["result"] == "applied" else None
        if new:
            self.db.x("UPDATE filter_changes SET list_id=? WHERE id=?", (new["id"], ch["id"]))
            ch["list_id"] = new["id"]
        return self._result(ch, new)

    async def _run(self, actor, action, list_id, list_name, ops, steps, witnesses, blocked, removed_ids, wait_parity=True):
        try:
            return await self._pipeline(actor, action, list_id, list_name, ops, steps=steps, witnesses=witnesses, witnesses_blocked=blocked,
                                        removed_ids=removed_ids, wait_parity=wait_parity)
        except Refused as e:
            self._refuse(e)

    async def _witnesses(self, doms, want_blocked, only_blocked_by=None):
        """Up to 3 domains of the list whose state on the replica tells us it followed: for an addition domains that are NOT blocked now; for a removal domains
        blocked now by this list alone."""
        out = []
        for d in doms[:400]:
            try:
                c = await self.agh.check_host(d)
            except aghmod.AghError:
                continue
            if want_blocked and not aghmod.filtered(c):
                out.append(d)
            elif not want_blocked and aghmod.filtered(c) and aghmod.blocking_list_ids(c) == {only_blocked_by}:
                out.append(d)
            if len(out) >= 3:
                break
        return out

    async def _find(self, list_id):
        lst = next((l for l in await self.lists() if l["id"] == list_id), None)
        if not lst:
            raise GwError(404, "not_found", "no such list", {"id": list_id})
        return lst

    async def patch(self, actor, list_id, body):
        lst = await self._find(list_id)
        enabled = body.get("enabled", lst["enabled"])
        name = (body.get("name") if body.get("name") is not None else lst["name"]).strip()
        if not isinstance(enabled, bool) or not name or len(name) > 80:
            raise GwError(400, "bad_body", "enabled must be true/false and the name 1-80 characters")
        if enabled == lst["enabled"] and name == lst["name"]:
            raise GwError(409, "no_change", "the list is already like that")
        action = "rename" if enabled == lst["enabled"] else ("enable" if enabled else "disable")
        steps = [{"name": "validate", "ok": True, "detail": "list %s" % lst["name"]}]
        doms, blocked = [], True
        if enabled != lst["enabled"]:
            try:
                _n, doms = count_rules(await self.fetch(lst["url"]))
            except Refused:
                doms = []                                     # a list we cannot download now can still be switched; the parity checker covers the replica
            doms = await self._witnesses(doms, want_blocked=True) if enabled else await self._witnesses(doms, want_blocked=False, only_blocked_by=list_id)
            blocked = enabled
        ch = await self._run(actor, action, list_id, lst["name"], [("set", {"name": name, "url": lst["url"], "enabled": enabled})], steps, doms, blocked,
                             () if enabled else (list_id,))
        return self._result(ch, next((l for l in await self.lists() if l["id"] == list_id), None))

    async def remove(self, actor, list_id):
        lst = await self._find(list_id)
        steps = [{"name": "validate", "ok": True, "detail": "list %s" % lst["name"]}]
        doms = []
        if lst["enabled"]:
            try:
                _n, doms = count_rules(await self.fetch(lst["url"]))
            except Refused:
                doms = []
        w = await self._witnesses(doms, want_blocked=False, only_blocked_by=list_id)
        ch = await self._run(actor, "remove", list_id, lst["name"], [("remove", {"url": lst["url"]})], steps, w, False, (list_id,))
        return self._result(ch, None)

    async def refresh(self, actor, list_id, confirm=False):
        lst = await self._find(list_id)
        steps = []
        try:
            text = await self.fetch(lst["url"])
        except Refused as e:
            self._refuse(e)
        n, _doms = count_rules(text)
        if n == 0:
            self._refuse(Refused(422, "empty_list", "the list now contains no rules"))
        old = lst["rules_count"]
        pct = abs(n - old) * 100.0 / old if old else 0.0
        steps.append({"name": "validate", "ok": True, "detail": "%d rules now, %d before (%+.0f%%)" % (n, old, (n - old) * 100.0 / old if old else 0)})
        if old and pct > self.limits["change_percent"] and not confirm:
            reason = "the list changed by %.0f%% (limit %d%%): %d -> %d rules; send confirm to apply it" % (pct, self.limits["change_percent"], old, n)
            steps.append({"name": "hold", "ok": True, "detail": reason})
            cid = self._begin(actor, "refresh", list_id, lst["name"], [])
            return self._result(self._finish(cid, "held", steps, reason, actor, "refresh", lst["name"]), lst, reason)
        ch = await self._run(actor, "refresh", list_id, lst["name"], [("refresh", {})], steps, [], True, ())
        return self._result(ch, next((l for l in await self.lists() if l["id"] == list_id), None))

    async def revert(self, actor, list_id):
        """Puts one list back to the state before its last applied change (add -> remove, remove -> add, enable/disable/rename -> the old values)."""
        rows = self.db.q("SELECT * FROM filter_changes WHERE result='applied' AND action IN ('add','remove','enable','disable','rename') AND list_id=? ORDER BY at DESC, rowid DESC LIMIT 1", (list_id,))
        if not rows:
            raise GwError(409, "not_revertable", "there is no change of this list that can be reverted")
        snap = json.loads(rows[0]["snapshot"])
        cur = await self.lists()
        mine = next((l for l in cur if l["id"] == list_id), None)
        before = next((l for l in snap if l["id"] == list_id), None)
        url = (mine or before or {}).get("url")
        if not url:
            raise GwError(404, "not_found", "no such list")
        ops = self.diff_ops(cur, snap, only_url=url)
        if not ops:
            raise GwError(409, "no_change", "the list is already as it was before the change")
        name = (mine or before)["name"]
        steps = [{"name": "validate", "ok": True, "detail": "back to the state before change %s" % rows[0]["id"]}]
        removing = any(k == "remove" or (k == "set" and not p["enabled"]) for k, p in ops)
        ch = await self._run(actor, "revert", list_id, name, ops, steps, [], True, (list_id,) if removing else ())
        return self._result(ch, next((l for l in await self.lists() if l["url"] == url), None))
