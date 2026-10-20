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
GUARDRAIL_KEYS = ("max_on_minutes", "max_level", "min_gap_minutes", "allowed_from", "allowed_to", "max_triggers_per_day",
                  "max_open_minutes_per_day", "max_exception_minutes", "exception_latest_end", "all_latest_end")      # the last four: child policy (Task 29)
FLAG_KEYS = ("household_editable", "guardrails_proposed")
CHILD_TARGETS = ("youtube", "roblox", "maps")      # what a child-policy window opens (the policy job's GATED services + the weekday Maps list)
GATED_TARGETS = ("youtube", "roblox")                # the services a window/exception opens that the guardrails bound
EXCEPTION_TARGETS = CHILD_TARGETS + ("all",)         # "all" = lift the catch-all for the device
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
    intval("max_open_minutes_per_day", 1, 1440)
    intval("max_exception_minutes", 1, 1440)
    for k in ("allowed_from", "allowed_to", "exception_latest_end", "all_latest_end"):
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
        if kind == "child_window":                  # the child guardrails bound only what opens YouTube/Roblox: checked below, per day
            good.append(normalise_rule({**r, "end": r.get("end")}))
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
        grps = (r.get("variables") or {}).get("opens", "").split(",") if kind == "child_window" else [""]
        for grp in grps:
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
    if kind == "child_window" and any(k in g for k in ("allowed_from", "allowed_to", "max_open_minutes_per_day")):
        # Task 29: what the windows may open. Only the gated services (YouTube, Roblox) are bounded; the Maps weekday list is not an "open window" in this sense.
        lo = parse_hhmm(g["allowed_from"]) if "allowed_from" in g else 0
        hi = parse_hhmm(g["allowed_to"]) if "allowed_to" in g else 1440
        per_day = {}
        for r in good:
            if not set(r["variables"]["opens"].split(",")) & set(GATED_TARGETS):
                continue
            s_, e_ = parse_hhmm(r["start"]), parse_hhmm(r["end"], allow_24=True)
            if s_ < lo:
                probs.append(problem("allowed_range", "rule '%s'.start" % r["id"], "%s is before the allowed range start %s" % (r["start"], fmt_hhmm(lo))))
            if e_ > hi:
                probs.append(problem("allowed_range", "rule '%s'.end" % r["id"], "%s is after the allowed range end %s" % (r["end"], fmt_hhmm(hi))))
            for d in r["days"]:
                per_day.setdefault(d, []).append((s_, e_))
        mx = g.get("max_open_minutes_per_day")
        if mx is not None:
            for d, iv in sorted(per_day.items()):
                total, cur_s, cur_e = 0, None, None
                for s_, e_ in sorted(iv):
                    if cur_e is None or s_ > cur_e:
                        total += (cur_e - cur_s) if cur_e is not None else 0
                        cur_s, cur_e = s_, e_
                    else:
                        cur_e = max(cur_e, e_)
                total += (cur_e - cur_s) if cur_e is not None else 0
                if total > mx:
                    probs.append(problem("max_open", "rules", "on weekday %d YouTube/Roblox would be open %d min, the guardrail max_open_minutes_per_day is %d" % (d, total, mx)))
                    break
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


# ── timed runs (Task 29 Part 1): "run for N minutes" ─────────────────────────
# A run is a bounded, audited, explicit exception: the plug is switched ON now (by the gateway, after the run is accepted AND armed in HA) and
# Home Assistant switches it OFF at `ends_at` by itself (generated automation, see render.py). Guardrails apply exactly as to a schedule:
# the run lies inside the allowed time-of-day range, never exceeds max_on_minutes counted from the REAL start including every extension,
# keeps min_gap_minutes after the previous run, and never overlaps a scheduled window of the same plug (a run is for the gaps).
RUN_GRACE_S = 20 * 60           # the generated end-of-run automation stays armed this long after ends_at (HA outage across the end), then it expires
RUN_MIN_REMAINING_S = 90        # an extension needs at least this much time left: the new end must be armed in HA before the old end passes


def ceil_minute(t):
    """The next whole minute at or after t (aware datetime). Runs end on a minute boundary: HA's time_pattern fires there."""
    t = t.replace(microsecond=0)
    return t if t.second == 0 else (t + dt.timedelta(seconds=60 - t.second))


