"""Child-policy renderer (Task 27 Phase E, READ-ONLY): turns the modelled policy into the AdGuard user_rules block, exactly as
adguard/childpolicy.py:render_block does today. Nothing here talks to AdGuard; step (c) is the one that may cut over.

Model (what the import stores):
  devices   : [{"id", "ip", "name", "mac", "rules": [{"id","days","start","end","variables":{"opens": "youtube,roblox"}}]}]
  lists     : {"allow_always": [...], "allow_weekdays": [...], "allow_extra": [...], "deny_extra": [...]}
  always_services : ["telegram"]            (domains come from AdGuard's own service catalogue at render time)
  overrides : [{"client": ip, "target": "youtube"|"roblox"|"maps"|"all"}]   (runtime exceptions, supplied by the caller)
A rule opens its `opens` targets while now (local) is inside its day/time window; 'maps' is the Monday-Saturday allow list.
"""
import re

BEGIN = "! BEGIN child-policy (managed by apply-child-policy.py - do not edit between markers)"
END = "! END child-policy"
GATED = ("youtube", "roblox")


def is_under(domain, parents):
    return any(domain == p or domain.endswith("." + p) for p in parents)


def _minutes(s):
    return 1440 if s == "24:00" else int(s[:2]) * 60 + int(s[3:])


def open_targets(rules, now):
    """Set of targets (youtube/roblox/maps) opened by the device's rules at the local time `now`."""
    iso, t = now.isoweekday(), now.hour * 60 + now.minute
    out = set()
    for r in rules:
        if iso in r["days"] and _minutes(r["start"]) <= t < _minutes(r["end"]):
            out |= set(r["variables"]["opens"].split(","))
    return out


def render_block(policy, service_domains, now, overrides=()):
    """policy: the model above; service_domains: {"telegram": [...], "youtube": [...], "roblox": [...]}; returns the managed lines."""
    lists = policy["lists"]
    base = set(lists["allow_always"])
    for svc in policy["always_services"]:
        base |= set(service_domains[svc])
    maps = set(lists["allow_weekdays"])
    gated = {t: set(service_domains[t]) for t in GATED}
    extra_allow_global = set(lists.get("allow_extra") or [])
    deny_global = set(lists.get("deny_extra") or [])
    lines = [BEGIN]
    for dev in policy["devices"]:
        ip = dev["ip"]
        deny = sorted(deny_global | set(dev.get("deny_extra") or []))                  # Task 29: per-child lists (the house-wide extra lists are legacy and empty after the import)
        extra_allow = extra_allow_global | set(dev.get("allow_extra") or [])
        targets = {o["target"] for o in overrides if o["client"] == ip}
        deny_rules = ["||%s^$client=%s" % (d, ip) for d in deny]
        if "all" in targets:
            lines += deny_rules
            continue
        opened = open_targets(dev["rules"], now)
        allowed = set(base) | extra_allow
        if "maps" in opened or "maps" in targets:
            allowed |= maps
        for t in GATED:
            if t in opened or t in targets:
                allowed |= gated[t]
        allowed = {d for d in allowed if not is_under(d, deny)}
        lines.append("||*^$client=%s" % ip)
        lines += deny_rules
        lines += ["@@||%s^$client=%s" % (d, ip) for d in sorted(allowed)]
    lines.append(END)
    return lines


DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
LIST_NAMES = ("allow_always", "allow_weekdays", "allow_extra", "deny_extra")


def validate_lists(lists):
    """Returns (normalised lists, problems) - a domain goes into a filtering rule, so only plain domain names are accepted."""
    from .model import problem
    probs, out = [], {}
    if not isinstance(lists, dict):
        return {}, [problem("type", "lists", "lists must be an object")]
    for name in LIST_NAMES:
        v = lists.get(name, [])
        if not isinstance(v, list) or any(not isinstance(d, str) or not DOMAIN_RE.match(d) for d in v) or len(v) > 2000:
            probs.append(problem("domains", "lists." + name, "%s must be a list of plain domain names" % name))
        else:
            out[name] = sorted(set(v))
    for k in lists:
        if k not in LIST_NAMES:
            probs.append(problem("unknown_key", "lists." + k, "unknown list '%s'" % k))
    return out, probs


def validate_device(d):
    from .model import problem
    ok = (isinstance(d, dict) and isinstance(d.get("id"), str) and isinstance(d.get("name"), str) and isinstance(d.get("ip"), str)
          and IP_RE.match(d["ip"]) and (not d.get("mac") or MAC_RE.match(d["mac"])))
    if not ok:
        return [problem("device", "devices", "a device needs id, name, a valid ip and an optional valid mac: %r" % (d if not isinstance(d, dict) else d.get("id"),))]
    from .child_invariants import device_ip_problem
    why = device_ip_problem(d["ip"])
    return [problem("protected_ip", "devices", "%s: %s" % (d["id"], why))] if why else []
