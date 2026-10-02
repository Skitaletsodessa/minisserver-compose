"""Browsing audit for a child device (Task 20): which domains it asked for, how often,
and whether they were allowed - read from AdGuard Home's query log (kept 7 days).

What this can and cannot show, said plainly: DNS sees DOMAIN NAMES, not URLs. HTTPS hides
the path and parameters, so "which page" is invisible here; "which site, how often, when"
is not. A domain being unknown to the block lists means nothing either way - it only
means no list has flagged it.
"""
import datetime
import time
import urllib.parse

import overrides

PAGE = 500
MAX_PAGES = 20  # 10 000 log entries at most per view
_class_cache = {}  # name -> (checked_at, label or None)
_CLASS_TTL = 3600


def _parse(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(overrides.TZ)


def collect(api, ip, hours, now=None):
    """Returns (rows, truncated). Each row: name, allowed, blocked, first, last. Counting
    uses A queries (a lookup is normally an A plus an AAAA/HTTPS; counting all would
    double it), falling back to every query for names that were only asked another way."""
    now = now or datetime.datetime.now(overrides.TZ)
    cutoff = now - datetime.timedelta(hours=hours)
    events = {}
    older, truncated = None, False
    for page in range(MAX_PAGES):
        q = f"/querylog?limit={PAGE}&search={urllib.parse.quote(ip)}"
        if older:
            q += f"&older_than={urllib.parse.quote(older)}"
        res = api.call("GET", q)
        data = res.get("data") or []
        past_cutoff = False
        for e in data:
            if e.get("client") != ip:
                continue
            t = _parse(e["time"])
            if t < cutoff:
                past_cutoff = True
                continue
            name = e["question"]["name"].lower().rstrip(".")
            events.setdefault(name, []).append(
                (t, str(e.get("reason", "")).startswith("Filtered"), e["question"].get("type", "")))
        older = res.get("oldest")
        if past_cutoff or len(data) < PAGE or not older:
            break
        if page == MAX_PAGES - 1:
            truncated = True
    rows = []
    for name, evs in events.items():
        use = [x for x in evs if x[2] == "A"] or evs
        rows.append({"name": name,
                     "allowed": sum(1 for x in use if not x[1]),
                     "blocked": sum(1 for x in use if x[1]),
                     "first": min(x[0] for x in evs), "last": max(x[0] for x in evs)})
    rows.sort(key=lambda r: r["last"], reverse=True)
    return rows, truncated


def list_label(api, name, filters):
    """Which of the house-wide block lists (ads / trackers / threats) already lists this
    domain, independent of the child's own catch-all. None = no list flags it."""
    hit = _class_cache.get(name)
    if hit and time.time() - hit[0] < _CLASS_TTL:
        return hit[1]
    label = None
    try:
        r = api.call("GET", "/filtering/check_host?name=" + urllib.parse.quote(name))
        if str(r.get("reason", "")).startswith("Filtered"):
            ids = [x.get("filter_list_id") for x in (r.get("rules") or [])]
            names = [filters.get(i) for i in ids if i]
            label = names[0] if names else "правило AdGuard"
    except Exception:  # noqa: BLE001 - a missing label must never break the page
        label = None
    _class_cache[name] = (time.time(), label)
    return label
