#!/usr/bin/env python3
"""Task 32: policy tests for the plan-less robot rules of ha-alerts.py (synthetic recorder, nothing is sent to Telegram).   python3 /srv/compose/_system/ha-alerts/test_ha_alerts_tiger.py"""
import datetime, importlib.util, os, shutil, sqlite3, sys, tempfile, time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify as W

PRAGUE = ZoneInfo("Europe/Prague")
DAY = datetime.date(2026, 10, 20)
def ts(h, m=0, s=0, day_off=0):
    d = datetime.datetime.combine(DAY + datetime.timedelta(days=day_off), datetime.time(h, m, s), PRAGUE)
    return d.timestamp()

spec = importlib.util.spec_from_file_location("haa", os.path.join(HERE, "ha-alerts.py"))
haa = importlib.util.module_from_spec(spec); spec.loader.exec_module(haa)

STATE = tempfile.mkdtemp(prefix="haa-test-")
CLOCK, SENT = [0.0], []
class FT:
    def __getattr__(self, n): return getattr(time, n)
    def time(self): return CLOCK[0]
W.time, W.STATE_DIR, W.OUTBOX, W.TAG = FT(), STATE, os.path.join(STATE, "outbox.jsonl"), ""
W._send_raw = lambda msg, attempts=3: (SENT.append((CLOCK[0], msg)) or True)

FAILS = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)

class DB:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.executescript("""
            CREATE TABLE states_meta(metadata_id INTEGER PRIMARY KEY, entity_id TEXT);
            CREATE TABLE state_attributes(attributes_id INTEGER PRIMARY KEY, shared_attrs TEXT);
            CREATE TABLE states(state_id INTEGER PRIMARY KEY, metadata_id INT, state TEXT, last_updated_ts REAL, last_changed_ts REAL, attributes_id INT);
            CREATE TABLE event_types(event_type_id INTEGER PRIMARY KEY, event_type TEXT);
            CREATE TABLE event_data(data_id INTEGER PRIMARY KEY, shared_data TEXT);
            CREATE TABLE events(event_id INTEGER PRIMARY KEY, event_type_id INT, time_fired_ts REAL, data_id INT);
            INSERT INTO event_types VALUES (1,'automation_triggered'),(2,'logbook_entry');""")
        self.ids = {}
    def put(self, eid, t, state):
        mid = self.ids.setdefault(eid, len(self.ids) + 1)
        self.c.execute("INSERT OR IGNORE INTO states_meta VALUES (?,?)", (mid, eid))
        self.c.execute("INSERT INTO states(metadata_id,state,last_updated_ts,last_changed_ts) VALUES (?,?,?,NULL)", (mid, state, t))
    def log(self, t, name, message):
        import json
        self.c.execute("INSERT INTO event_data(shared_data) VALUES (?)", (json.dumps({"name": name, "message": message}),))
        self.c.execute("INSERT INTO events(event_type_id,time_fired_ts,data_id) VALUES (2,?,last_insert_rowid())", (t,))

def reset():
    shutil.rmtree(STATE, ignore_errors=True); os.makedirs(STATE); SENT.clear()
    return DB()

def at(db, t):
    CLOCK[0] = t
    haa.evaluate(haa.Ctx(db.c, now=t, filter_state=os.path.join(STATE, "filter.json")), W)

def run(db, times):
    for t in times:
        at(db, t)

def msgs(sub=None): return [m for _t, m in SENT if sub is None or sub in m]
def quiet(): return [d["text"] for d in W.active_detail() if d["quiet"]]
def active_keys(): return [d["key"] for d in W.active_detail()]

def every(a, b, step=300):
    out, t = [], a
    while t <= b:
        out.append(t); t += step
    return out


V, B = "vacuum.eta_tiger_robot_vacuum", "sensor.eta_tiger_robot_vacuum_battery"
print("== Task 32: plan-less robot (ETA Tiger) rules")
db = reset()
at(db, ts(10)); check("entity absent: nothing at all (not paired yet)", not SENT and not active_keys(), str((SENT, active_keys())))

