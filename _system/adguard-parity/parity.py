#!/usr/bin/env python3
"""adguard-parity (Task 21): proves the replica is IDENTICAL to the origin, instead of trusting the sync.

Every run (timer: 5 min) it compares
  1. CONFIG: custom rules byte-for-byte, persistent clients, DNS config, filter lists (url/name/enabled),
     rewrites, access list, safe search, blocked services, parental/safebrowsing flags, protection state,
     versions. Rule COUNTS per list may differ by TOLERANCE (each instance refreshes lists on its own);
     that alone alerts only if it persists over an hour.
  2. BEHAVIOUR: the same fixed probe set sent to both resolvers (real DNS answers), and AGH's check_host
     with a client parameter for the child device and for a normal device. check_host is used instead of
     changing the real child's DNS.
A mismatch that is present in two consecutive runs sends ONE Telegram message naming the probe; recovery
sends one more. A daily "parity OK" line goes to the log, not to Telegram.

The intentional differences (see pi/adguard/conf/AdGuardHome.yaml.example header) are excluded below.
"""
import base64
import datetime
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402  - the shared Telegram/reminder helper (Task 24)

STATE_DIR = os.environ.get("PARITY_STATE_DIR", "/var/lib/adguard-parity")
STATE = os.path.join(STATE_DIR, "state.json")
OK_LOG = os.path.join(STATE_DIR, "ok.log")
TG_ENV = "/srv/compose/scrutiny/.env"
SYNC_ENV = "/srv/compose/adguard-sync/.env"
ORIGIN = ("http://127.0.0.1:3000", "admin", "/home/skit/adguard-admin-password.txt")
REPLICA = ("https://piserver.tail6bf4d5.ts.net", "admin", None)
ORIGIN_DNS, REPLICA_DNS = "192.168.31.2", "192.168.31.5"
TOLERANCE = 0.05          # relative difference in per-list rule counts that is "just refresh timing"
COUNT_GRACE = 3600        # seconds a count difference may persist before it alerts
CONSECUTIVE = int(os.environ.get("PARITY_CONSECUTIVE", "2"))          # runs a mismatch must persist before alerting
STALE_SECONDS = int(os.environ.get("PARITY_STALE_SECONDS", "1800"))   # a successful sync pass older than this is an alert
CLIENTS_CONF = "/srv/compose/adguard/child-policy/clients.conf"

# Intentional differences (not compared). dns_info: default_local_ptr_upstreams are computed from each host's own
# resolvers. (use_private_ptr_resolvers is false on both since the 2026-10-02 DNS loop and IS compared.)
DNS_INFO_SKIP = {"default_local_ptr_upstreams"}

DNS_PROBES = ["example.com", "doubleclick.net", "use-application-dns.net", "dnssec-failed.org",
              "parity-probe-nonexistent-name.example.com"]
# Time-aware child-policy probe (Task 24 B). The names cover every class of the schedule: opened by the 16-19 window
# (youtube, roblox), always blocked (tiktok, google accounts), always allowed (telegram, weather). AGH's check_host with a
# client parameter evaluates the rules AS THAT CLIENT would hit them, so the verdict flips at 16:00 / 19:00 / Sunday on
# BOTH instances - a replica that missed a window change differs here at once, whatever its config text looks like.
CHILD_NAMES = ["youtube.com", "www.youtube.com", "roblox.com", "www.tiktok.com", "t.me", "telegram.org",
               "api.accuweather.com", "accounts.google.com"]
NORMAL_NAMES = ["youtube.com", "doubleclick.net", "example.com"]
NORMAL_CLIENT = "192.168.31.107"


def child_clients():
    """{nickname: ip} for every device in child-policy/clients.conf (so a new child device is probed automatically)."""
    out = {}
    try:
        for line in open(CLIENTS_CONF):
            line = line.split("#", 1)[0].strip()
            if line:
                ip, name, *_ = line.split()
                out[name] = ip
    except OSError:
        pass
    return out


def read_env(path, key):
    for line in open(path):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise KeyError(key)


def api(inst, path):
    url, user, pw = inst
    req = urllib.request.Request(url + "/control" + path)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def strip(obj, drop):
    if isinstance(obj, dict):
        return {k: strip(v, drop) for k, v in obj.items() if k not in drop}
    if isinstance(obj, list):
        return [strip(x, drop) for x in obj]
    return obj


def lists(status):
    out = {}
    for key in ("filters", "whitelist_filters"):
        for f in status.get(key) or []:
            out[(key, f["url"])] = (f.get("name"), f.get("enabled"), f.get("rules_count", 0))
    return out


