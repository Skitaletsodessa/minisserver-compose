#!/usr/bin/env python3
"""Parity proof (Task 27 Phase D): every schedule edge of the OLD hand-written Home Assistant configuration versus the edges of the
GENERATED configuration, for N simulated days; plus the reconcile decision of both for every minute of every weekday.

  python tools/parity_edges.py [--days 14] [--old DIR] [--render DIR]       (needs PyYAML: use the test venv)

--old    directory holding the hand-written automations.yaml and scripts.yaml (default: the live git copy)
--render directory holding rendered automations/ and scripts/ (default: the service's out/render)
Edges are compared as sets of (local time, service, target, data); the reconcile templates are evaluated for all 7 x 1440 minutes.
Exit status 0 only when both comparisons are identical.
"""
import os
import re
import sys
import json
import argparse
import datetime as dt
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sched import model, render as renderer     # noqa: E402
import tempfile

class HALoader(yaml.SafeLoader):
    """Home Assistant YAML: !secret is a reference, not data."""


HALoader.add_constructor("!secret", lambda loader, node: "<secret>")


def load_ha(path):
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=HALoader)


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def targets(action):
    t = action.get("target", {}).get("entity_id")
    return [t] if isinstance(t, str) else list(t or [])


def edge_actions(auto):
    out = []
    for a in auto.get("action", []):
        svc = a.get("service") or a.get("action")
        if svc not in ("switch.turn_on", "switch.turn_off", "script.turn_on"):
            continue
        data = a.get("data", {})
        for t in targets(a):
            out.append((svc, t, json.dumps(data, sort_keys=True)))
    return out


def conditions_pass(auto, date):
    for c in auto.get("condition", []) or []:
        if c.get("condition") == "time" and "weekday" in c:
            if DAYS[date.weekday()] not in c["weekday"]:
                return False
        elif c.get("condition") == "template":
            m = re.search(r"not in \[([^\]]*)\]", c["value_template"])
            skips = set(re.findall(r"'([\d-]+)'", m.group(1))) if m else set()
            if date.isoformat() in skips:
                return False
        else:
            raise ValueError("unknown condition in %s: %s" % (auto.get("id"), c))
    return True


def time_edges(autos, days, start, want_id):
    """Set of (iso local time, service, target, data) for the automations accepted by want_id."""
    edges = set()
    for a in autos:
        if not want_id(a.get("id", "")):
            continue
        for tr in a.get("trigger", []):
            if tr.get("platform") != "time":
                continue
            hh, mm, ss = (int(x) for x in tr["at"].split(":"))
            for d in range(days):
                date = start + dt.timedelta(days=d)
                if not conditions_pass(a, date):
                    continue
                when = dt.datetime(date.year, date.month, date.day, hh, mm, ss, tzinfo=model.TZ).isoformat()
                for act in edge_actions(a):
                    edges.add((when,) + act)
    return edges


def load_dir_list(path):
    out = []
    for n in sorted(os.listdir(path)):
        out += yaml.safe_load(open(os.path.join(path, n))) or []
    return out


def load_dir_named(path):
    out = {}
    for n in sorted(os.listdir(path)):
        out.update(yaml.safe_load(open(os.path.join(path, n))) or {})
    return out


def jinja_expr(template):
    m = re.match(r"^\s*\{\{\s*(.*?)\s*\}\}\s*$", template, re.S)
    return m.group(1)


def old_reconcile(scripts):
    """entity -> function(t, dow, today) -> 'on'/'off', from the old reconcile_* scripts."""
    fns = {}
    for key, sc in scripts.items():
        if not key.startswith("reconcile_") or key == "reconcile_all_plugs":
            continue
        seq = sc["sequence"]
        expr = jinja_expr(seq[0]["variables"]["target"])
        ents = sorted(set(re.findall(r"switch\.[a-z0-9_]+", yaml.safe_dump(sc))))
        for e in ents:
            fns[e] = (lambda x: (lambda t, dow, today: eval(x, {"__builtins__": {}}, {"t": t})))(expr)
    return fns


