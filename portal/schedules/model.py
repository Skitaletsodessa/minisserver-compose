"""Schedule domain: validation, guardrails, edges, effective state. Pure functions, no I/O.

Rules of the house (ADR 2): times are local wall-clock (Europe/Prague); a window lies inside one day;
times 02:00-03:59 do not exist or exist twice on DST nights and are rejected; windows of one entity may not
overlap; a window is never longer than max_on_minutes; guardrails REJECT, they never clamp.
"""
import re
import datetime as dt
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Prague"
TZ = ZoneInfo(TZ_NAME)

ENTITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
RULE_ID_RE = re.compile(r"^[a-z0-9_]{1,24}$")
VAR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
HA_TARGET_RE = re.compile(r"^(switch|script)\.[a-z0-9_]+$")

KINDS = ("plug", "vacuum_plan", "child_window", "dimmer")
RENDERABLE_KINDS = ("plug", "vacuum_plan")
RULE_KEYS = {"id", "days", "start", "end", "level", "variables"}
GUARDRAIL_KEYS = ("max_on_minutes", "max_level", "min_gap_minutes", "allowed_from", "allowed_to", "max_triggers_per_day")
FLAG_KEYS = ("household_editable",)
CHILD_TARGETS = ("youtube", "roblox", "maps")      # what a child-policy window opens (the policy job's GATED services + the weekday Maps list)
MAX_RULES = 12
DST_FORBIDDEN = range(2 * 60, 4 * 60)          # 02:00 .. 03:59
WEEK = 7 * 1440


class ValidationError(Exception):
    """problems: list of {"code", "field", "message"}; every problem is reported, not just the first."""
    def __init__(self, problems):
        super().__init__("; ".join(p["message"] for p in problems))
        self.problems = problems


def problem(code, field, message):
    return {"code": code, "field": field, "message": message}


# ── time helpers ──────────────────────────────────────────────────────────────

def parse_hhmm(s, allow_24=False):
    if not isinstance(s, str):
        return None
    if allow_24 and s == "24:00":              # end of day, for child-policy windows only
        return 1440
    m = TIME_RE.match(s)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def fmt_hhmm(minutes):
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def parse_date(s):
    try:
        return dt.date.fromisoformat(s) if isinstance(s, str) and len(s) == 10 else None
    except ValueError:
        return None


# ── guardrails and flags ──────────────────────────────────────────────────────

