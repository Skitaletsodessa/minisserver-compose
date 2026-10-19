#!/usr/bin/env python3
"""Policy tests for ha-alerts.py (Task 26): synthetic recorder database, real watchnotify with its sender and clock replaced.
Nothing is sent to Telegram.   python3 /srv/compose/_system/ha-alerts/test_ha_alerts_policy.py
Each scenario builds a small history for one device, evaluates the rules at chosen moments of a fixed day (Tue 2026-10-20,
Europe/Prague) and checks which Telegram messages (tier A/B) and which quiet entries (tier C) exist."""
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

print("== A: the dryer / UV lamp left ON after their OFF edge still gives an immediate message")
db = reset(); P = "switch.bathroom_towels"
db.put(P, ts(6, 0), "off"); db.put(P, ts(6, 30), "on")                      # HA turned it on at 06:30, and the 08:00 OFF never happened
run(db, every(ts(6, 30), ts(8, 25)))
check("dryer still ON 25 min after 08:00: no message yet", not msgs("сушилка") and not msgs("bathroom_towels"), str(msgs()))
run(db, [ts(8, 30), ts(8, 35)])
check("dryer still ON 30 min after 08:00: ONE immediate message (tier A)", len(msgs("bathroom_towels")) == 1 and "после выключения по расписанию" in msgs()[0], str(msgs()))
run(db, every(ts(8, 40), ts(9, 20)))
check("tier A repeats like any watcher (6 h) - not within 50 min", len(msgs("bathroom_towels")) == 1, str(msgs()))
db.put(P, ts(9, 30), "off"); at(db, ts(9, 35))
check("tier A sends a recovery message", len(SENT) == 2 and "outage lasted" in SENT[-1][1], str(SENT))

db = reset(); P = "switch.bedroom_parrot"
db.put(P, ts(11, 0), "on")
run(db, every(ts(11, 5), ts(18, 25)))
check("UV lamp ON 25 min after 18:00: silent", not msgs(), str(msgs()))
run(db, [ts(18, 30), ts(18, 35)])
check("UV lamp ON 30 min after 18:00: ONE immediate message", len(msgs("bedroom_parrot")) == 1, str(msgs()))

db = reset(); P = "switch.bathroom_towels"
db.put(P, ts(13, 0), "on")                                                  # switched on by hand at 13:00 and forgotten
run(db, every(ts(13, 5), ts(15, 55)))
check("dryer on by hand 2 h 55 min: silent", not msgs(), str(msgs()))
run(db, [ts(16, 5)])
check("dryer continuously ON > 3 h: immediate message (hard ceiling, tier A)", len(msgs("подряд")) == 1, str(msgs()))

print("\n== foreign changes are never an alert, in either direction")
db = reset()
db.put("switch.bathroom_towels", ts(0), "off"); db.put("switch.bathroom_towels", ts(6, 2), "on"); db.put("switch.bathroom_towels", ts(8, 0, 1), "off")
db.put("switch.kitchen_orchids", ts(0), "off"); db.put("switch.kitchen_orchids", ts(5, 54), "on"); db.put("switch.kitchen_orchids", ts(7, 0, 1), "on")
db.put("switch.living_room_rack", ts(0), "off"); db.put("switch.living_room_rack", ts(7, 0, 1), "on"); db.put("switch.living_room_rack", ts(13, 55), "off")   # Daria switches it off
db.put("switch.living_room_rack", ts(16, 2), "on")
run(db, every(ts(5, 50), ts(8, 30)) + every(ts(13, 50), ts(16, 30)))
check("early ON of the dryer (06:02, 91 min run) and of the orchids (05:54): zero messages", not SENT, str(SENT))
q = quiet()
check("...but the 06:02 dryer ON and the 05:54 orchids ON are in the quiet list", any("bathroom_towels ON в 06:02" in x for x in q) and any("kitchen_orchids ON в 05:54" in x for x in q), str(q))
check("a hand switch-OFF inside the window (13:55) gives no message and is listed quietly", any("living_room_rack OFF в 13:55" in x for x in q), str(q))
check("the quiet list has no duplicate mismatch lines for the same changes", not any("по расписанию" in x and "не по расписанию HA" not in x for x in q), str(q))

print("\n== B: plant lights, plugs, power - once, no repeat, no recovery")
db = reset(); P = "switch.kitchen_orchids"
db.put(P, ts(7, 0), "on")
run(db, every(ts(7, 5), ts(20, 55)))
check("plant light ON 55 min after 20:00: silent", not msgs(), str(msgs()))
run(db, [ts(21, 5), ts(21, 10)])
check("plant light ON 60 min after 20:00: ONE message", len(msgs("kitchen_orchids")) == 1, str(msgs()))
run(db, every(ts(21, 15), ts(23, 55)))
check("tier B never repeats", len(msgs("kitchen_orchids")) == 1, str(msgs()))
db.put(P, ts(23, 58), "off"); at(db, ts(24, 5) if False else ts(0, 5, day_off=1))
check("tier B sends no recovery message and drops out of the active list", len(msgs()) == 1 and not [k for k in active_keys() if "stuck" in k], str((msgs(), active_keys())))