def new_reconcile(scripts):
    fns = {}
    for key, sc in scripts.items():
        if not key.startswith("sch_reconcile_") or key == "sch_reconcile_all":
            continue
        v = sc["sequence"][0]["variables"]
        expr = jinja_expr(v["target"])
        ent = re.search(r"states\('(switch\.[a-z0-9_]+)'\)", v["cur"]).group(1)
        fns[ent] = (lambda x: (lambda t, dow, today: eval(x, {"__builtins__": {}}, {"t": t, "dow": dow, "today": today})))(expr)
    return fns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--old", default="/srv/compose/homeassistant")
    ap.add_argument("--render", default="/srv/apps/portal/schedules/out/render")
    ap.add_argument("--start", default=None, help="first simulated date (default today)")
    ap.add_argument("--all-managed", action="store_true", help="render every entity from the service snapshot as if all were managed (pre-cutover proof)")
    ap.add_argument("--only", default=None, help="comma list of HA entity ids to restrict the comparison to (default: all in the render)")
    args = ap.parse_args()
    start = dt.date.fromisoformat(args.start) if args.start else dt.datetime.now(model.TZ).date()

    if args.all_managed:
        snap = json.load(open("/srv/apps/portal/schedules/out/schedules.json"))
        for e in snap["entities"]:
            e["managed"] = e["kind"] in model.RENDERABLE_KINDS
        out = renderer.render(snap)
        args.render = tempfile.mkdtemp(prefix="parity-render-")
        for rel, text in out["files"].items():
            pth = os.path.join(args.render, rel)
            os.makedirs(os.path.dirname(pth), exist_ok=True)
            open(pth, "w").write(text)
    old_autos = load_ha(os.path.join(args.old, "automations.yaml"))
    old_scripts = load_ha(os.path.join(args.old, "scripts.yaml"))
    new_autos = load_dir_list(os.path.join(args.render, "automations"))
    new_scripts = load_dir_named(os.path.join(args.render, "scripts"))

    old_e = time_edges(old_autos, args.days, start, lambda i: i.startswith("sched_"))
    new_e = time_edges(new_autos, args.days, start, lambda i: i.startswith("sch_") and not i.endswith(("_failsafe", "_reconnect")))
    only = set(args.only.split(",")) if args.only else None
    ents_new = {e[2] for e in new_e}
    if only:
        old_e = {e for e in old_e if e[2] in only}
        new_e = {e for e in new_e if e[2] in only}
    else:                      # compare exactly the entities the render covers
        old_e = {e for e in old_e if e[2] in ents_new}
    print("simulated %d days from %s: old %d edges, generated %d edges" % (args.days, start, len(old_e), len(new_e)))
    only_old, only_new = sorted(old_e - new_e), sorted(new_e - old_e)
    for e in only_old[:10]:
        print("  ONLY OLD :", e)
    for e in only_new[:10]:
        print("  ONLY NEW :", e)
    edges_ok = not only_old and not only_new
    print("edge parity:", "IDENTICAL" if edges_ok else "DIFFERENT (%d only old, %d only new)" % (len(only_old), len(only_new)))

    fo, fn = old_reconcile(old_scripts), new_reconcile(new_scripts)
    ents = sorted(set(fn) & (only or set(fn)))
    bad = 0
    for e in ents:
        if e not in fo:
            print("  no old reconcile for", e)
            bad += 1
            continue
        for dow in range(1, 8):
            for t in range(1440):
                a = "on" if fo[e](t, dow, "") in ("on", True) else fo[e](t, dow, "")
                b = fn[e](t, dow, "1970-01-01")
                if a != b:
                    bad += 1
                    if bad <= 5:
                        print("  RECONCILE DIFFERS", e, "dow", dow, "t", t, a, b)
    print("reconcile parity over %d entities x 7 weekdays x 1440 minutes:" % len(ents), "IDENTICAL" if not bad else "DIFFERENT (%d)" % bad)
    return 0 if edges_ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
