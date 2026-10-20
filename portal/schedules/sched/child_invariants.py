"""The children's policy INVARIANTS as code (Task 29, ADR 0008 (b)). Pure, stdlib plus model.py; imported by the schedule service (which rejects a change
that would break one) AND by the host policy job (which refuses a snapshot that breaks one - the second enforcement, like the plugs' fail-safe OFF).

Invariants (parents can change windows, exceptions and per-domain allow/deny; they cannot remove any of these, through any API):
  1. every child device keeps its catch-all block (the only lift is the time-boxed `all` exception, bounded by the guardrails),
  2. Safe Search and restricted YouTube stay on for every child client (enforced by the job's `desired_client`, not data),
  3. the always-on allow list stays: Telegram (AdGuard's own service domains), weather, the Android plumbing, the generate_204 probe hosts,
  4. deny beats allow, always (including while everything is open).
"""
import re

from . import model

# Invariant 3: the always-on hosts. These are CODE: the data may add hosts (admin only), never remove these. (Content as of 2026-10-20, the 17 lines of
# adguard/child-policy/allow-always.txt: connectivity/push, weather, the Google services Family Link needs, the Lenovo tablets' generate_204 probe host.)
REQUIRED_ALWAYS = frozenset([
    "connectivitycheck.gstatic.com", "connectivitycheck.android.com", "time.android.com", "mtalk.google.com", "fcm.googleapis.com",
    "firebaseinstallations.googleapis.com", "android.apis.google.com",
    "accuweather.com", "awxcdn.com",
    "www.googleapis.com", "android.googleapis.com", "play.googleapis.com", "play-fe.googleapis.com", "deviceintegritytokens-pa.googleapis.com",
    "kidsmanagement-pa.googleapis.com",
    "www.google.com",
])
REQUIRED_SERVICES = ("telegram",)
# Telegram's own domains as far as the service can know them (the authoritative list is AdGuard's service catalogue, read by the job at render time).
TELEGRAM_CORE = ("telegram.org", "t.me", "telegram.me", "telesco.pe", "cdn-telegram.org", "telegram.dog", "tdesktop.com")

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
IP_RE = re.compile(r"^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$")


def is_under(domain, parent):
    return domain == parent or domain.endswith("." + parent)


# Invariant 5: the infrastructure is never a child device. A catch-all block `||*^$client=IP` on the router, this server or the Pi would cut the HOUSE's DNS (the
# server resolves for everything, the Pi is the replica, the router forwards for every other device). These addresses are CODE too.
PROTECTED_IPS = {"192.168.31.1": "the router", "192.168.31.2": "this server (origin DNS and every service)", "192.168.31.5": "the Pi (replica DNS)"}


def device_ip_problem(ip):
    """None when `ip` may be a child device, else the reason it may not (a malformed address, a non-private one, or house infrastructure)."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "%r is not an IP address" % (ip,)
    if a.version != 4 or not a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified:
        return "%s is not a private LAN address" % ip
    if ip in PROTECTED_IPS:
        return "%s is %s: a catch-all block on it would cut DNS for the whole house" % (ip, PROTECTED_IPS[ip])
    return None


def deny_breaks_baseline(domain):
    """A deny rule `||domain^` blocks the domain and every subdomain: it breaks invariant 3 when it equals, contains or sits under a required host."""
    for h in list(REQUIRED_ALWAYS) + list(TELEGRAM_CORE):
        if is_under(h, domain) or is_under(domain, h):
            return h
    return None


def verify_snapshot(snap):
    """Returns a list of problems (empty = the snapshot is acceptable). Never raises on bad input."""
    probs = []

    def bad(code, msg):
        probs.append({"code": code, "message": msg})
    if not isinstance(snap, dict) or snap.get("schema") != 1:
        return [{"code": "schema", "message": "unexpected snapshot shape"}]
    devices = snap.get("devices")
    if not isinstance(devices, list) or not devices:
        bad("devices", "a snapshot without any child device would remove every restriction")
        devices = []
    lists = snap.get("lists") or {}
    always = set(lists.get("allow_always") or [])
    missing = sorted(REQUIRED_ALWAYS - always)
    if missing:
        bad("always_on", "the always-on allow list lost required hosts: %s" % ", ".join(missing[:6]))
    if any(s not in (snap.get("always_services") or []) for s in REQUIRED_SERVICES):
        bad("always_services", "Telegram must stay in the always-on services")
    seen = set()
    for d in devices:
        did = d.get("id") if isinstance(d, dict) else None
        if not isinstance(d, dict) or not isinstance(did, str) or not isinstance(d.get("ip"), str) or not IP_RE.match(d["ip"]) or not isinstance(d.get("name"), str):
            bad("device", "a device needs id, name and a valid ip")
            continue
        ipp = device_ip_problem(d["ip"])
        if ipp:
            bad("protected_ip", "%s: %s" % (did, ipp))                       # invariant 5: house infrastructure is never a child device
        if did in seen or d["ip"] in {x.get("ip") for x in devices if x is not d}:
            bad("duplicate", "duplicate device id or ip: %s" % did)
        seen.add(did)
        for kind in ("allow_extra", "deny_extra"):
            for dom in d.get(kind) or []:
                if not isinstance(dom, str) or not DOMAIN_RE.match(dom):
                    bad("domain", "%s: %s holds a value that is not a plain domain name" % (did, kind))
        for dom in d.get("deny_extra") or []:
            if isinstance(dom, str) and deny_breaks_baseline(dom):
                bad("deny_baseline", "%s: a deny rule for %s would block the always-on host %s" % (did, dom, deny_breaks_baseline(dom)))
        rules, rp = model.validate_rules({"id": did, "kind": "child_window", "guardrails": d.get("guardrails") or {}}, d.get("rules") or [])
        for p in rp:
            bad("window", "%s: %s" % (did, p["message"]))
        for x in d.get("exceptions") or []:
            if not isinstance(x, dict) or x.get("target") not in model.EXCEPTION_TARGETS or not isinstance(x.get("expires_ts"), (int, float)):
                bad("exception", "%s: an exception with a bad target or expiry" % did)
    return probs