def validate_guardrails(kind, g):
    """Returns (normalised guardrails, problems)."""
    probs = []
    if not isinstance(g, dict):
        return {}, [problem("type", "guardrails", "guardrails must be an object")]
    out = {}
    for k in g:
        if k not in GUARDRAIL_KEYS:
            probs.append(problem("unknown_key", "guardrails." + k, "unknown guardrail '%s'" % k))
    def intval(k, lo, hi):
        if k not in g or g[k] is None:
            return
        v = g[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            probs.append(problem("range", "guardrails." + k, "%s must be an integer %d..%d" % (k, lo, hi)))
        else:
            out[k] = v
    intval("max_on_minutes", 1, 1440)
    intval("max_level", 1, 100)
    intval("min_gap_minutes", 0, 1440)
    intval("max_triggers_per_day", 1, 24)
    for k in ("allowed_from", "allowed_to"):
        if k in g and g[k] is not None:
            v = parse_hhmm(g[k])
            if v is None:
                probs.append(problem("format", "guardrails." + k, "%s must be HH:MM" % k))
            else:
                out[k] = g[k]
    if "allowed_from" in out and "allowed_to" in out and parse_hhmm(out["allowed_from"]) >= parse_hhmm(out["allowed_to"]):
        probs.append(problem("range", "guardrails.allowed_from", "allowed_from must be earlier than allowed_to"))
    if kind == "plug" and "max_on_minutes" not in out and not probs:
        probs.append(problem("required", "guardrails.max_on_minutes", "a plug needs max_on_minutes (the fail-safe OFF is generated from it)"))
    if kind == "plug" and not ("allowed_from" in out and "allowed_to" in out) and not probs:
        probs.append(problem("required", "guardrails.allowed_from", "a plug needs allowed_from and allowed_to"))
    if kind == "vacuum_plan" and not ("allowed_from" in out and "allowed_to" in out) and not probs:
        probs.append(problem("required", "guardrails.allowed_from", "a vacuum plan needs allowed_from and allowed_to"))
    return out, probs


def validate_flags(f):
    if not isinstance(f, dict):
        return {}, [problem("type", "flags", "flags must be an object")]
    probs, out = [], {}
    for k, v in f.items():
        if k not in FLAG_KEYS:
            probs.append(problem("unknown_key", "flags." + k, "unknown flag '%s'" % k))
        elif not isinstance(v, bool):
            probs.append(problem("type", "flags." + k, "%s must be true or false" % k))
        else:
            out[k] = v
    return out, probs


# ── rules ─────────────────────────────────────────────────────────────────────

def normalise_rule(r):
    out = {"id": r["id"], "days": sorted(r["days"]), "start": r["start"], "end": r.get("end"),
           "level": r.get("level"), "variables": r.get("variables")}
    return out


def validate_rules(entity, rules):
    """entity: dict with kind + guardrails. Returns (normalised rules, problems). Never modifies values to make them fit."""
    kind, g = entity["kind"], entity.get("guardrails", {})
    probs = []
    if kind not in RENDERABLE_KINDS and kind not in ("child_window", "dimmer"):
        return [], [problem("kind", "entity", "unknown entity kind '%s'" % kind)]
    if not isinstance(rules, list):
        return [], [problem("type", "rules", "rules must be a list")]
    if len(rules) > MAX_RULES:
        probs.append(problem("too_many", "rules", "at most %d rules per entity" % MAX_RULES))
    seen_ids, good = set(), []
    for i, r in enumerate(rules):
        fld = "rules[%d]" % i
        if not isinstance(r, dict):
            probs.append(problem("type", fld, "a rule must be an object"))
            continue
        for k in r:
            if k not in RULE_KEYS:
                probs.append(problem("unknown_key", "%s.%s" % (fld, k), "unknown rule field '%s'" % k))
        rid = r.get("id")
        if not isinstance(rid, str) or not RULE_ID_RE.match(rid):
            probs.append(problem("format", fld + ".id", "rule id must match [a-z0-9_]{1,24}"))
            continue
        if rid in seen_ids:
            probs.append(problem("duplicate", fld + ".id", "duplicate rule id '%s'" % rid))
            continue
        seen_ids.add(rid)
        fld = "rule '%s'" % rid
        days = r.get("days")
        if (not isinstance(days, list) or not days or len(set(days)) != len(days)
                or any(isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 7 for d in days)):
            probs.append(problem("days", fld + ".days", "days must be a non-empty list of distinct ISO weekdays 1..7"))
            continue
        s = parse_hhmm(r.get("start"))
        if s is None:
            probs.append(problem("format", fld + ".start", "start must be HH:MM"))
            continue
        if s in DST_FORBIDDEN:
            probs.append(problem("dst_ambiguous", fld + ".start", "%s is inside 02:00-03:59, which does not exist or exists twice on DST nights" % r["start"]))
            continue
        e = None
        if kind in ("plug", "child_window"):
            e = parse_hhmm(r.get("end"), allow_24=(kind == "child_window"))
            if e is None:
                probs.append(problem("format", fld + ".end", "a window needs an end, HH:MM"))
                continue
            if e in DST_FORBIDDEN:
                probs.append(problem("dst_ambiguous", fld + ".end", "%s is inside 02:00-03:59" % r["end"]))
                continue
            if e <= s:
                probs.append(problem("window", fld, "the window must end after it starts and stay inside one day (split a window across midnight into two rules)"))
                continue
            if kind == "plug" and r.get("variables"):
                probs.append(problem("not_allowed", fld + ".variables", "a plug rule takes no variables"))
                continue
            if kind == "child_window":
                opens = (r.get("variables") or {}).get("opens")
                targets = opens.split(",") if isinstance(opens, str) else []
                if not targets or any(t not in CHILD_TARGETS for t in targets):
                    probs.append(problem("variables", fld + ".variables", "a child window needs variables.opens: a comma list of %s" % ", ".join(CHILD_TARGETS)))
                    continue
        elif kind == "vacuum_plan":
            if r.get("end") is not None:
                probs.append(problem("not_allowed", fld + ".end", "a vacuum trigger has no end"))
                continue
        v = r.get("variables")
        if v is not None:
            if (not isinstance(v, dict) or len(v) > 8 or any(not isinstance(k, str) or not VAR_KEY_RE.match(k) for k in v)
                    or any(isinstance(x, (dict, list)) or x is None or (isinstance(x, str) and len(x) > 64) for x in v.values())):
                probs.append(problem("variables", fld + ".variables", "variables must be a flat object of short scalars"))
                continue
        lvl = r.get("level")
        if lvl is not None:
            if isinstance(lvl, bool) or not isinstance(lvl, int) or lvl < 1:
                probs.append(problem("range", fld + ".level", "level must be a positive integer"))
                continue
            if "max_level" not in g:
                probs.append(problem("not_allowed", fld + ".level", "this entity has no level (no max_level guardrail)"))
                continue
            if lvl > g["max_level"]:
                probs.append(problem("max_level", fld + ".level", "level %d exceeds the guardrail max_level %d" % (lvl, g["max_level"])))
                continue
        # guardrails per rule
        lo = parse_hhmm(g["allowed_from"]) if "allowed_from" in g else None
        hi = parse_hhmm(g["allowed_to"]) if "allowed_to" in g else None
        last = e if e is not None else s
        if lo is not None and s < lo:
            probs.append(problem("allowed_range", fld + ".start", "%s is before the allowed range start %s" % (r["start"], g["allowed_from"])))
            continue
        if hi is not None and last > hi:
            probs.append(problem("allowed_range", fld + (".end" if e is not None else ".start"),
                                 "%s is after the allowed range end %s" % (fmt_hhmm(last), g["allowed_to"])))
            continue
        if e is not None and "max_on_minutes" in g and e - s > g["max_on_minutes"]:
            probs.append(problem("max_on", fld, "the window is %d min, the guardrail max_on_minutes is %d" % (e - s, g["max_on_minutes"])))
            continue
        good.append(normalise_rule({**r, "end": r.get("end") if kind in ("plug", "child_window") else None}))
    # cross-rule checks (only on rules that are individually sound); child windows are independent per thing they open
    groups = {}
    for r in good:
        s_ = parse_hhmm(r["start"])
        e_ = parse_hhmm(r["end"], allow_24=True) if r["end"] is not None else s_
        grp = (r.get("variables") or {}).get("opens", "") if kind == "child_window" else ""
        for d in r["days"]:
            groups.setdefault(grp, []).append(((d - 1) * 1440 + s_, (d - 1) * 1440 + e_, r["id"]))
    gap = g.get("min_gap_minutes", 0)
    reported = set()
    for iv in groups.values():
        iv.sort()
        for i, cur in enumerate(iv):
            j = (i + 1) % len(iv)
            nxt = iv[j]
            nstart = nxt[0] + (WEEK if j <= i else 0)      # j <= i: wrapped round the week
            if nstart < cur[1]:
                key = ("overlap",) + tuple(sorted((cur[2], nxt[2])))
                if key not in reported:
                    reported.add(key)
                    probs.append(problem("overlap", "rules", "rules '%s' and '%s' overlap" % (cur[2], nxt[2])))
            elif nstart - cur[1] < gap:
                key = ("gap",) + tuple(sorted((cur[2], nxt[2])))
                if key not in reported:
                    reported.add(key)
                    probs.append(problem("min_gap", "rules", "rules '%s' and '%s' are %d min apart, the guardrail min_gap_minutes is %d"
                                         % (cur[2], nxt[2], nstart - cur[1], gap)))
    if "max_triggers_per_day" in g:
        per_day = {}
        for r in good:
            for d in r["days"]:
                per_day[d] = per_day.get(d, 0) + 1
        worst = max(per_day.values(), default=0)
        if worst > g["max_triggers_per_day"]:
            probs.append(problem("max_triggers", "rules", "%d triggers on one day, the guardrail max_triggers_per_day is %d" % (worst, g["max_triggers_per_day"])))
    return sorted(good, key=lambda r: (parse_hhmm(r["start"]), r["id"])), probs


# ── exceptions ────────────────────────────────────────────────────────────────

def validate_exception(entity, body, today):
    """Returns (normalised exception, problems). kind: skip_date {date} | override_until {until, state}."""
    probs = []
    kind = body.get("kind")
    out = {"kind": kind}
    allowed = {"kind", "entity_id", "date", "until", "state"}
    for k in body:
        if k not in allowed:
            probs.append(problem("unknown_key", k, "unknown field '%s'" % k))
    if kind == "skip_date":
        d = parse_date(body.get("date"))
        if d is None:
            probs.append(problem("format", "date", "date must be YYYY-MM-DD"))
        elif d < today:
            probs.append(problem("past", "date", "that date is already past"))
        elif d > today + dt.timedelta(days=366):
            probs.append(problem("far", "date", "a skipped date is limited to one year ahead"))
        else:
            out["date"] = d.isoformat()
    elif kind == "override_until":
        u = parse_hhmm(body.get("until"))
        st = body.get("state")
        if entity["kind"] != "plug":
            probs.append(problem("kind", "kind", "override_until applies to plugs only"))
        if u is None:
            probs.append(problem("format", "until", "until must be HH:MM (today)"))
        elif u in DST_FORBIDDEN:
            probs.append(problem("dst_ambiguous", "until", "until is inside 02:00-03:59"))
        if st not in ("on", "off"):
            probs.append(problem("value", "state", "state must be 'on' or 'off'"))
        g = entity.get("guardrails", {})
        if st == "on" and u is not None and not probs:
            hi = parse_hhmm(g["allowed_to"]) if "allowed_to" in g else 1439
            if u > hi:
                probs.append(problem("allowed_range", "until", "an override may not run past the allowed range end %s" % g.get("allowed_to")))
        if not probs:
            out["until"], out["state"] = body["until"], st
    else:
        probs.append(problem("kind", "kind", "kind must be skip_date or override_until"))
    return out, probs


# ── edges and effective state ─────────────────────────────────────────────────

def skip_dates(exceptions):
    return {e["date"] for e in exceptions if e["kind"] == "skip_date"}


def day_edges(kind, rules, date, skips=()):
    """Edges on one local date: list of (minute, action, rule id) sorted. action: on | off | trigger.
    A skipped date drops the on/trigger edges; the off edge stays (it is harmless and keeps hand-started runs bounded)."""
    iso = date.isoweekday()
    skipped = date.isoformat() in skips
    out = []
    for r in rules:
        if iso not in r["days"]:
            continue
        s = parse_hhmm(r["start"])
        if kind == "plug":
            if not skipped:
                out.append((s, "on", r["id"]))
            out.append((parse_hhmm(r["end"]), "off", r["id"]))
        elif kind == "vacuum_plan" and not skipped:
            out.append((s, "trigger", r["id"]))
    return sorted(out)


def edges_between(kind, rules, start, end, skips=()):
    """All edges with start <= local time < end (aware datetimes in TZ) as (datetime, action, rule id)."""
    out = []
    d = start.astimezone(TZ).date() - dt.timedelta(days=1)
    last = end.astimezone(TZ).date() + dt.timedelta(days=1)
    while d <= last:
        for (m, action, rid) in day_edges(kind, rules, d, skips):
            t = dt.datetime(d.year, d.month, d.day, m // 60, m % 60, tzinfo=TZ)
            if start <= t < end:
                out.append((t, action, rid))
        d += dt.timedelta(days=1)
    return sorted(out, key=lambda x: (x[0], x[1], x[2]))


def target_state(kind, rules, at, skips=()):
    """For a plug: 'on' | 'off' at the local time `at` (the rule the old reconcile scripts implemented: start <= t < end)."""
    if kind != "plug":
        return None
    at = at.astimezone(TZ)
    if at.date().isoformat() in skips:
        return "off"
    t = at.hour * 60 + at.minute
    iso = at.isoweekday()
    for r in rules:
        if iso in r["days"] and parse_hhmm(r["start"]) <= t < parse_hhmm(r["end"]):
            return "on"
    return "off"


def next_edge(kind, rules, at, skips=()):
    at = at.astimezone(TZ)
    for e in edges_between(kind, rules, at, at + dt.timedelta(days=9), skips):
        if e[0] > at:
            return e
    return None


def effective(entity, rules, exceptions, at):
    """What the schedule says at `at`: {state, source, next_edge{at, action, rule}, overrides}. Overrides are reported, not enforced here (ADR 2/Task 27: model and API only)."""
    at = at.astimezone(TZ)
    skips = skip_dates(exceptions)
    st = target_state(entity["kind"], rules, at, skips)
    src = "schedule"
    active = []
    for x in exceptions:
        if x["kind"] == "override_until" and at.date() == dt.date.fromisoformat(x["created_at"][:10]) \
                and at.hour * 60 + at.minute < parse_hhmm(x["until"]):
            active.append(x["id"])
            st, src = x["state"], "override"
    ne = next_edge(entity["kind"], rules, at, skips)
    return {"entity": entity["id"], "at": at.isoformat(), "state": st, "source": src, "active_overrides": active,
            "skipped_today": at.date().isoformat() in skips,
            "next_edge": ({"at": ne[0].isoformat(), "action": ne[1], "rule": ne[2]} if ne else None)}
