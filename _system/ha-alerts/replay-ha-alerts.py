#!/usr/bin/env python3
"""Replay the HA alert logic over the recorded history (Task 26 acceptance proof). Sends NOTHING to Telegram.

  python3 replay-ha-alerts.py [--from 2026-10-15] [--to "2026-10-19 10:00"] [--old GITREV] [--list]

Both rule sets run with the REAL watchnotify module (tiers included) in a temporary state directory, with its clock and its
Telegram sender replaced, once every 5 minutes of the recorded history:
  old = _system/ha-alerts/ha-alerts.py at GITREV (default: the version that produced Ivan's 2026-10-18/19 messages)
  new = the ha-alerts.py next to this file
It prints, per day: Telegram messages (old | new) and, for the new rules, the quiet-list entries (heartbeat only).
Limits: the recorder keeps no HA context for Tuya Local changes and no `last_triggered`; the replay uses the same inputs the
live watcher has. Foreign-change detection is by schedule time, as in the live code."""
import argparse, collections, datetime, importlib.util, os, shutil, sqlite3, subprocess, sys, tempfile, time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify as W

PRAGUE = ZoneInfo("Europe/Prague")
DB = "/srv/apps/homeassistant/home-assistant_v2.db"
ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="start", default="2026-10-15")
ap.add_argument("--to", dest="end", default="2026-10-19 10:00")
ap.add_argument("--old", default="8f34b2a~1")
ap.add_argument("--list", action="store_true", help="print every message")
args = ap.parse_args()

def parse(s):
    fmt = "%Y-%m-%d %H:%M" if " " in s else "%Y-%m-%d"
    return datetime.datetime.strptime(s, fmt).replace(tzinfo=PRAGUE).timestamp()
T0, T1 = parse(args.start), parse(args.end)
STEP = 300

# ── harness around the real watchnotify ──
STATE = tempfile.mkdtemp(prefix="replay-wn-")
CLOCK = [0.0]
SENT = []
class FakeTime:
    def __getattr__(self, n):
        return getattr(time, n)
    def time(self):
        return CLOCK[0]
W.time = FakeTime()
W.STATE_DIR = STATE
W.OUTBOX = os.path.join(STATE, "outbox.jsonl")
W.TAG = ""
W._send_raw = lambda msg, attempts=3: (SENT.append((CLOCK[0], msg)) or True)

def reset():
    shutil.rmtree(STATE, ignore_errors=True)
    os.makedirs(STATE)
    SENT.clear()

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

def asof_state(eid, T):
    r = conn.execute("""SELECT s.state, COALESCE(s.last_changed_ts, s.last_updated_ts) FROM states s JOIN states_meta m ON s.metadata_id=m.metadata_id
        WHERE m.entity_id=? AND s.last_updated_ts<=? ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid, T + 1)).fetchone()
    return (r[0], r[1]) if r else (None, None)

def asof_attrs(eid, T):
    import json
    r = conn.execute("""SELECT a.shared_attrs FROM states s JOIN states_meta m ON s.metadata_id=m.metadata_id JOIN state_attributes a ON s.attributes_id=a.attributes_id
        WHERE m.entity_id=? AND s.last_updated_ts<=? ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid, T + 1)).fetchone()
    return json.loads(r[0]) if r else {}

def asof_script_start(script, T):
    rows = conn.execute("""SELECT s.state, s.last_updated_ts FROM states s JOIN states_meta m ON s.metadata_id=m.metadata_id
        WHERE m.entity_id=? AND s.last_updated_ts<=? ORDER BY s.last_updated_ts DESC LIMIT 600""", (script, T + 1)).fetchall()
    start = None
    for st, ts in rows:
        if st == "on":
            start = ts
        elif start is not None:
            break
    return start

class ProxyConn:
    """Gives the OLD module a database that ends at replay time T (its logbook query has no upper bound)."""
    def __init__(self, T): self.T = T
    def execute(self, sql, params=()):
        if "logbook_entry" in sql and "time_fired_ts >= ?" in sql:
            return conn.execute(sql + " AND e.time_fired_ts <= ?", tuple(params) + (self.T,))
        return conn.execute(sql, params)
    def close(self): pass

