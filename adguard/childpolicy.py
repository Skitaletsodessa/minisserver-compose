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
import re
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


# A domain name that is safe to place inside a filtering rule: letters, digits, hyphens and
# dots only, at least two labels. Nothing that could smuggle in a rule modifier ($ ^ | / etc).
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
EXTRA = {"allow": "allow-extra.txt", "deny": "deny-extra.txt"}
EXTRA_HEADER = [
    "# Managed from the admin page (child-ui). One domain per line; subdomains are included.",
    "# allow-extra: always allowed. deny-extra: always blocked, even when everything is opened.",
]


def read_optional(name):
    return read_list(name) if (POLICY / name).exists() else []


def extra_list(kind):
    return read_optional(EXTRA[kind])


def _write_extra(kind, domains):
    (POLICY / EXTRA[kind]).write_text("\n".join(EXTRA_HEADER + sorted(set(domains))) + "\n", encoding="utf-8")


def extra_change(kind, domain, add):
    """Add/remove one domain in allow-extra.txt or deny-extra.txt. Adding to one list
    removes it from the other (a domain cannot be both)."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.match(domain):
        raise ValueError(f"not a valid domain: {domain!r}")
    other = "deny" if kind == "allow" else "allow"
    mine = [d for d in extra_list(kind) if d != domain]
    if add:
        mine.append(domain)
        _write_extra(other, [d for d in extra_list(other) if d != domain])
    _write_extra(kind, mine)


def is_under(domain, parents):
    return any(domain == p or domain.endswith("." + p) for p in parents)

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
    extra_allow = set(extra_list("allow"))
    deny = sorted(set(extra_list("deny")))
    lines = [BEGIN]
    for ip, _name in clients:
        targets = {e["target"] for e in active_overrides if e["client"] == ip}
        # "Block always" holds even while everything is opened: that is the case where it matters.
        deny_rules = [f"||{d}^$client={ip}" for d in deny]
        if "all" in targets:
            lines += deny_rules  # no catch-all for this device while the exception lasts
            continue
        allowed = set(base) | extra_allow
        if not sunday or "maps" in targets:
            allowed |= maps
        for t in GATED:
            if in_window or t in targets:
                allowed |= gated[t]
        allowed = {d for d in allowed if not is_under(d, deny)}
        lines.append(f"||*^$client={ip}")
        lines += deny_rules
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


SYNC_TRIGGER = Path(__file__).resolve().parent / "sync-trigger.py"


def trigger_sync():
    """Task 21: tell adguard-sync to push the change to the replica NOW instead of at the next minute tick,
    so the two resolvers disagree for seconds, not up to a minute, at the moments the policy changes (window
    boundaries, expiring exceptions, UI clicks). Detached (a pass takes ~10 s and the caller holds a lock);
    best effort, the 1-minute cron is the net."""
    try:
        import subprocess
        subprocess.Popen([sys.executable, str(SYNC_TRIGGER)], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 - never let the sync trigger break the policy job
        print(f"trigger_sync: {type(e).__name__}: {e}", file=sys.stderr)


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
        if notes:
            trigger_sync()
        return notes


def _record_result(ok, detail=""):
    """5 consecutive real failures -> an alert through watchnotify (first message, a REMINDER every 6 h while it lasts,
    a recovery message with the duration). A wedged policy job is dangerous (a time window could stay open past 19:00),
    so it must not be silent - but a down AGH is adguard-watch's alert, not this one."""
    sys.path.insert(0, "/srv/compose/_system/lib")
    import watchnotify  # noqa: E402  (Task 24)
    n = int(ERR_FILE.read_text()) if ERR_FILE.exists() else 0
    if ok:
        if n >= 5:
            watchnotify.clear("child-policy-job", "minisserver child-policy: the DNS policy job works again.")
        ERR_FILE.write_text("0")
        return
    n += 1
    ERR_FILE.write_text(str(n))
    if n >= 5:
        watchnotify.alert("child-policy-job",
                          f"minisserver child-policy: the children's DNS policy job has failed {n} times in a row "
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
