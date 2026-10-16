#!/usr/bin/env python3
"""Task 25 Phase D: HA plug alert watchdog.

Checks every 5 min:
 1. Plug unavailable >10 min
 2. State != schedule for >15 min (mismatch); a plant light switched off by hand inside its ON window is not reported
 3. Bathroom towels on >90 min (dryer runaway)
 4. Switch on but power <2 W for >15 min (device off at socket)
 5. Air purifier unavailable >60 min (non-critical, slower than plugs)
 6. Air purifier filter life <10%: one message, then a reminder at most every 30 days
 7. Robot vacuum unavailable >60 min (non-critical)
 8. Robot vacuum stuck: state paused or error for >10 min (vacuum.fault is a sticky last-event code, e.g. 100008 on every docking - never alert on it alone)
 9. Robot vacuum consumable <10% (filter, mop, dust bag, brushes): one message, reminder at most every 30 days
10. Livingroom vacuum plan started but not finished (no 'ПЛАН ВЫПОЛНЕН' logbook line) or running >4 h

Reads HA recorder DB directly. Alerts via watchnotify.
WATCH_TEST=1 -> print only.
"""
import os, sys, time, sqlite3, datetime, json
sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify

DB = "/srv/apps/homeassistant/home-assistant_v2.db"
NOW = time.time()

SCHEDULE = {
    "switch.bathroom_towels":     [(6*60+30, 8*60), (18*60+30, 20*60)],
    "switch.living_room_rack":    [(7*60, 20*60)],
    "switch.livingroom_rack_big": [(7*60, 20*60)],
    "switch.kitchen_orchids":     [(7*60, 20*60)],
    "switch.bedroom_parrot":      [(11*60, 18*60)],
    "switch.minisserver":         [(7*60, 20*60)],
}

POWER_SENSOR = {
    "switch.bathroom_towels":     "sensor.bathroom_towels_power",
    "switch.living_room_rack":    "sensor.living_room_rack_power",
    "switch.livingroom_rack_big": "sensor.livingroom_rack_big_power",
    "switch.kitchen_orchids":     "sensor.kitchen_orchids_power",
    "switch.bedroom_parrot":      "sensor.bedroom_parrot_power",
    "switch.minisserver":         "sensor.minisserver_power",
}

# kitchen_orchids grow light may draw very little; lower threshold for it
ZERO_PWR_WATTS = {
    "switch.bathroom_towels":     2.0,
    "switch.living_room_rack":    5.0,
    "switch.livingroom_rack_big": 5.0,
    "switch.kitchen_orchids":     1.0,
    "switch.bedroom_parrot":      2.0,
    "switch.minisserver":         2.0,
}

UNAVAIL_SECS  = 10 * 60
MISMATCH_SECS = 15 * 60
DRYER_SECS    = 90 * 60
ZERO_PWR_SECS = 15 * 60

# kitchen_orchids: fake metering chip (DPS 20-25 always 0, no factory calibration)
SKIP_ZERO_POWER = {"switch.kitchen_orchids"}

# Xiaomi air purifiers (Task 25 Phase F): monitoring only, nothing is ever switched.
PURIFIERS = {
    "zhimi_airp_rmb1": ("Очиститель 4 Lite", "fan.zhimi_airp_rmb1",
                        "sensor.zhimi_airp_rmb1_filter_life_remaining"),
    "zhimi_cpa4_9832": ("Очиститель 4 Compact", "fan.zhimi_cpa4_9832_air_purifier",
                        "sensor.zhimi_cpa4_9832_filter_life_level"),
}
PURIFIER_UNAVAIL_SECS = 60 * 60
FILTER_LOW_PCT = 10
FILTER_REMIND_SECS = 30 * 24 * 3600
FILTER_STATE = "/var/lib/watch-alerts/ha-purifier-filter.json"

