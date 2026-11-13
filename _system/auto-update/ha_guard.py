#!/usr/bin/env python3
"""Task 35b-1: may Home Assistant or the gateway be recreated RIGHT NOW? Used by container-update.py before an entry flagged "guard": true is deployed.

Returns a list of reasons it must wait (empty = go). Conditions (Ivan 2026-11-13, same spirit as Task 32's HA restart rule):
  - a vacuum plan script is running (HA state `on` of the plan script of every vacuum_plan entity),
  - a schedule edge is within +-10 minutes (start or end of a plug window, a vacuum plan trigger, the end of a timed run),
  - a plug is ON outside its schedule window (a hand-started or timed run: its fail-safe OFF lives in HA) or a timed run is active.
Plugs ON inside their normal window do not block (the plant lights are ON all day; HA reconciles them on start).
HA state unreadable = blocked (we cannot prove the house is idle). Nothing here writes anything; the HA token (gateway user, read-only) is never printed.

usage (manual check): python3 ha_guard.py"""
import json
import os
import sys
import urllib.request
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    PRAGUE = ZoneInfo("Europe/Prague")
except Exception:  # noqa: BLE001
    PRAGUE = None

SNAPSHOT = os.environ.get("PORTAL_SNAPSHOT", "/srv/apps/portal/schedules/out/schedules.json")
HA_URL = os.environ.get("HA_URL", "http://127.0.0.1:8123")
HA_TOKEN_FILE = os.environ.get("HA_TOKEN_FILE", "/srv/apps/portal/gateway-secrets/ha-token")
MARGIN = 10                                                              # minutes around a schedule edge


def _hhmm(s):
    return int(s[:2]) * 60 + int(s[3:])


def _near(m, edge):
    d = abs(m - edge)
    return min(d, 1440 - d) <= MARGIN


def minute_of_day(now):
    return now.hour * 60 + now.minute


def edges_and_windows(snap, date):
    """({entity: [(start, end)]} plug windows of `date`, [(minute, label)] all edges of that day, [active timed runs as labels evaluated by the caller])."""
    iso, day = date.isoweekday(), date.isoformat()
    wins, edges = {}, []
    for e in snap["entities"]:
        if e.get("skip_dates") and day in e["skip_dates"]:
            continue
        rules = [r for r in e.get("rules", ()) if iso in r.get("days", ())]
        if e["kind"] == "plug" and e.get("ha_target"):
            wins[e["ha_target"]] = [(_hhmm(r["start"]), _hhmm(r["end"])) for r in rules]
            for r in rules:
                edges += [(_hhmm(r["start"]), "%s starts %s" % (e["id"], r["start"])), (_hhmm(r["end"]), "%s ends %s" % (e["id"], r["end"]))]
        elif e["kind"] == "vacuum_plan" and e.get("ha_target"):
            for r in rules:
                edges.append((_hhmm(r["start"]), "%s plan starts %s" % (e["id"], r["start"])))
    return wins, edges


def blockers(now, snap, states):
    """now = aware/naive local datetime (Prague), snap = schedules.json, states = {entity_id: 'on'|'off'|...} (None = unreadable)."""
    out = []
    if states is None:
        return ["HA states unreadable (cannot prove the house is idle)"]
    m = minute_of_day(now)
    wins, edges = edges_and_windows(snap, now.date())
    for minute, label in edges:
        if _near(m, minute):
            out.append("schedule edge within %d min (%s)" % (MARGIN, label))
    ts = now.timestamp()
    for e in snap["entities"]:
        if e["kind"] == "vacuum_plan" and e.get("ha_target") and states.get(e["ha_target"]) == "on":
            out.append("vacuum plan running (%s)" % e["id"])
        for run in list(e.get("runs", ())):                              # timed runs: active now, or ending/starting around now
            st, en = run["start_ts"], run.get("effective_end_ts", run["end_ts"])
            if st <= ts < en:
                out.append("timed run active (%s)" % e["id"])
            elif abs(en - ts) <= MARGIN * 60 or abs(st - ts) <= MARGIN * 60:
                out.append("timed run edge within %d min (%s)" % (MARGIN, e["id"]))
        if e["kind"] == "plug" and e.get("ha_target") and states.get(e["ha_target"]) == "on":
            if not any(s <= m < en for s, en in wins.get(e["ha_target"], ())):
                out.append("plug ON outside its schedule window (%s)" % e["id"])
    return sorted(set(out))


def read_snapshot(path=None):
    with open(path or SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("schema") != 1 or not isinstance(snap.get("entities"), list):
        raise ValueError("unexpected snapshot shape")
    return snap


def read_states(snap, url=None, token_file=None, opener=None):
    """{entity_id: state} for every plug and plan script of the snapshot, or None when HA (or the token) is not readable."""
    try:
        with open(token_file or HA_TOKEN_FILE) as f:
            token = f.read().strip()
        opener = opener or (lambda req: urllib.request.urlopen(req, timeout=10))
        res = {}
        for e in snap["entities"]:
            t = e.get("ha_target")
            if t and e["kind"] in ("plug", "vacuum_plan"):
                req = urllib.request.Request("%s/api/states/%s" % (url or HA_URL, t), headers={"Authorization": "Bearer " + token})
                res[t] = json.load(opener(req)).get("state")
        return res
    except Exception:  # noqa: BLE001 - any failure = unknown, never "idle"
        return None


def check(now=None):
    """The live check: list of reasons to wait (empty = go)."""
    now = now or (datetime.now(PRAGUE) if PRAGUE else datetime.now())
    try:
        snap = read_snapshot()
    except Exception as ex:  # noqa: BLE001
        return ["schedule snapshot unreadable (%s)" % type(ex).__name__]
    return blockers(now, snap, read_states(snap))


if __name__ == "__main__":
    r = check()
    print("GO" if not r else "WAIT: " + "; ".join(r))
    sys.exit(0 if not r else 1)
