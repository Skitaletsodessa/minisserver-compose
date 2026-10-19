#!/usr/bin/env python3
"""Coverage invariant of the migration (Task 27 Phase D): at EVERY step, each edge of the original hand-written schedule is produced
exactly once - by what is left of the hand-written files OR by the generated files, never by both, never by neither - and each plug has
exactly one reconcile mechanism.

  python tools/coverage_check.py [--days 14] [--baseline DIR] [--handwritten DIR] [--generated DIR]      (needs PyYAML: the test venv)
Exit status 0 only when the invariant holds.
"""
import os
import re
import sys
import argparse
import collections
import datetime as dt
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sched import model     # noqa: E402
import parity_edges as pe   # noqa: E402


def counted_edges(autos, days, start, want_id):
    c = collections.Counter()
    for a in autos:
        if not want_id(a.get("id", "")):
            continue
        for tr in a.get("trigger", []):
            if tr.get("platform") != "time":
                continue
            hh, mm, ss = (int(x) for x in tr["at"].split(":"))
            for d in range(days):
                date = start + dt.timedelta(days=d)
                if not pe.conditions_pass(a, date):
                    continue
                when = dt.datetime(date.year, date.month, date.day, hh, mm, ss, tzinfo=model.TZ).isoformat()
                for act in pe.edge_actions(a):
                    c[(when,) + act] += 1
    return c


def reconcile_entities(scripts, prefix):
    out = set()
    for key, sc in scripts.items():
        if key.startswith(prefix) and key not in ("reconcile_all_plugs", "sch_reconcile_all", "sch_fingerprint"):
            out |= set(re.findall(r"switch\.(?!turn_on|turn_off|toggle)[a-z0-9_]+", yaml.safe_dump(sc)))
    return out


def check(baseline_dir, hand_dir, gen_dir, days=14, start=None):
    start = start or dt.datetime.now(model.TZ).date()
    base_a = pe.load_ha(os.path.join(baseline_dir, "automations.yaml"))
    base_s = pe.load_ha(os.path.join(baseline_dir, "scripts.yaml"))
    hand_a = pe.load_ha(os.path.join(hand_dir, "automations.yaml"))
    hand_s = pe.load_ha(os.path.join(hand_dir, "scripts.yaml"))
    gen_a = pe.load_dir_list(os.path.join(gen_dir, "automations")) if os.path.isdir(os.path.join(gen_dir, "automations")) else []
    gen_s = pe.load_dir_named(os.path.join(gen_dir, "scripts")) if os.path.isdir(os.path.join(gen_dir, "scripts")) else {}

    want = counted_edges(base_a, days, start, lambda i: i.startswith("sched_"))
    have = counted_edges(hand_a, days, start, lambda i: i.startswith("sched_"))
    have.update(counted_edges(gen_a, days, start, lambda i: i.startswith("sch_") and not i.endswith(("_failsafe", "_reconnect"))))
    problems = []
    for e in sorted(set(want) | set(have)):
        if want.get(e, 0) != have.get(e, 0):
            problems.append("edge %s %s %s: baseline %d, now %d" % (e[0], e[1], e[2], want.get(e, 0), have.get(e, 0)))
    ent_old = reconcile_entities(base_s, "reconcile_")
    in_hand, in_gen = reconcile_entities(hand_s, "reconcile_"), reconcile_entities(gen_s, "sch_reconcile_")
    for e in sorted(ent_old | in_hand | in_gen):
        n = (e in in_hand) + (e in in_gen)
        if n != 1:
            problems.append("reconcile for %s: handled by %d mechanisms (hand-written %s, generated %s)" % (e, n, e in in_hand, e in in_gen))
    # reconnect automations: one per plug, from exactly one side
    recon = collections.Counter()
    for a in hand_a + gen_a:
        for tr in a.get("trigger", []):
            if tr.get("platform") == "state" and tr.get("from") == "unavailable" and a.get("id", "").startswith(("reconcile_", "sch_")):
                recon[tr["entity_id"]] += 1
    for e in sorted(ent_old):
        if recon[e] != 1:
            problems.append("reconnect trigger for %s: %d automations" % (e, recon[e]))
    return {"edges_baseline": sum(want.values()), "edges_now": sum(have.values()), "problems": problems,
            "handwritten_entities": sorted(in_hand), "generated_entities": sorted(in_gen)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--baseline", default="/srv/compose/portal/schedules/baseline")
    ap.add_argument("--handwritten", default="/srv/compose/homeassistant")
    ap.add_argument("--generated", default="/srv/compose/homeassistant/generated")
    a = ap.parse_args()
    r = check(a.baseline, a.handwritten, a.generated, a.days)
    print("coverage over %d days: baseline %d edges, hand-written + generated now %d" % (a.days, r["edges_baseline"], r["edges_now"]))
    print("reconcile: hand-written for %s | generated for %s" % (", ".join(e[7:] for e in r["handwritten_entities"]) or "-",
                                                              ", ".join(e[7:] for e in r["generated_entities"]) or "-"))
    for p in r["problems"][:20]:
        print("  PROBLEM:", p)
    print("coverage:", "EXACTLY ONCE EVERYWHERE" if not r["problems"] else "BROKEN (%d problems)" % len(r["problems"]))
    return 0 if not r["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