class FakeSqlite:
    def __init__(self, T): self.T = T
    Error = sqlite3.Error
    def connect(self, *a, **k): return ProxyConn(self.T)

def run_old(m, T):
    m.NOW = T
    m.get_state = lambda c, eid: asof_state(eid, T)
    m.get_attrs = lambda c, eid: asof_attrs(eid, T)
    m.script_last_start = lambda c, sc: asof_script_start(sc, T)
    d = datetime.datetime.fromtimestamp(T, PRAGUE)
    m.prague_hhmm = lambda: d.hour * 60 + d.minute
    m.sqlite3 = FakeSqlite(T)
    m.main()

def day(ts): return datetime.datetime.fromtimestamp(ts, PRAGUE).strftime("%m-%d")
def hm(ts): return datetime.datetime.fromtimestamp(ts, PRAGUE).strftime("%H:%M")

def replay(kind):
    reset()
    tmpfilter = os.path.join(STATE, "filter.json")
    if kind == "old":
        src = subprocess.run(["git", "-C", "/srv/compose", "show", f"{args.old}:_system/ha-alerts/ha-alerts.py"], capture_output=True, text=True, check=True).stdout
        p = os.path.join(STATE, "old_ha_alerts.py"); open(p, "w").write(src)
        m = load(p, "haa_old"); m.FILTER_STATE = tmpfilter
    else:
        m = load(os.path.join(HERE, "ha-alerts.py"), "haa_new")
    quiet_seen, quiet_log = {}, []
    T = T0
    while T <= T1:
        CLOCK[0] = T
        if kind == "old":
            run_old(m, T)
        else:
            m.evaluate(m.Ctx(conn, now=T, filter_state=tmpfilter), W)
            cur = {d["key"]: d for d in W.active_detail() if d["quiet"]}
            for k, d in cur.items():
                if k not in quiet_seen:
                    quiet_seen[k] = T; quiet_log.append((T, d["text"]))
            for k in list(quiet_seen):
                if k not in cur:
                    del quiet_seen[k]
        T += STEP
    msgs = list(SENT)
    open_at_end = [(d["key"], d["tier"], d["quiet"]) for d in W.active_detail()]
    return msgs, quiet_log, open_at_end

old_msgs, _, _ = replay("old")
new_msgs, quiet_log, open_new = replay("new")

def per_day(msgs):
    c = collections.Counter(day(ts) for ts, _ in msgs)
    return c
od, nd = per_day(old_msgs), per_day(new_msgs)
qd = collections.Counter(day(ts) for ts, _ in quiet_log)
days = sorted(set(od) | set(nd) | set(qd) | {day(T0), day(T1)})
print(f"Replay {args.start} -> {args.end}, step {STEP // 60} min, old rules = {args.old}")
print(f"{'day':<7}{'old msgs':>10}{'new msgs':>10}{'new quiet entries':>20}")
for d in days:
    print(f"{d:<7}{od.get(d, 0):>10}{nd.get(d, 0):>10}{qd.get(d, 0):>20}")
print(f"{'total':<7}{len(old_msgs):>10}{len(new_msgs):>10}{len(quiet_log):>20}")
two = ("10-18", "10-19")
print(f"\nIvan's two logged days {two}: old {sum(od.get(d, 0) for d in two)}  new {sum(nd.get(d, 0) for d in two)} (target <= 4)")
def show(title, msgs):
    print("\n" + title)
    for ts, msg in msgs:
        print(f"  {day(ts)} {hm(ts)}  " + msg.replace("\n", " | ")[:150])
if args.list or True:
    show("OLD rules, messages that would have been sent:", [x for x in old_msgs if day(x[0]) in two])
    show("NEW rules, messages (all days):", new_msgs)
    print("\nNEW rules, quiet-list entries (heartbeat only):")
    for ts, text in quiet_log:
        print(f"  {day(ts)} {hm(ts)}  {text[:140]}")
shutil.rmtree(STATE, ignore_errors=True)
