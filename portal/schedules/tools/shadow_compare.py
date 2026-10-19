#!/usr/bin/env python3
"""Shadow comparison (Task 27 Phase D): what Home Assistant ACTUALLY did versus what the generated schedule WOULD have done.

Reads the recorder's `automation_triggered` events (source "time") of the old hand-written `Schedule:` automations and compares them,
edge by edge, with the edges predicted by rendering the service's current data. Nothing is loaded into HA.
  python tools/shadow_compare.py --since 2026-10-16T14:30 [--until now]            (needs PyYAML: the test venv)
An edge that was predicted but did not fire is listed together with the nearest Home Assistant start (a restart explains a missed edge);
an edge that fired but was not predicted is always a real difference.
"""
import os
import sys
import json
import sqlite3
import argparse
import datetime as dt
import tempfile
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sched import model, render as renderer          # noqa: E402
import parity_edges as pe                           # noqa: E402

DB = "/srv/apps/homeassistant/home-assistant_v2.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default="now")
    ap.add_argument("--old", default="/srv/compose/homeassistant")
    args = ap.parse_args()
    since = dt.datetime.fromisoformat(args.since).replace(tzinfo=model.TZ)
    until = dt.datetime.now(model.TZ) - dt.timedelta(minutes=2) if args.until == "now" else dt.datetime.fromisoformat(args.until).replace(tzinfo=model.TZ)

    old_autos = pe.load_ha(os.path.join(args.old, "automations.yaml"))
    by_name = {a["alias"]: a for a in old_autos if a.get("id", "").startswith("sched_")}

    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    rows = conn.execute("""SELECT e.time_fired_ts, d.shared_data FROM events e JOIN event_types t ON e.event_type_id = t.event_type_id
                           LEFT JOIN event_data d ON e.data_id = d.data_id WHERE t.event_type = 'automation_triggered' AND e.time_fired_ts BETWEEN ? AND ?
                           ORDER BY e.time_fired_ts""", (since.timestamp(), until.timestamp())).fetchall()
    starts = [ts for ts, d in rows if json.loads(d).get("source") == "Home Assistant starting"]
    actual, unknown = set(), []
    for ts, d in rows:
        j = json.loads(d)
        if j.get("source") != "time" or not j["name"].startswith("Schedule:"):
            continue
        a = by_name.get(j["name"])
        if a is None:
            unknown.append(j["name"])
            continue
        when = dt.datetime.fromtimestamp(ts, model.TZ)
        # time triggers fire at hh:mm:00.xxx; normalise to the scheduled second
        sched = when.replace(microsecond=0)
        for act in pe.edge_actions(a):
            actual.add((sched.isoformat(),) + act)

    snap = json.load(open("/srv/apps/portal/schedules/out/schedules.json"))
    for e in snap["entities"]:
        e["managed"] = e["kind"] in model.RENDERABLE_KINDS
    out = renderer.render(snap)
    tmp = tempfile.mkdtemp(prefix="shadow-render-")
    for rel, text in out["files"].items():
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
    new_autos = pe.load_dir_list(os.path.join(tmp, "automations"))
    days = (until.date() - since.date()).days + 1
    predicted_all = pe.time_edges(new_autos, days, since.date(), lambda i: i.startswith("sch_") and not i.endswith(("_failsafe", "_reconnect")))
    predicted = {e for e in predicted_all if since <= dt.datetime.fromisoformat(e[0]) <= until}

    only_actual = sorted(actual - predicted)
    missed = sorted(predicted - actual)
    explained, unexplained = [], []
    for e in missed:
        t = dt.datetime.fromisoformat(e[0]).timestamp()
        near = [s for s in starts if -600 <= s - t <= 300]       # HA started within 10 min after / 5 min before the edge: it was restarting
        (explained if near else unexplained).append(e)
    print("window %s .. %s (%d days); %d old-automation firings with source=time in the recorder, %d HA starts"
          % (since.isoformat(timespec="minutes"), until.isoformat(timespec="minutes"), days, len(actual), len(starts)))
    print("predicted edges in the window: %d; actually fired: %d" % (len(predicted), len(actual)))
    print("fired but NOT predicted:", len(only_actual))
    for e in only_actual[:10]:
        print("   ", e)
    print("predicted but did not fire: %d (explained by an HA restart: %d, unexplained: %d)" % (len(missed), len(explained), len(unexplained)))
    for e in unexplained[:10]:
        print("   UNEXPLAINED", e)
    for e in explained[:10]:
        print("   restart     ", e)
    if unknown:
        print("firings of automations not found in the old file:", sorted(set(unknown)))
    ok = not only_actual and not unexplained
    print("shadow result:", "MATCH" if ok else "DIFFERENCES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