def norm_ip(v):
    return v if v in ("0.0.0.0", "::") else ("IP6" if ":" in v else "IP4")


def dig(server, name):
    p = subprocess.run(["dig", f"@{server}", name, "A", "+time=4", "+tries=1", "+noall", "+comments", "+answer"],
                       capture_output=True, text=True)
    m = re.search(r"status: (\w+)", p.stdout)
    status = m.group(1) if m else "NOREPLY"
    ans = sorted({norm_ip(l.split()[-1]) for l in p.stdout.splitlines() if re.match(r"^\S+\s+\d+\s+IN\s+A\s", l)})
    return status, ans


def check_host(inst, name, client):
    r = api(inst, "/filtering/check_host?name=%s&client=%s" % (urllib.parse.quote(name), client))
    return r.get("reason"), sorted(x.get("text", "") for x in r.get("rules") or [])


def threat_domain(origin, st):
    """A domain from the URLhaus list, stable between runs while the origin still lists it."""
    dom = st.get("threat_domain")
    if dom:
        reason, _ = check_host(origin, dom, "192.168.31.107")
        if str(reason).startswith("Filtered"):
            return dom
    fs = api(origin, "/filtering/status")
    for f in fs.get("filters") or []:
        if "urlhaus" in (f.get("name") or "").lower() or "malicious" in (f.get("name") or "").lower():
            path = f"/srv/apps/adguard/work/data/filters/{f['id']}.txt"
            try:
                lines = subprocess.run(["sudo", "-n", "sed", "-n", "200,260p", path], capture_output=True, text=True).stdout.splitlines()
            except OSError:
                continue
            for l in lines:
                m = re.match(r"^\|\|([a-z0-9.-]+)\^", l)
                if m:
                    st["threat_domain"] = m.group(1)
                    return m.group(1)
    return None


def compare():
    """Returns (problems: dict probe-name -> text, notes: list). Raises if an instance is unreachable."""
    origin = (ORIGIN[0], ORIGIN[1], open(ORIGIN[2]).read().strip())
    replica = (REPLICA[0], REPLICA[1], read_env(SYNC_ENV, "REPLICA1_PASSWORD"))
    st = load_state()
    problems, notes = {}, []

    def eq(name, a, b):
        if a != b:
            problems[name] = f"origin={str(a)[:140]} replica={str(b)[:140]}"

    so, sr = api(origin, "/status"), api(replica, "/status")
    eq("version", so.get("version"), sr.get("version"))
    eq("protection_enabled", so.get("protection_enabled"), sr.get("protection_enabled"))
    eq("running", so.get("running"), sr.get("running"))

    eq("dns_config", strip(api(origin, "/dns_info"), DNS_INFO_SKIP), strip(api(replica, "/dns_info"), DNS_INFO_SKIP))
    fo, fr = api(origin, "/filtering/status"), api(replica, "/filtering/status")
    eq("custom_rules", fo.get("user_rules"), fr.get("user_rules"))
    eq("filtering_enabled", (fo.get("enabled"), fo.get("interval")), (fr.get("enabled"), fr.get("interval")))
    lo, lr = lists(fo), lists(fr)
    eq("filter_lists", {k: v[:2] for k, v in lo.items()}, {k: v[:2] for k, v in lr.items()})
    drift = {}
    for k in set(lo) & set(lr):
        a, b = lo[k][2], lr[k][2]
        if max(a, b) and abs(a - b) / max(a, b) > TOLERANCE:
            drift[k[1].rsplit("/", 1)[-1][:40]] = (a, b)
    if drift:
        first = st.setdefault("count_drift_since", time.time())
        notes.append(f"rule count drift {drift}")
        if time.time() - first > COUNT_GRACE:
            problems["rule_counts"] = f"persisting >1h: {drift}"
    else:
        st.pop("count_drift_since", None)

    co, cr = api(origin, "/clients"), api(replica, "/clients")
    eq("clients", sorted(co.get("clients") or [], key=lambda c: c["name"]), sorted(cr.get("clients") or [], key=lambda c: c["name"]))
    for ep, label in (("/rewrite/list", "rewrites"), ("/access/list", "access"), ("/safesearch/status", "safesearch"),
                      ("/blocked_services/get", "blocked_services"), ("/parental/status", "parental"),
                      ("/safebrowsing/status", "safebrowsing")):
        eq(label, api(origin, ep), api(replica, ep))

    # behaviour: real DNS answers
    probes = list(DNS_PROBES)
    td = threat_domain(origin, st)
    if td:
        probes.append(td)
    else:
        notes.append("no threat-feed probe domain found")
    for name in probes:
        eq("dns:" + name, dig(ORIGIN_DNS, name), dig(REPLICA_DNS, name))
    # behaviour: per-client policy as AGH itself evaluates it, for EVERY child device and for a normal device
    kids = child_clients()
    notes.append("child devices probed: " + ", ".join(f"{n}={ip}" for n, ip in kids.items()))
    for who, ip in kids.items():
        for name in CHILD_NAMES:
            eq(f"policy:{who}:{name}", check_host(origin, name, ip), check_host(replica, name, ip))
    for name in NORMAL_NAMES + ([td] if td else []):
        eq(f"policy:normal:{name}", check_host(origin, name, NORMAL_CLIENT), check_host(replica, name, NORMAL_CLIENT))
    save_state(st)
    return problems, notes


