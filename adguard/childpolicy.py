"""Task 20: applies the children's DNS policy to AdGuard Home through its API.

Policy (Ivan, 2026-10-02), per child device: everything blocked except an allow-list
(Telegram, weather, Google Maps, plus the Android plumbing the phone needs to work at
all). YouTube and Roblox are open 16:00-19:00 Mon-Sat only. Sundays: only Telegram and
weather, all day (so Maps is dropped on Sundays too). On top of that, temporary
exceptions (overrides.py, set from child-ui.py / child-override) open a target for a
while; target "all" lifts the catch-all for that device.

How it maps onto AdGuard Home - and what the experiments showed (2026-10-02):
  - "Block all except": a per-client catch-all `||*^$client=IP` plus `@@||domain^$client=IP`
    exceptions. Verified: exceptions override the catch-all, scoped to that client only.
  - AGH's own blocked-service schedule is NOT used. Tested: an allow rule beats a service
    block in every case (inside and outside the pause window), so with a catch-all in
    play the only way to open something for a time window is to add/remove its allow
    rule. That is what this does, so it re-runs every minute (boundaries and expiries).
  - Only the block between the BEGIN/END markers in user_rules is managed; rules added
    by hand outside it (AGH admin UI -> Custom filtering rules) are preserved.

Idempotent and quiet: AGH is only written to when the rendered result actually differs.
Auth is HTTP Basic against AGH's API (no session is created per run). The admin password
is read from /home/skit/adguard-admin-password.txt (if it is changed, update that file).

Limit that DNS cannot fix: closing a domain stops NEW lookups; a stream already open
keeps playing until it ends, and the phone's DNS cache can keep names alive for minutes.
"""
import base64
import datetime
import fcntl
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import overrides  # noqa: E402

POLICY = HERE / "child-policy"
API = "http://127.0.0.1:3000/control"
PASSWORD_FILE = Path("/home/skit/adguard-admin-password.txt")
TZ = ZoneInfo("Europe/Prague")
BEGIN = "! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)"
END = "! END child-policy"
GATED = ["youtube", "roblox"]  # domains come from AGH's own service catalogue
WINDOW = (16, 19)  # hours, Mon-Sat
LOCK = overrides.STATE_DIR / "apply.lock"
ERR_FILE = overrides.STATE_DIR / "consecutive-errors"
TELEGRAM_ENV = Path("/srv/compose/scrutiny/.env")