db = reset(); P = "switch.living_room_rack"
db.put(P, ts(7, 0), "unavailable")
run(db, every(ts(7, 5), ts(8, 0)))
check("plug unavailable 60 min: silent", not msgs(), str(msgs()))
run(db, [ts(8, 5), ts(8, 10)])
check("plug unavailable > 60 min: ONE message", len(msgs("недоступна")) == 1, str(msgs()))
db.put(P, ts(9, 0), "on"); at(db, ts(9, 5))
check("...and no recovery message", len(msgs()) == 1, str(msgs()))

db = reset(); P = "switch.bathroom_towels"
db.put(P, ts(18, 30), "on"); db.put("sensor.bathroom_towels_power", ts(18, 31), "0.4")
run(db, every(ts(18, 31), ts(18, 40)) + [ts(18, 55), ts(19, 5)])
check("zero power while ON 15+ min: ONE message", len(msgs("потребление")) == 1, str(msgs()))
db = reset(); db.put("switch.kitchen_orchids", ts(7, 0), "on"); db.put("sensor.kitchen_orchids_power", ts(7, 0), "0.0")
run(db, every(ts(7, 5), ts(9, 0)))
check("kitchen_orchids (fake meter) is exempt from the zero-power rule", not msgs(), str(msgs()))

print("\n== vacuums: one message per incident, no repeats, no recovery")
V, SC = "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "script.vacuum_livingroom_plan"
t0 = ts(20, 30)
db = reset()
db.put(V, ts(20, 0), "docked"); db.put(SC, ts(20, 0), "off"); db.put(SC, t0, "on"); db.put(V, t0 + 60, "cleaning"); db.put(V, ts(21, 28), "paused")
run(db, every(ts(21, 30), ts(21, 55)))
check("paused 27 min: silent", not msgs(), str(msgs()))
run(db, [ts(22, 0), ts(22, 5)])
check("paused >= 30 min during a plan: ONE message that says a plan was interrupted", len(msgs()) == 1 and "во время сценария" in msgs()[0], str(msgs()))
db.put(SC, ts(22, 56), "off"); run(db, every(ts(23, 0), ts(23, 59)))
check("the failed plan adds NO second message (same incident) - only a quiet line", len(msgs()) == 1 and any("инцидент уже отправлен" in x for x in quiet()), str((msgs(), quiet())))
run(db, every(ts(0, 0, day_off=1), ts(8, 0, day_off=1), 900))
check("no reminders for hours of the same pause", len(msgs()) == 1, str(msgs()))
db.put(V, ts(9, 0, day_off=1), "docked"); at(db, ts(9, 5, day_off=1))
check("no recovery message", len(msgs()) == 1, str(msgs()))

db = reset()                                                             # a plan that failed with no stuck robot: ONE message
db.put(V, ts(20, 0), "docked"); db.put(SC, ts(20, 0), "off"); db.put(SC, t0, "on"); db.put(SC, t0 + 30, "off")
run(db, every(ts(20, 40), ts(21, 10)))
check("plan stopped at once (robot not ready) with no incident: ONE message", len(msgs("не дошёл до конца")) == 1, str(msgs()))

db = reset()                                                             # unavailable for a long time after a failed plan = part of the incident
db.put(V, ts(20, 0), "docked"); db.put(SC, ts(20, 0), "off"); db.put(SC, t0, "on"); db.put(SC, t0 + 30, "off"); db.put(V, ts(23, 0), "unavailable")
run(db, every(ts(23, 5), ts(23, 55)) + every(ts(0, 0, day_off=1), ts(12, 0, day_off=1), 1800))
check("vacuum unavailable for 13 h after a failed plan: only the plan message, nothing else", len(msgs()) == 1 and "не дошёл" in msgs()[0], str(msgs()))
db = reset()                                                             # unavailable with no incident: B after 6 h, once
db.put(V, ts(1, 0), "unavailable")
run(db, every(ts(1, 5), ts(6, 55)))
check("vacuum unavailable 6 h with no incident: silent before 6 h", not msgs(), str(msgs()))
run(db, [ts(7, 5), ts(7, 10)] + every(ts(8, 0), ts(12, 0), 1800))
check("vacuum unavailable >= 6 h with no incident: ONE message, no repeat", len(msgs("недоступен")) == 1, str(msgs()))

