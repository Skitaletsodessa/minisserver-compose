#!/usr/bin/env python3
"""Task 25 Phase D: HA plug alert watchdog.

Checks every 5 min:
 1. Plug unavailable >10 min
 2. State != schedule for >15 min (mismatch)
 3. Bathroom towels on >90 min (dryer runaway)
 4. Switch on but power <2 W for >15 min (device off at socket)

Reads HA recorder DB directly. Alerts via watchnotify.
WATCH_TEST=1 -> print only.
"""
import os, sys, time, sqlite3, datetime
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
}

POWER_SENSOR = {
    "switch.bathroom_towels":     "sensor.bathroom_towels_power",
    "switch.living_room_rack":    "sensor.living_room_rack_power",
    "switch.livingroom_rack_big": "sensor.livingroom_rack_big_power",
    "switch.kitchen_orchids":     "sensor.kitchen_orchids_power",
    "switch.bedroom_parrot":      "sensor.bedroom_parrot_power",
}

# kitchen_orchids grow light may draw very little; lower threshold for it
ZERO_PWR_WATTS = {
    "switch.bathroom_towels":     2.0,
    "switch.living_room_rack":    5.0,
    "switch.livingroom_rack_big": 5.0,
    "switch.kitchen_orchids":     1.0,
    "switch.bedroom_parrot":      2.0,
}

UNAVAIL_SECS  = 10 * 60
MISMATCH_SECS = 15 * 60
DRYER_SECS    = 90 * 60
ZERO_PWR_SECS = 15 * 60


def prague_hhmm():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Europe/Prague"))
    except Exception:
        now = datetime.datetime.utcnow()
    return now.hour * 60 + now.minute


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
        if state != exp and age > MISMATCH_SECS:
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

        # 4. Zero power while on
        psensor = POWER_SENSOR.get(eid)
        if psensor and state == "on" and age > ZERO_PWR_SECS:
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

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