def last_good_sync():
    """Epoch seconds of the newest SUCCESSFUL sync pass, from adguard-sync's own log (a failing pass logs 'Sync done' at
    level ERROR, a good one at INFO), or None. Independent of the content comparison and of the replica's API."""
    p = subprocess.run(["docker", "logs", "--since", "3h", "adguard-sync"], capture_output=True, text=True)
    newest = None
    for line in (p.stdout + p.stderr).splitlines():
        parts = line.split("\t")
        if len(parts) > 3 and parts[1] == "INFO" and "Sync done" in line:
            try:
                newest = datetime.datetime.strptime(parts[0][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                pass
    return newest


def sync_stamp_problem():
    t = last_good_sync()
    if t is None:
        return "no successful sync pass in the last 3 h (adguard-sync log)"
    age = time.time() - t
    if age > STALE_SECONDS:
        return f"last successful sync pass was {int(age // 60)} min ago (limit {STALE_SECONDS // 60} min)"
    return None


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(st, open(STATE, "w"))


def main():
    """Options for tests: --dry-run (compare and print only: no state change, no message), --inject NAME (adds a fake
    mismatch named NAME, e.g. to see what a mismatching run prints or to exercise the alert path with WATCH_TEST=1)."""
    dry = "--dry-run" in sys.argv
    inject = sys.argv[sys.argv.index("--inject") + 1] if "--inject" in sys.argv else None
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        problems, notes = compare()
    except Exception as e:  # noqa: BLE001 - an unreachable instance is itself a parity failure
        problems, notes = {"unreachable": f"{type(e).__name__}: {str(e)[:160]}"}, []
    stale = sync_stamp_problem()               # checked on its own: it must fire even when the replica API is unreachable
    if stale:
        problems["sync_stamp"] = stale
    if inject:
        problems[inject] = "injected for a test (origin=('Filtered', ['||*^$client=x']) replica=('NotFilteredNotFound', []))"
    if dry:
        for k, v in problems.items():
            print(f"MISMATCH {k}: {v}  [dry run: nothing sent, nothing stored]")
        for n in notes:
            print("note:", n)
        print("dry run complete:", "mismatches found" if problems else "parity OK")
        return 0
    st = load_state()
    streak = st.get("streak", {})
    new_streak = {k: streak.get(k, 0) + 1 for k in problems}
    st["streak"] = new_streak
    alerted = set(st.get("alerted", []))
    firing = {k for k, n in new_streak.items() if n >= CONSECUTIVE}
    if firing:
        # Through watchnotify (Task 24): delivery is logged and retried, a still-open mismatch gets a reminder every 6 h,
        # and a NEW probe joining the set is sent at once (force).
        lines = [f"- {k}: {problems[k]}" for k in sorted(firing)[:8]]
        watchnotify.alert("adguard-parity",
                          "minisserver adguard-parity: the replica (piserver) DIFFERS from the origin in %d place(s):\n%s%s" %
                          (len(firing), "\n".join(lines), "\n..." if len(firing) > 8 else ""),
                          force=bool(firing - alerted))
    else:
        watchnotify.clear("adguard-parity", "minisserver adguard-parity: origin and replica are identical again.")
    st["alerted"] = sorted(firing)
    save_state(st)
    for k, v in problems.items():
        print(f"MISMATCH {k}: {v} (run {new_streak[k]})")
    for n in notes:
        print("note:", n)
    if not problems:
        today = datetime.date.today().isoformat()
        last = open(OK_LOG).read().strip().splitlines()[-1] if os.path.exists(OK_LOG) and os.path.getsize(OK_LOG) else ""
        if not last.startswith(today):
            with open(OK_LOG, "a") as f:
                f.write(f"{today} parity OK ({datetime.datetime.now().strftime('%H:%M')})\n")
        print("parity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