def read_list(name):
    out = []
    for line in (POLICY / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def load_clients():
    clients = []
    for line in read_list("clients.conf"):
        ip, name, *rest = line.split()
        clients.append((ip, name, rest[0] if rest else ""))
    return clients


class Api:
    def __init__(self):
        tok = base64.b64encode(f"admin:{PASSWORD_FILE.read_text().strip()}".encode()).decode()
        self.headers = {"Authorization": "Basic " + tok, "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers=self.headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip().startswith((b"{", b"[")) else raw


def service_domains(api, ids):
    doms = []
    for s in api.call("GET", "/blocked_services/all")["blocked_services"]:
        if s["id"] in ids:
            doms += [r[2:-1] for r in s["rules"] if r.startswith("||") and r.endswith("^")]
    return doms


def schedule_state(now):
    sunday = now.weekday() == 6
    return sunday, (not sunday) and WINDOW[0] <= now.hour < WINDOW[1]


def render_block(api, clients, now, active_overrides):
    sunday, in_window = schedule_state(now)
    base = set(read_list("allow-always.txt")) | set(service_domains(api, ["telegram"]))
    maps = set(read_list("allow-weekdays.txt"))
    gated = {t: set(service_domains(api, [t])) for t in GATED}
    lines = [BEGIN]
    for ip, _name in clients:
        targets = {e["target"] for e in active_overrides if e["client"] == ip}
        if "all" in targets:
            continue  # no catch-all for this device while the exception lasts
        allowed = set(base)
        if not sunday or "maps" in targets:
            allowed |= maps
        for t in GATED:
            if in_window or t in targets:
                allowed |= gated[t]
        lines.append(f"||*^$client={ip}")
        lines += [f"@@||{d}^$client={ip}" for d in sorted(allowed)]
    lines.append(END)
    return lines


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


def desired_client(ip, name, mac):
    return {
        "name": name, "ids": [ip] + ([mac] if mac else []), "tags": [], "upstreams": [],
        "use_global_settings": True, "filtering_enabled": True, "parental_enabled": False,
        # Safe Search forced; YouTube "restricted" matters during the 16-19 window.
        "safe_search": {"enabled": True, "bing": True, "duckduckgo": True, "ecosia": False,
                        "google": True, "pixabay": False, "yandex": False, "youtube": True},
        # Time gating is done by the rules above, not by AGH's service block (see docstring).
        "use_global_blocked_services": False, "blocked_services": [],
    }


def client_matches(existing, want):
    for k in ("use_global_settings", "filtering_enabled", "parental_enabled",
              "use_global_blocked_services", "blocked_services"):
        if existing.get(k) != want[k]:
            return False
    # AGH stores MAC addresses lower-cased; compare case-insensitively or this "differs" forever
    if sorted(i.lower() for i in existing.get("ids") or []) != sorted(i.lower() for i in want["ids"]):
        return False
    ss = existing.get("safe_search") or {}
    return all(ss.get(k) == v for k, v in want["safe_search"].items())


def ensure_client(api, ip, name, mac):
    want = desired_client(ip, name, mac)
    existing = {c["name"]: c for c in (api.call("GET", "/clients").get("clients") or [])}
    if name not in existing:
        api.call("POST", "/clients/add", want)
        return "added"
    if client_matches(existing[name], want):
        return None
    api.call("POST", "/clients/update", {"name": name, "data": want})
    return "updated"


def run(now=None):
    """Applies the policy. Returns a list of human-readable change notes (empty = nothing
    changed). Raises URLError if AGH is down, HTTPError on API/auth failures."""
    now = now or datetime.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    overrides.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        clients = load_clients()
        api = Api()
        api.call("GET", "/status")
        notes = []
        block = render_block(api, [(c[0], c[1]) for c in clients], now, overrides.active(now))
        current = api.call("GET", "/filtering/status").get("user_rules") or []
        merged = merge_rules(current, block)
        if merged != current:
            api.call("POST", "/filtering/set_rules", {"rules": merged})
            sunday, in_window = schedule_state(now)
            notes.append(f"rules updated ({len(block) - 2} managed lines; sunday={sunday} "
                         f"yt/roblox_window_open={in_window})")
        for ip, name, mac in clients:
            r = ensure_client(api, ip, name, mac)
            if r:
                notes.append(f"client {name} ({ip}): {r}")
        return notes


def _telegram(text):
    env = {}
    for line in TELEGRAM_ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", data=data), timeout=15)


def _record_result(ok, detail=""):
    """5 consecutive real failures -> one Telegram message; recovery -> one more. A wedged
    policy job is dangerous (a time window could stay open past 19:00), so it must not be
    silent - but a down AGH is adguard-watch's alert, not this one."""
    n = int(ERR_FILE.read_text()) if ERR_FILE.exists() else 0
    if ok:
        if n >= 5:
            _telegram("minisserver child-policy: the DNS policy job works again.")
        ERR_FILE.write_text("0")
        return
    n += 1
    ERR_FILE.write_text(str(n))
    if n == 5:
        _telegram(f"minisserver child-policy: the children's DNS policy job has failed 5 times in a row "
                  f"({detail}). Time windows (YouTube/Roblox) may be stuck open or closed.")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="ISO local time to render for, e.g. 2026-10-04T17:00 (testing; writes to AGH)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    now = datetime.datetime.fromisoformat(args.now) if args.now else None
    try:
        notes = run(now)
    except urllib.error.HTTPError as e:
        print(f"AdGuard Home API error: {e.code} {e.reason}", file=sys.stderr)
        _record_result(False, f"HTTP {e.code}")
        return 1
    except urllib.error.URLError as e:
        print(f"AdGuard Home not reachable ({e.reason}); nothing applied", file=sys.stderr)
        return 0  # adguard-watch reports an AGH outage
    except Exception as e:  # noqa: BLE001
        print(f"policy job failed: {e!r}", file=sys.stderr)
        _record_result(False, repr(e)[:120])
        return 1
    _record_result(True)
    for n in notes:
        print(n)
    if args.verbose and not notes:
        print("up to date")
    return 0
