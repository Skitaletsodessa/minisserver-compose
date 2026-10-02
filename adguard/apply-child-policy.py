#!/usr/bin/env python3
"""Task 20: applies the children's DNS policy to AdGuard Home through its API.

Policy (Ivan, 2026-10-02), per child device: everything blocked except an allow-list
(Telegram, weather, Google Maps, plus the Android plumbing the phone needs to work at
all). YouTube and Roblox are open 16:00-19:00 Mon-Sat only. Sundays: only Telegram and
weather, all day (so Maps is dropped on Sundays too).

How it maps onto AdGuard Home - and what the experiments showed (2026-10-02):
  - "Block all except": a per-client catch-all `||*^$client=IP` plus `@@||domain^$client=IP`
    exceptions. Verified: exceptions override the catch-all, scoped to that client only.
  - AGH's own blocked-service schedule is NOT used. Tested: an allow rule beats a service
    block in every case (inside and outside the pause window), so with a catch-all in
    play the only way to open something for a time window is to add/remove its allow
    rule. That is what this script does, so it must run at the window boundaries.
  - Only the block between the BEGIN/END markers in user_rules is managed; rules Ivan adds
    by hand outside it are preserved.

Idempotent: safe to run any time, changes AGH only when the rendered result differs.
Run by adguard-policy.timer at 00:00, 16:00, 19:00 and every 10 minutes as self-heal.
Policy files live in child-policy/ next to this script. The admin password is read from
/home/skit/adguard-admin-password.txt (if that password is changed, update that file).

Limit that DNS cannot fix: closing a domain stops NEW lookups; a stream already open
keeps playing until it ends, and the phone's DNS cache can keep names alive for minutes.

  apply-child-policy.py [--now 2026-10-02T17:00]   # --now: render for another moment (tests)
"""
import argparse
import datetime
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
POLICY = HERE / "child-policy"
API = "http://127.0.0.1:3000/control"
PASSWORD_FILE = Path("/home/skit/adguard-admin-password.txt")
TZ = ZoneInfo("Europe/Prague")
BEGIN = "! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)"
END = "! END child-policy"
GATED_SERVICES = ["youtube", "roblox"]  # domains come from AGH's own service catalogue
WINDOW = (16, 19)  # hours, Mon-Sat


def read_list(name):
    out = []
    for line in (POLICY / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


class Api:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.call("POST", "/login", {"name": "admin", "password": PASSWORD_FILE.read_text().strip()})

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=20) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip().startswith((b"{", b"[")) else raw


def service_domains(api, ids):
    doms = []
    for s in api.call("GET", "/blocked_services/all")["blocked_services"]:
        if s["id"] in ids:
            doms += [r[2:-1] for r in s["rules"] if r.startswith("||") and r.endswith("^")]
    return doms


def render_block(api, clients, now):
    sunday = now.weekday() == 6
    in_window = (not sunday) and WINDOW[0] <= now.hour < WINDOW[1]
    allowed = set(read_list("allow-always.txt")) | set(service_domains(api, ["telegram"]))
    if not sunday:
        allowed |= set(read_list("allow-weekdays.txt"))
    if in_window:
        allowed |= set(service_domains(api, GATED_SERVICES))
    lines = [BEGIN]
    for ip, _name in clients:
        lines.append(f"||*^$client={ip}")
        lines += [f"@@||{d}^$client={ip}" for d in sorted(allowed)]
    lines.append(END)
    return lines, sunday, in_window


def merge_rules(existing, block):
    out, skipping = [], False
    for r in existing:
        if r == BEGIN:
            skipping = True
        elif r == END:
            skipping = False
        elif not skipping:
            out.append(r)
    return out + block


def ensure_client(api, ip, name, mac):
    data = {
        "name": name, "ids": [ip] + ([mac] if mac else []), "tags": [], "upstreams": [],
        "use_global_settings": True, "filtering_enabled": True, "parental_enabled": False,
        # Safe Search forced; YouTube "restricted" matters during the 16-19 window.
        "safe_search": {"enabled": True, "bing": True, "duckduckgo": True, "ecosia": False,
                        "google": True, "pixabay": False, "yandex": False, "youtube": True},
        # Time gating is done by the rules above, not by AGH's service block (see docstring).
        "use_global_blocked_services": False, "blocked_services": [],
    }
    existing = api.call("GET", "/clients").get("clients") or []
    if any(c["name"] == name for c in existing):
        api.call("POST", "/clients/update", {"name": name, "data": data})
        return "updated"
    api.call("POST", "/clients/add", data)
    return "added"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="ISO local time to render for, e.g. 2026-10-04T17:00 (testing)")
    args = ap.parse_args()
    now = datetime.datetime.fromisoformat(args.now) if args.now else datetime.datetime.now(TZ)

    clients = []
    for line in read_list("clients.conf"):
        ip, name, *rest = line.split()
        clients.append((ip, name, rest[0] if rest else ""))

    try:
        api = Api()
    except urllib.error.URLError as e:
        # AGH down is adguard-watch's job to report; don't double-alert from here.
        print(f"AdGuard Home not reachable ({e.reason}); nothing applied", file=sys.stderr)
        return 0

    block, sunday, in_window = render_block(api, [(c[0], c[1]) for c in clients], now)
    current = api.call("GET", "/filtering/status").get("user_rules") or []
    merged = merge_rules(current, block)
    state = f"sunday={sunday} yt/roblox_window_open={in_window}"
    if merged != current:
        api.call("POST", "/filtering/set_rules", {"rules": merged})
        print(f"rules updated: {len(block) - 2} managed lines ({state})")
    else:
        print(f"rules already up to date ({state})")
    for ip, name, mac in clients:
        print(f"client {name} ({ip}): {ensure_client(api, ip, name, mac)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