# Xiaomi RoboVac H40 (xiaomi_miot, local). Monitoring only, nothing is ever commanded.
VACUUMS = {
    "ov51gl_e160": ("Пылесос H40 (спальня)", "vacuum.xiaomi_ov51gl_e160_robot_cleaner", "xiaomi_ov51gl_e160"),
    "ov51gl_b5f6": ("Пылесос H40 (гостиная)", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "xiaomi_ov51gl_b5f6"),
}
VACUUM_CONSUMABLES = (
    ("filter_life_level", "фильтр"),
    ("mop_life_level", "моп"),
    ("dust_bag_life_level", "мешок для пыли"),
    ("brush_life_level", "щётка (1)"),
    ("brush_life_level_2", "щётка (2)"),
)
VACUUM_STUCK_SECS = 10 * 60

# Plan runs started before this moment never wrote the 'ПЛАН ВЫПОЛНЕН' line: do not alert on them.
PLAN_ALERT_FROM = 1792151514
VACUUM_STUCK_STATES = {"paused": "на паузе", "error": "в ошибке"}


def prague_hhmm():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Europe/Prague"))
    except Exception:
        now = datetime.datetime.utcnow()
    return now.hour * 60 + now.minute


# A grow light switched OFF by hand inside its ON window is the household's decision, not a fault (Ivan, 2026-10-16).
# Only OFF-by-hand is exempt; a light that is OFF since before the last schedule edge (HA command lost) still alerts.
PLANT_LIGHTS = {"switch.living_room_rack", "switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver"}
MANUAL_MARGIN_SECS = 120


def last_edge_ts(entity_id, t):
    """Epoch of the most recent schedule edge (start or end of a window) of this plug; t = Prague minute of the day."""
    edges = sorted({m for w in SCHEDULE[entity_id] for m in w})
    past = [m for m in edges if m <= t]
    delta = (t - max(past)) if past else (t - (max(edges) - 1440))
    return NOW - delta * 60


def expected(entity_id, t):
    for (s, e) in SCHEDULE[entity_id]:
        if s <= t < e:
            return "on"
    return "off"


def get_state(conn, entity_id):
    """Return (state_str, last_changed_ts) or (None, None)."""
    cur = conn.execute("""
        SELECT s.state, COALESCE(s.last_changed_ts, s.last_updated_ts)
        FROM states s
        JOIN states_meta sm ON s.metadata_id = sm.metadata_id
        WHERE sm.entity_id = ?
        ORDER BY s.state_id DESC LIMIT 1
    """, (entity_id,))
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def check_purifiers(conn):
    try:
        with open(FILTER_STATE) as f:
            fstate = json.load(f)
    except (OSError, ValueError):
        fstate = {}
    dirty = False

    for key, (label, fan, filt) in PURIFIERS.items():
        state, changed_ts = get_state(conn, fan)
        age = (NOW - changed_ts) if changed_ts else 0

        key_u = f"ha-purifier-unavail-{key}"
        if state == "unavailable" and age > PURIFIER_UNAVAIL_SECS:
            watchnotify.alert(key_u, f"HA: {label} недоступен {int(age//60)} мин")
        else:
            watchnotify.clear(key_u, f"HA: {label} снова доступен")

        f_state, _ = get_state(conn, filt)
        try:
            pct = float(f_state)
        except (TypeError, ValueError):
            continue
        if pct < FILTER_LOW_PCT:
            last = fstate.get(key)
            if last is None or NOW - last >= FILTER_REMIND_SECS:
                watchnotify.send(f"HA: {label} - ресурс фильтра {pct:.0f}%, пора заменить фильтр")
                if not watchnotify.TEST:
                    fstate[key] = NOW
                    dirty = True
        elif key in fstate:
            del fstate[key]
            dirty = True

    if dirty:
        try:
            with open(FILTER_STATE, "w") as f:
                json.dump(fstate, f)
        except OSError as exc:
            print(f"ha-alerts: cannot write {FILTER_STATE}: {exc}", file=sys.stderr)


def get_attrs(conn, entity_id):
    row = conn.execute("""
        SELECT a.shared_attrs
        FROM states s
        JOIN states_meta sm ON s.metadata_id = sm.metadata_id
        JOIN state_attributes a ON s.attributes_id = a.attributes_id
        WHERE sm.entity_id = ?
        ORDER BY s.state_id DESC LIMIT 1
    """, (entity_id,)).fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