db = reset(); db.put("sensor.xiaomi_ov51gl_e160_mop_life_level", ts(8, 0), "8"); db.put("fan.zhimi_airp_rmb1", ts(8, 0), "on"); db.put("sensor.zhimi_airp_rmb1_filter_life_remaining", ts(8, 0), "50")
db.put("vacuum.xiaomi_ov51gl_e160_robot_cleaner", ts(8, 0), "docked"); at(db, ts(8, 5))
check("consumable < 10 % is a quiet line only (heartbeat), no message", not SENT and any("моп 8%" in x for x in quiet()), str((SENT, quiet())))
db = reset(); db.put("fan.zhimi_airp_rmb1", ts(8, 0), "unavailable"); run(db, [ts(9, 5), ts(10, 0)])
check("purifier unavailable is quiet", not SENT and any("недоступен" in x for x in quiet()), str((SENT, quiet())))

print("\n== schedules come from the portal snapshot (Task 27)")
import json as _json
_orig = (haa.SNAPSHOT, haa.SNAPSHOT_CACHE, haa.SCHEDULE, haa.PLAN_TIMES)
_tmp = tempfile.mkdtemp(prefix="haa-snap-")
_snap = {"schema": 1, "entities": [
    {"id": "d", "kind": "plug", "ha_target": "switch.bathroom_towels", "rules": [{"id": "m", "days": [1, 3], "start": "06:30", "end": "08:00"}], "skip_dates": []},
    {"id": "p", "kind": "plug", "ha_target": "switch.bedroom_parrot", "rules": [{"id": "d", "days": [1, 2, 3, 4, 5, 6, 7], "start": "11:00", "end": "18:00"}], "skip_dates": ["2026-10-20"]},
    {"id": "v", "kind": "vacuum_plan", "ha_target": "script.vacuum_bedroom_plan", "rules": [{"id": "a", "days": [2], "start": "09:00"}, {"id": "b", "days": [1, 2], "start": "19:00"}], "skip_dates": []}]}
sched, plans = haa.schedules_for(_snap, datetime.date(2026, 10, 20))          # a Tuesday
check("weekday rule: the Monday/Wednesday dryer window does not exist on Tuesday", sched["switch.bathroom_towels"] == [], str(sched))
check("a skipped date removes the day's window", sched["switch.bedroom_parrot"] == [], str(sched))
check("vacuum triggers of the day only (Tuesday: 09:00 and 19:00)", plans["script.vacuum_bedroom_plan"] == (540, 1140), str(plans))
sched, plans = haa.schedules_for(_snap, datetime.date(2026, 10, 19))          # a Monday
check("Monday: dryer window, parrot window, only the 19:00 trigger", sched["switch.bathroom_towels"] == [(390, 480)] and sched["switch.bedroom_parrot"] == [(660, 1080)]
      and plans["script.vacuum_bedroom_plan"] == (1140,), str((sched, plans)))
haa.SCHEDULE = {"switch.bathroom_towels": []}
check("no window today: no recent edge, no crash", haa.last_edge("switch.bathroom_towels", 600)[1] > 10 ** 6)

haa.SNAPSHOT, haa.SNAPSHOT_CACHE = os.path.join(_tmp, "missing.json"), os.path.join(_tmp, "cache.json")
check("snapshot and cache both missing: reported unusable", haa.init_schedules() is False and haa.SCHEDULE == {})
db = reset(); at(db, ts(12, 0))
check("unreadable schedules: ONE tier-B message, no plug checks, no crash", len(msgs("расписания недоступны")) == 1, str(msgs()))
at(db, ts(12, 5)); at(db, ts(12, 10))
check("... and it does not repeat", len(msgs("расписания недоступны")) == 1, str(msgs()))
_json.dump(_snap, open(haa.SNAPSHOT_CACHE, "w"))
check("live snapshot missing, cache present: the cache is used", haa.init_schedules(DAY) is True and haa.SCHEDULE_STATE["source"] == "cache", str(haa.SCHEDULE_STATE))
_json.dump(_snap, open(haa.SNAPSHOT, "w"))
os.remove(haa.SNAPSHOT_CACHE)
check("live snapshot present: used, and a fresh cache copy is written", haa.init_schedules(DAY) is True and haa.SCHEDULE_STATE["source"] == "live"
      and os.path.exists(haa.SNAPSHOT_CACHE))
open(haa.SNAPSHOT, "w").write("{garbled")
check("garbled live snapshot falls back to the cache", haa.init_schedules(DAY) is True and haa.SCHEDULE_STATE["source"] == "cache")
haa.SNAPSHOT, haa.SNAPSHOT_CACHE, haa.SCHEDULE, haa.PLAN_TIMES = _orig
shutil.rmtree(_tmp, ignore_errors=True)

print("\n== not covered here, by design")
print("NOTE  HA down: ha-watch.py is untouched and uses the default tier A (immediate, repeats, recovery - see test_watchnotify_tiers.py).")
print("NOTE  Carp light above 20 % / on after 19:00: Task 25 phase E was not implemented, there is no such rule to keep or test.")
print("\nRESULT:", "all passed" if not FAILS else "FAILED: " + "; ".join(FAILS))
shutil.rmtree(STATE, ignore_errors=True)
sys.exit(1 if FAILS else 0)