db = reset(); db.put(V, ts(9), "docked"); db.put(B, ts(9), "100")
run(db, every(ts(9, 5), ts(23, 55)))
check("docked all day at 100 %: silent", not SENT and not active_keys(), str((SENT, active_keys())))

db = reset(); db.put(V, ts(9), "docked"); db.put(V, ts(10), "cleaning"); db.put(V, ts(10, 40), "error")
run(db, every(ts(10, 45), ts(10, 44)) + [ts(10, 42), ts(10, 44)])
check("error for 4 minutes: silent", not msgs("Tiger"), str(msgs()))
run(db, [ts(10, 46), ts(10, 50), ts(11, 0), ts(12, 0)])
check("error for 5+ minutes: ONE tier-B message, never repeated", len(msgs("Tiger")) == 1 and "ошибке" in msgs()[0], str(msgs()))
db.put(V, ts(12, 5), "docked"); at(db, ts(12, 10))
check("recovery from the fault drops it from the active list (tier B: no recovery message)", len(msgs("Tiger")) == 1 and not [k for k in active_keys() if "tiger" in k], str((msgs(), active_keys())))

db = reset(); db.put(V, ts(10), "cleaning"); db.put(V, ts(10, 20), "paused")
run(db, every(ts(10, 25), ts(10, 49)))
check("paused 29 min: silent", not msgs("Tiger"), str(msgs()))
run(db, [ts(10, 51), ts(10, 56)])
check("paused 30 min: ONE message", len(msgs("Tiger")) == 1 and "на паузе" in msgs()[0], str(msgs()))

db = reset(); db.put(V, ts(10), "idle")
run(db, [ts(10, 31), ts(10, 40)])
check("stopped off the dock (idle) 30 min: ONE message", len(msgs("вне базы")) == 1, str(msgs()))

db = reset(); db.put(V, ts(10), "cleaning"); db.put(V, ts(12, 0), "returning")
run(db, every(ts(10, 5), ts(12, 55)))
check("cleaning then returning 2 h 55 min: silent", not msgs("Tiger"), str(msgs()))
run(db, [ts(13, 5), ts(13, 10)])
check("in work > 3 h without a pause: ONE message", len(msgs("подряд")) == 1, str(msgs()))
db.put(V, ts(13, 20), "docked"); at(db, ts(13, 25))
check("docked again clears it (no recovery message)", len(msgs("Tiger")) == 1 and not [k for k in active_keys() if "tiger" in k], str(active_keys()))

db = reset(); db.put(V, ts(10), "docked"); db.put(B, ts(10), "60")
run(db, every(ts(10, 5), ts(12, 55)))
check("docked 2 h 55 min at 60 %: silent", not msgs("Tiger"), str(msgs()))
run(db, [ts(13, 5), ts(13, 10)])
check("docked 3 h, battery flat at 60 %: ONE message 'заряд не растёт'", len(msgs("заряд не растёт")) == 1, str(msgs()))

db = reset(); db.put(V, ts(10), "docked"); db.put(B, ts(10), "40"); db.put(B, ts(11), "55"); db.put(B, ts(12), "70"); db.put(B, ts(12, 59), "85")
run(db, every(ts(13, 5), ts(13, 30)))
check("docked 3 h, battery rising: silent", not msgs("Tiger"), str(msgs()))

db = reset(); db.put(V, ts(10), "docked"); db.put(B, ts(10), "95")
run(db, every(ts(13, 5), ts(13, 30)))
check("docked 3 h, battery flat but >= 90 %: silent (full)", not msgs("Tiger"), str(msgs()))

db = reset(); db.put(V, ts(9), "unavailable")
run(db, every(ts(9, 5), ts(10, 50)))
check("unavailable 1 h 50 min: no Telegram message, one quiet entry", not SENT and any("Tiger" in x for x in quiet()), str((SENT, quiet())))
db.put(V, ts(11), "docked"); at(db, ts(11, 5))
check("back: the quiet entry is gone", not [k for k in active_keys() if "tiger" in k], str(active_keys()))

print("\n%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