def check_vacuums(conn):
    try:
        with open(FILTER_STATE) as f:
            fstate = json.load(f)
    except (OSError, ValueError):
        fstate = {}
    dirty = False

    for key, (label, vac, prefix) in VACUUMS.items():
        state, changed_ts = get_state(conn, vac)
        if state is None:
            continue
        age = (NOW - changed_ts) if changed_ts else 0

        key_u = f"ha-vacuum-unavail-{key}"
        if state == "unavailable" and age > PURIFIER_UNAVAIL_SECS:
            watchnotify.alert(key_u, f"HA: {label} недоступен {int(age//60)} мин")
        else:
            watchnotify.clear(key_u, f"HA: {label} снова доступен")
        if state == "unavailable":
            continue

        attrs = get_attrs(conn, vac)
        try:
            fault = int(attrs.get("vacuum.fault") or 0)
        except (TypeError, ValueError):
            fault = 0
        key_f = f"ha-vacuum-fault-{key}"
        if state in VACUUM_STUCK_STATES and age > VACUUM_STUCK_SECS:
            desc = attrs.get("vacuum.status_desc") or state
            watchnotify.alert(key_f,
                f"HA: {label} {VACUUM_STUCK_STATES[state]} уже {int(age//60)} мин "
                f"(статус «{desc}», последний код {fault}), возможно застрял")
        else:
            watchnotify.clear(key_f, f"HA: {label} снова в работе")

        for suffix, what in VACUUM_CONSUMABLES:
            val, _ = get_state(conn, f"sensor.{prefix}_{suffix}")
            fkey = f"{key}:{suffix}"
            try:
                pct = float(val)
            except (TypeError, ValueError):
                continue
            if pct < FILTER_LOW_PCT:
                last = fstate.get(fkey)
                if last is None or NOW - last >= FILTER_REMIND_SECS:
                    watchnotify.send(f"HA: {label} - {what} {pct:.0f}%, пора заменить")
                    if not watchnotify.TEST:
                        fstate[fkey] = NOW
                        dirty = True
            elif fkey in fstate:
                del fstate[fkey]
                dirty = True

    if dirty:
        try:
            with open(FILTER_STATE, "w") as f:
                json.dump(fstate, f)
        except OSError as exc:
            print(f"ha-alerts: cannot write {FILTER_STATE}: {exc}", file=sys.stderr)


VACUUM_PLANS = (
    ("script.vacuum_livingroom_plan", "Vacuum Livingroom", "livingroom", "гостиная"),
    ("script.vacuum_bedroom_plan", "Vacuum Bedroom", "bedroom", "спальня"),
)


def check_vacuum_plan(conn):
    """Each plan script writes 'ПЛАН ВЫПОЛНЕН' to the logbook at its end; a run that stopped earlier (battery, wrong
    mode, station fault, manual stop) leaves no such line."""
    for script, logname, key, label in VACUUM_PLANS:
        _check_one_plan(conn, script, logname, key, label)