def _window_overlaps(entity, rules, skips, start, end):
    """The scheduled windows (on their local dates, skipped dates removed) that intersect [start, end)."""
    out = []
    for d in {start.astimezone(TZ).date(), end.astimezone(TZ).date()}:
        if d.isoformat() in skips:
            continue
        for r in rules:
            if d.isoweekday() not in r["days"]:
                continue
            ws = dt.datetime(d.year, d.month, d.day, parse_hhmm(r["start"]) // 60, parse_hhmm(r["start"]) % 60, tzinfo=TZ)
            we = dt.datetime(d.year, d.month, d.day, parse_hhmm(r["end"]) // 60, parse_hhmm(r["end"]) % 60, tzinfo=TZ)
            if ws < end and start < we:
                out.append("%s-%s" % (r["start"], r["end"]))
    return out


def validate_run(entity, rules, skips, minutes, started_at, previous_end=None, real_start=None):
    """Checks a new run (real_start None) or an extension (real_start = when the run really started; started_at = the OLD end, minutes = the extra).
    Returns (ends_at, problems). Never clamps: a run that does not fit is rejected with the reason."""
    probs = []
    g = entity.get("guardrails", {})
    if entity["kind"] != "plug":
        return None, [problem("kind", "entity", "timed runs apply to plugs only")]
    if not entity.get("managed"):
        probs.append(problem("not_managed", "entity", "the plug is not managed by the schedule service: its end could not be enforced in Home Assistant"))
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1 or minutes > 1440:
        return None, probs + [problem("range", "minutes", "minutes must be a whole number of minutes, 1 or more")]
    extending = real_start is not None
    first = real_start if extending else started_at
    ends_at = ceil_minute(started_at + dt.timedelta(minutes=minutes))
    total = (ends_at - first).total_seconds() / 60.0
    mx = g.get("max_on_minutes")
    if mx is not None and total > mx + 1e-9:
        probs.append(problem("max_on", "minutes", ("the run would last %d min in total counted from its real start, the guardrail max_on_minutes is %d" if extending
                                                    else "%d min run (ending on a whole minute) exceeds the guardrail max_on_minutes %d") % (round(total), mx)))
    lo = parse_hhmm(g["allowed_from"]) if "allowed_from" in g else None
    hi = parse_hhmm(g["allowed_to"]) if "allowed_to" in g else None
    ls, le = first.astimezone(TZ), ends_at.astimezone(TZ)
    if ls.date() != le.date():
        probs.append(problem("allowed_range", "minutes", "a run must end on the day it started"))
    else:
        sm, em = ls.hour * 60 + ls.minute, le.hour * 60 + le.minute
        if lo is not None and sm < lo:
            probs.append(problem("allowed_range", "start", "%s is before the allowed range start %s" % (fmt_hhmm(sm), g["allowed_from"])))
        if hi is not None and em > hi:
            probs.append(problem("allowed_range", "minutes", "the run would end at %s, after the allowed range end %s" % (fmt_hhmm(em), g["allowed_to"])))
        if sm in DST_FORBIDDEN or em in DST_FORBIDDEN:
            probs.append(problem("dst_ambiguous", "minutes", "a run may not start or end inside 02:00-03:59"))
    if not extending and previous_end is not None and "min_gap_minutes" in g:
        gap = (started_at - previous_end).total_seconds() / 60.0
        if gap < g["min_gap_minutes"]:
            probs.append(problem("min_gap", "start", "the previous run ended %d min ago, the guardrail min_gap_minutes is %d" % (max(0, int(gap)), g["min_gap_minutes"])))
    over = _window_overlaps(entity, rules, skips, first, ends_at)
    if over:
        probs.append(problem("window_overlap", "minutes", "the run %s-%s overlaps the scheduled window %s; start after it or shorten the run"
                             % (fmt_hhmm(ls.hour * 60 + ls.minute), fmt_hhmm(le.hour * 60 + le.minute), ", ".join(sorted(set(over))))))
    return (None if probs else ends_at), probs


# ── the children's policy (Task 29 Part 2): exceptions, open intervals, status ───────────────────────────────────────────
def validate_child_exception(entity, target, minutes, until_eod, now):
    """A time-boxed exception of one child device. Returns (expires_at aware datetime, problems). Rejected with reasons, never clamped.
    minutes XOR until_end_of_day; the length and the latest end come from the device's guardrails ('all' has its own latest end); an exception never
    crosses midnight."""
    probs = []
    g = entity.get("guardrails", {})
    if target not in EXCEPTION_TARGETS:
        return None, [problem("target", "target", "target must be one of %s" % ", ".join(EXCEPTION_TARGETS))]
    if until_eod and minutes is not None:
        return None, [problem("both", "minutes", "give either minutes or until_end_of_day, not both")]
    if not until_eod and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1):
        return None, [problem("range", "minutes", "minutes must be a whole number of minutes, 1 or more")]
    now = now.astimezone(TZ).replace(microsecond=0)
    latest_key = "all_latest_end" if target == "all" else "exception_latest_end"
    latest = parse_hhmm(g[latest_key]) if latest_key in g else 1439
    latest_dt = now.replace(hour=latest // 60, minute=latest % 60, second=0)
    if until_eod:
        end = latest_dt
        if end <= now + dt.timedelta(seconds=60):
            return None, [problem("too_late", "until_end_of_day", "it is too late today: the latest end for this exception is %s" % fmt_hhmm(latest))]
        return end, []
    end = now + dt.timedelta(minutes=minutes)
    mx = g.get("max_exception_minutes")
    if mx is not None and minutes > mx:
        probs.append(problem("max_exception", "minutes", "an exception may last at most %d min, you asked for %d" % (mx, minutes)))
    if end.date() != now.date():
        probs.append(problem("midnight", "minutes", "an exception must end on the day it starts"))
    elif end > latest_dt:
        probs.append(problem("latest_end", "minutes", "it would end at %s, the latest end for this exception is %s" % (end.strftime("%H:%M"), fmt_hhmm(latest))))
    return (None if probs else end), probs


def child_open_intervals(rules, start, end):
    """The windows over [start, end): list of (from, to, [targets]) as aware datetimes, sorted. Exceptions are not included (they are runtime)."""
    out = []
    d = start.astimezone(TZ).date() - dt.timedelta(days=1)
    last = end.astimezone(TZ).date()
    while d <= last:
        for r in rules:
            if d.isoweekday() not in r["days"]:
                continue
            s = parse_hhmm(r["start"])
            e = parse_hhmm(r["end"], allow_24=True)
            f = dt.datetime(d.year, d.month, d.day, tzinfo=TZ) + dt.timedelta(minutes=s)
            t = dt.datetime(d.year, d.month, d.day, tzinfo=TZ) + dt.timedelta(minutes=e)
            if t > start and f < end:
                out.append((max(f, start), min(t, end), sorted(r["variables"]["opens"].split(","))))
        d += dt.timedelta(days=1)
    return sorted(out, key=lambda x: (x[0], x[1], x[2]))


def child_status(rules, exceptions, now):
    """State of one child device at `now` from its windows and its ACTIVE exceptions [{target, expires_at}]: {state, open_services, open_until, opens_at}.
    open = YouTube and Roblox (or everything) open; restricted = exactly one of them; closed = only the always-on baseline."""
    now = now.astimezone(TZ)
    opened, ends = set(), []
    for f, t, targets in child_open_intervals(rules, now, now + dt.timedelta(days=2)):          # a span long enough that the end of the current window is not clipped
        if f <= now < t:
            opened |= set(targets)
            if set(targets) & set(GATED_TARGETS):
                ends.append(t)
    for x in exceptions:
        exp = dt.datetime.fromisoformat(x["expires_at"])
        if exp > now:
            opened |= {x["target"]}
            if x["target"] in GATED_TARGETS or x["target"] == "all":
                ends.append(exp)
    gated = set(GATED_TARGETS) if "all" in opened else opened & set(GATED_TARGETS)
    state = "open" if gated == set(GATED_TARGETS) else ("restricted" if gated else "closed")
    opens_at = None
    if state != "open":
        for f, t, targets in child_open_intervals(rules, now, now + dt.timedelta(days=8)):
            if f > now and set(targets) & set(GATED_TARGETS):
                opens_at = f.isoformat(timespec="seconds")
                break
    return {"state": state, "open_services": sorted(opened), "open_until": (max(ends).isoformat(timespec="seconds") if ends and state != "closed" else None),
            "opens_at": opens_at}