def _check_one_plan(conn, script, logname, key, label):
    state, _ = get_state(conn, script)
    if state is None:
        return
    attrs = get_attrs(conn, script)
    try:
        last = datetime.datetime.fromisoformat(str(attrs.get("last_triggered")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return
    if last < PLAN_ALERT_FROM:
        return
    run_age = NOW - last
    key_h = f"ha-vacuum-plan-hung-{key}"
    key_f = f"ha-vacuum-plan-failed-{key}"
    if state == "on" and run_age > 4 * 3600:
        watchnotify.alert(key_h, f"HA: сценарий пылесоса ({label}) идёт уже {int(run_age//3600)} ч, возможно завис")
    else:
        watchnotify.clear(key_h, f"HA: сценарий пылесоса ({label}) больше не висит")
    if state == "on" or run_age < 600 or run_age > 12 * 3600:
        if run_age > 12 * 3600:
            watchnotify.clear(key_f, f"HA: прошлый сбой сценария пылесоса ({label}) старше 12 ч, алерт снят")
        return
    done = False
    for (ts, data) in conn.execute("""
            SELECT e.time_fired_ts, d.shared_data FROM events e
            JOIN event_types et ON e.event_type_id = et.event_type_id
            JOIN event_data d ON e.data_id = d.data_id
            WHERE et.event_type = 'logbook_entry' AND e.time_fired_ts >= ?""", (last - 5,)):
        try:
            j = json.loads(data)
        except ValueError:
            continue
        if logname in j.get("name", "") and "ПЛАН ВЫПОЛНЕН" in j.get("message", ""):
            done = True
            break
    if done:
        watchnotify.clear(key_f, f"HA: сценарий пылесоса ({label}) выполнен до конца")
    else:
        when = datetime.datetime.fromtimestamp(last).strftime("%H:%M")
        watchnotify.alert(key_f, f"HA: сценарий пылесоса ({label}), запущенный в {when}, не дошёл до конца "
                                 "(смотри уведомления HA и журнал). Пылесос мог остаться на базе с недоделанной уборкой.")


def main():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    except Exception as exc:
        watchnotify.alert("ha-alerts-db", f"ha-alerts: не могу открыть HA DB: {exc}")
        return 1

    t = prague_hhmm()

    for eid in SCHEDULE:
        name = eid.replace("switch.", "")
        state, changed_ts = get_state(conn, eid)
        if state is None:
            continue

        age = (NOW - changed_ts) if changed_ts else 0

        # 1. Unavailable
        key_u = f"ha-unavail-{name}"
        if state == "unavailable" and age > UNAVAIL_SECS:
            watchnotify.alert(key_u,
                f"HA: розетка {name} недоступна {int(age//60)} мин")
        else:
            watchnotify.clear(key_u, f"HA: {name} снова доступна")

        if state == "unavailable":
            continue

        # 2. Mismatch
        exp = expected(eid, t)
        key_m = f"ha-mismatch-{name}"
        manual_off = (eid in PLANT_LIGHTS and state == "off" and exp == "on" and bool(changed_ts)
                      and changed_ts > last_edge_ts(eid, t) + MANUAL_MARGIN_SECS)
        if state != exp and age > MISMATCH_SECS and not manual_off:
            watchnotify.alert(key_m,
                f"HA: {name} в состоянии '{state}', по расписанию '{exp}' "
                f"уже {int(age//60)} мин")
        else:
            watchnotify.clear(key_m, f"HA: {name} в норме")

        # 3. Dryer runaway
        if eid == "switch.bathroom_towels":
            key_d = "ha-dryer-runaway"
            if state == "on" and age > DRYER_SECS:
                watchnotify.alert(key_d,
                    f"HA: сушилка работает {int(age//60)} мин подряд (норма ≤90 мин)")
            else:
                watchnotify.clear(key_d, "HA: сушилка выключена или в норме")

        # 4. Zero power while on (skip entities with fake/broken metering)
        psensor = POWER_SENSOR.get(eid)
        if psensor and eid in SKIP_ZERO_POWER:
            watchnotify.clear(f"ha-zero-power-{name}", f"HA: {name} мониторинг мощности отключён (нет метрики)")
        elif psensor and eid not in SKIP_ZERO_POWER and state == "on" and age > ZERO_PWR_SECS:
            p_state, p_ts = get_state(conn, psensor)
            key_z = f"ha-zero-power-{name}"
            try:
                watts = float(p_state) if p_state not in (None, "unavailable", "unknown") else None
            except ValueError:
                watts = None
            if watts is not None and watts < ZERO_PWR_WATTS[eid]:
                p_age = (NOW - p_ts) if p_ts else 0
                if p_age > ZERO_PWR_SECS:
                    watchnotify.alert(key_z,
                        f"HA: {name} включена, но потребление {watts:.1f} Вт уже "
                        f"{int(p_age//60)} мин (нагрузка выключена?)")
            else:
                watchnotify.clear(key_z, f"HA: {name} потребляет нормально")

    check_purifiers(conn)
    check_vacuums(conn)
    check_vacuum_plan(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
