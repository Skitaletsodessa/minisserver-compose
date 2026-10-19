#!/usr/bin/env python3
"""Home Assistant alert logic (Task 25 Phase D, rewritten for Task 26: severity tiers, one message per incident).

Runs every 5 minutes (ha-alerts.timer), reads the HA recorder database read-only, talks to Telegram only through watchnotify.
Tiers (watchnotify): A = message, repeats, recovery message; B = ONE message, no repeat, no recovery; C = quiet, only in the
08:00 heartbeat. A switch-on or switch-off that HA did not make is NEVER an alert by itself, in either direction.

  A  towel dryer / UV lamp still ON 30 min after its scheduled OFF edge; dryer continuously ON > 3 h
  B  plant lights ON >= 60 min after 20:00; plug unavailable > 60 min; zero power while ON for 15 min;
     purifier filter < 10 % (once, then every 30 days); vacuum paused/error/idle-off-dock >= 30 min (one message per incident,
     says whether a plan run was interrupted); vacuum plan not finished or running > 4 h (only if no such incident);
     vacuum unavailable at its plan time or for 6 h (only if no incident in the previous 24 h)
  C  any mismatch with the schedule; a switch change HA did not make ("foreign change", kept 24 h); purifier unavailable;
     vacuum unavailable (part of an incident); vacuum consumables < 10 %

All time-dependent input goes through a Ctx object, so the same rules can be replayed over the recorded history
(replay-ha-alerts.py). Not here: HA being down (ha-watch), the carp light (Phase E was not implemented).
WATCH_TEST=1 -> messages are prefixed [TEST] and DO go to Telegram (watchnotify); use the replay harness for dry runs.
"""
import os, sys, time, sqlite3, datetime, json
sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify

DB = "/srv/apps/homeassistant/home-assistant_v2.db"
try:
    from zoneinfo import ZoneInfo
    PRAGUE = ZoneInfo("Europe/Prague")
except Exception:                                   # pragma: no cover
    PRAGUE = datetime.timezone.utc

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
ZERO_PWR_WATTS = {
    "switch.bathroom_towels": 2.0, "switch.living_room_rack": 5.0, "switch.livingroom_rack_big": 5.0,
    "switch.kitchen_orchids": 1.0, "switch.bedroom_parrot": 2.0, "switch.minisserver": 2.0,
}
SKIP_ZERO_POWER = {"switch.kitchen_orchids"}        # fake metering chip (DPS 20-25 always 0)
PLANT_LIGHTS = {"switch.living_room_rack", "switch.livingroom_rack_big", "switch.kitchen_orchids", "switch.minisserver"}
DRYER = "switch.bathroom_towels"

# ── policy (Task 26) ──
UNAVAIL_PLUG_SECS = 60 * 60
ZERO_PWR_SECS = 15 * 60
DRYER_CEILING_SECS = 3 * 3600
STUCK_AFTER_EDGE = {                                # entity: (seconds after the OFF edge, tier)
    "switch.bathroom_towels": (30 * 60, "A"), "switch.bedroom_parrot": (30 * 60, "A"),
    **{e: (60 * 60, "B") for e in PLANT_LIGHTS},
}
STUCK_WINDOW_MAX = 4 * 3600                         # "after its OFF edge" means within the next hours, not the next morning
MISMATCH_GRACE = 120                                # HA's edge command takes a few seconds
FOREIGN_NEAR = 150                                  # a change this close to a schedule edge or an HA start is HA's own
FOREIGN_KEEP = 24 * 3600
START_BEFORE, START_AFTER = 90, 180                 # HA restores plug states a little before its start automation fires

PURIFIERS = {
    "zhimi_airp_rmb1": ("Очиститель 4 Lite", "fan.zhimi_airp_rmb1", "sensor.zhimi_airp_rmb1_filter_life_remaining"),
    "zhimi_cpa4_9832": ("Очиститель 4 Compact", "fan.zhimi_cpa4_9832_air_purifier", "sensor.zhimi_cpa4_9832_filter_life_level"),
}
PURIFIER_UNAVAIL_SECS = 60 * 60
FILTER_LOW_PCT = 10
FILTER_REMIND_SECS = 30 * 24 * 3600
FILTER_STATE = "/var/lib/watch-alerts/ha-purifier-filter.json"

VACUUMS = {                                         # key: (label, vacuum entity, sensor prefix, plan script, logbook name, plan times)
    "ov51gl_e160": ("Пылесос H40 (спальня)", "vacuum.xiaomi_ov51gl_e160_robot_cleaner", "xiaomi_ov51gl_e160",
                    "script.vacuum_bedroom_plan", "Vacuum Bedroom", (9 * 60, 19 * 60)),
    "ov51gl_b5f6": ("Пылесос H40 (гостиная)", "vacuum.xiaomi_ov51gl_b5f6_robot_cleaner", "xiaomi_ov51gl_b5f6",
                    "script.vacuum_livingroom_plan", "Vacuum Livingroom", (20 * 60 + 30,)),
}
VACUUM_CONSUMABLES = (("filter_life_level", "фильтр"), ("mop_life_level", "моп"), ("dust_bag_life_level", "мешок для пыли"),
                      ("brush_life_level", "щётка (1)"), ("brush_life_level_2", "щётка (2)"))
VACUUM_STUCK_STATES = ("paused", "error", "idle")   # idle = stopped off the dock; vacuum.fault is a sticky code and is never used
VACUUM_STUCK_SECS = 30 * 60
VACUUM_UNAVAIL_QUIET_SECS = 60 * 60
VACUUM_UNAVAIL_ESCALATE_SECS = 6 * 3600
PLAN_DUE_GRACE = 2 * 3600
PLAN_HUNG_SECS = 4 * 3600
PLAN_ALERT_FROM = 1792151514                        # earlier plan runs never wrote the 'ПЛАН ВЫПОЛНЕН' line
PLAN_DONE_MARK = "ПЛАН ВЫПОЛНЕН"


class Ctx:
    """The world as of `now`: live (now = the present) or replayed (now = a moment in the recorded history)."""
    def __init__(self, conn, now=None, filter_state=None):
        self.conn = conn
        self.now = time.time() if now is None else now
        self.filter_state = filter_state or FILTER_STATE

    def prague(self, ts=None):
        return datetime.datetime.fromtimestamp(self.now if ts is None else ts, PRAGUE)

    def hhmm(self):
        d = self.prague()
        return d.hour * 60 + d.minute

    def state(self, eid):
        row = self.conn.execute("""
            SELECT s.state, COALESCE(s.last_changed_ts, s.last_updated_ts) FROM states s
            JOIN states_meta m ON s.metadata_id = m.metadata_id
            WHERE m.entity_id = ? AND s.last_updated_ts <= ? ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid, self.now + 1)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def attrs(self, eid):
        row = self.conn.execute("""
            SELECT a.shared_attrs FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
            JOIN state_attributes a ON s.attributes_id = a.attributes_id
            WHERE m.entity_id = ? AND s.last_updated_ts <= ? ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid, self.now + 1)).fetchone()
        try:
            return json.loads(row[0]) if row else {}
        except ValueError:
            return {}

    def transitions(self, eid, since):
        """[(ts, state)]: the state at `since`, then every change up to now."""
        out, prev = [], None
        init = self.conn.execute("""
            SELECT s.state FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
            WHERE m.entity_id = ? AND s.last_updated_ts <= ? ORDER BY s.last_updated_ts DESC LIMIT 1""", (eid, since)).fetchone()
        if init:
            out.append((since, init[0])); prev = init[0]
        for ts, st in self.conn.execute("""
                SELECT s.last_updated_ts, s.state FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
                WHERE m.entity_id = ? AND s.last_updated_ts > ? AND s.last_updated_ts <= ? ORDER BY s.last_updated_ts""",
                (eid, since, self.now + 1)):
            if st != prev:
                out.append((ts, st)); prev = st
        return out

    def periods(self, eid, since, states, min_secs=0):
        """[(start, end)] where the entity stayed in one of `states` for at least min_secs (an open period ends now)."""
        tr = self.transitions(eid, since)
        res = []
        for i, (ts, st) in enumerate(tr):
            end = tr[i + 1][0] if i + 1 < len(tr) else self.now
            if st in states and end - ts >= min_secs:
                res.append((ts, end))
        return res

    def runs(self, script, since):
        """[(start, end_or_None)] of a script, from its off/on history (the recorder drops last_triggered)."""
        res, start = [], None
        for ts, st in self.transitions(script, since):
            if st == "on" and start is None:
                start = ts
            elif st != "on" and start is not None:
                res.append((start, ts)); start = None
        if start is not None:
            res.append((start, None))
        return res

    def plan_done(self, logname, start, end):
        for _ts, data in self.conn.execute("""
                SELECT e.time_fired_ts, d.shared_data FROM events e
                JOIN event_types et ON e.event_type_id = et.event_type_id JOIN event_data d ON e.data_id = d.data_id
                WHERE et.event_type = 'logbook_entry' AND e.time_fired_ts >= ? AND e.time_fired_ts <= ?""",
                (start - 5, (end if end is not None else self.now) + 30)):
            try:
                j = json.loads(data)
            except ValueError:
                continue
            if logname in j.get("name", "") and PLAN_DONE_MARK in j.get("message", ""):
                return True
        return False

    def ha_starts(self, since):
        out = []
        for ts, data in self.conn.execute("""
                SELECT e.time_fired_ts, d.shared_data FROM events e
                JOIN event_types et ON e.event_type_id = et.event_type_id JOIN event_data d ON e.data_id = d.data_id
                WHERE et.event_type = 'automation_triggered' AND e.time_fired_ts > ? AND e.time_fired_ts <= ?""",
                (since, self.now + 1)):
            try:
                if "on HA start" in json.loads(data).get("name", ""):
                    out.append(ts)
            except ValueError:
                pass
        return out


def hm(ctx, ts):
    return ctx.prague(ts).strftime("%H:%M")


def expected(eid, t):
    for (s, e) in SCHEDULE[eid]:
        if s <= t < e:
            return "on"
    return "off"


def last_edge(eid, t):
    """(kind 'start'|'end', seconds since that edge) for the most recent schedule edge at Prague minute-of-day t."""
    edges = sorted({(w[0], "start") for w in SCHEDULE[eid]} | {(w[1], "end") for w in SCHEDULE[eid]})
    past = [(m, k) for m, k in edges if m <= t]
    if past:
        m, k = past[-1]
        return k, (t - m) * 60
    m, k = edges[-1]
    return k, (t - (m - 1440)) * 60


def near_edge(eid, ts, ctx):
    d = ctx.prague(ts)
    m = d.hour * 60 + d.minute + d.second / 60
    for (s, e) in SCHEDULE[eid]:
        for edge in (s, e):
            if abs(m - edge) * 60 <= FOREIGN_NEAR or abs(m - edge - 1440) * 60 <= FOREIGN_NEAR or abs(m - edge + 1440) * 60 <= FOREIGN_NEAR:
                return True
    return False


def check_plugs(ctx, N):
    t = ctx.hhmm()
    starts = ctx.ha_starts(ctx.now - FOREIGN_KEEP - 3600)
    for item in N.active_detail():                  # foreign-change lines live for 24 h, then clear silently
        if item["key"].startswith("ha-foreign-"):
            try:
                if ctx.now - int(item["key"].rsplit("-", 1)[1]) > FOREIGN_KEEP:
                    N.clear(item["key"], "")
            except ValueError:
                pass

    for eid in SCHEDULE:
        name = eid.split(".", 1)[1]
        state, changed = ctx.state(eid)
        if state is None:
            continue
        age = ctx.now - (changed or ctx.now)

        key_u = f"ha-unavail-{name}"                                    # B: plug unavailable > 60 min, once
        if state == "unavailable" and age > UNAVAIL_PLUG_SECS:
            N.alert(key_u, f"HA: розетка {name} недоступна {int(age // 60)} мин", tier="B")
        else:
            N.clear(key_u, "")
        if state == "unavailable":
            continue

        # C: a change HA did not make (not at a schedule edge, not around an HA start, when states are restored): listed, never sent
        foreign = bool(changed and state in ("on", "off") and ctx.now - changed < FOREIGN_KEEP and not near_edge(eid, changed, ctx)
                       and not any(-START_BEFORE <= changed - s <= START_AFTER for s in starts))
        if foreign:
            N.alert(f"ha-foreign-{name}-{int(changed)}",
                    f"HA: {name} {state.upper()} в {hm(ctx, changed)} не по расписанию HA (нет контекста автоматизации)", tier="C")

        exp = expected(eid, t)
        kind, since_edge = last_edge(eid, t)
        key_m = f"ha-mismatch-{name}"                                   # C: mismatch with the schedule (a foreign change already says it)
        if state != exp and age > MISMATCH_GRACE and since_edge >= MISMATCH_GRACE + 60 and not foreign:
            N.alert(key_m, f"HA: {name} {state.upper()} с {hm(ctx, changed)}, по расписанию {exp.upper()}", tier="C")
        else:
            N.clear(key_m, "")

        key_s = f"ha-stuck-on-{name}"                                   # A/B: still ON after the scheduled OFF edge
        cfg = STUCK_AFTER_EDGE.get(eid)
        if cfg and state == "on" and exp == "off" and kind == "end" and cfg[0] <= since_edge <= STUCK_WINDOW_MAX:
            N.alert(key_s, f"HA: {name} всё ещё включена через {int(since_edge // 60)} мин после выключения по расписанию", tier=cfg[1])
        else:
            N.clear(key_s, "")

        if eid == DRYER:                                                # A: hard ceiling for the dryer
            key_d = "ha-dryer-runaway"
            if state == "on" and age > DRYER_CEILING_SECS:
                N.alert(key_d, f"HA: сушилка работает {int(age // 60)} мин подряд (предел 3 ч)", tier="A")
            else:
                N.clear(key_d, "")

        psensor = POWER_SENSOR.get(eid)                                 # B: zero power while ON
        key_z = f"ha-zero-power-{name}"
        if psensor and eid not in SKIP_ZERO_POWER and state == "on" and age > ZERO_PWR_SECS:
            p_state, p_ts = ctx.state(psensor)
            try:
                watts = float(p_state) if p_state not in (None, "unavailable", "unknown") else None
            except ValueError:
                watts = None
            if watts is not None and watts < ZERO_PWR_WATTS[eid] and (ctx.now - (p_ts or ctx.now)) > ZERO_PWR_SECS:
                N.alert(key_z, f"HA: {name} включена, но потребление {watts:.1f} Вт уже {int((ctx.now - p_ts) // 60)} мин (нагрузка выключена?)", tier="B")
            else:
                N.clear(key_z, "")
        else:
            N.clear(key_z, "")


def check_purifiers(ctx, N):
    try:
        fstate = json.load(open(ctx.filter_state))
    except (OSError, ValueError):
        fstate = {}
    dirty = False
    for key, (label, fan, filt) in PURIFIERS.items():
        state, changed = ctx.state(fan)
        age = ctx.now - (changed or ctx.now)
        key_u = f"ha-purifier-unavail-{key}"                            # C
        if state == "unavailable" and age > PURIFIER_UNAVAIL_SECS:
            N.alert(key_u, f"HA: {label} недоступен {int(age // 60)} мин", tier="C")
        else:
            N.clear(key_u, "")
        f_state, _ = ctx.state(filt)                                    # B: one message, then at most every 30 days
        try:
            pct = float(f_state)
        except (TypeError, ValueError):
            continue
        if pct < FILTER_LOW_PCT:
            last = fstate.get(key)
            if last is None or ctx.now - last >= FILTER_REMIND_SECS:
                N.send(f"HA: {label} - ресурс фильтра {pct:.0f}%, пора заменить фильтр")
                fstate[key] = ctx.now
                dirty = True
        elif key in fstate:
            del fstate[key]
            dirty = True
    if dirty:
        try:
            json.dump(fstate, open(ctx.filter_state, "w"))
        except OSError as exc:
            print(f"ha-alerts: cannot write {ctx.filter_state}: {exc}", file=sys.stderr)


def incidents(ctx, vac, since):
    """Stretches in which the vacuum was paused / in error / stopped off the dock for at least 30 min."""
    return ctx.periods(vac, since, VACUUM_STUCK_STATES, VACUUM_STUCK_SECS)


def recent_runs(ctx, script, horizon):
    """Runs of a plan script that are still running or ended within `horizon` seconds. The history is read further back
    than that so a run that began before the horizon keeps its real start time."""
    return [(s, e) for s, e in ctx.runs(script, ctx.now - horizon - 36 * 3600)
            if s >= PLAN_ALERT_FROM and (e is None or ctx.now - e < horizon)]


def failed_runs(ctx, script, logname, horizon):
    """Plan runs (started after the feature went live) that ended without the end marker."""
    return [(s, e) for s, e in recent_runs(ctx, script, horizon) if e is not None and not ctx.plan_done(logname, s, e)]


def check_vacuums(ctx, N):
    for key, (label, vac, prefix, script, logname, plan_times) in VACUUMS.items():
        state, changed = ctx.state(vac)
        if state is None:
            continue
        age = ctx.now - (changed or ctx.now)
        script_state, _ = ctx.state(script)
        recent_inc = incidents(ctx, vac, ctx.now - 24 * 3600)
        recent_fail = failed_runs(ctx, script, logname, 24 * 3600)

        # B: paused / error / stopped off the dock >= 30 min: ONE message per incident, says whether a plan was interrupted
        key_i = f"ha-vacuum-stuck-{key}"
        if state in VACUUM_STUCK_STATES and age >= VACUUM_STUCK_SECS:
            where = "во время сценария уборки (он остановится по таймауту)" if script_state == "on" else "вне сценария уборки"
            word = {"paused": "на паузе", "error": "в ошибке", "idle": "стоит вне базы"}[state]
            N.alert(key_i, f"HA: {label} {word} уже {int(age // 60)} мин, {where}. Возможно застрял.", tier="B")
        elif state != "unavailable":                        # unavailable keeps an open incident open (a dead battery is part of it)
            N.clear(key_i, "")

        # C -> B: unavailable. Part of an open incident when it was stuck or its plan failed in the last 24 h.
        key_u = f"ha-vacuum-unavail-{key}"
        if state == "unavailable":
            quiet_key = f"ha-vacuum-unavail-quiet-{key}"
            if age >= VACUUM_UNAVAIL_QUIET_SECS:
                N.alert(quiet_key, f"HA: {label} недоступен {int(age // 60)} мин", tier="C")
            due_now = any(0 <= ((ctx.hhmm() - m) % 1440) * 60 <= PLAN_DUE_GRACE and changed is not None
                          and changed < ctx.now - (((ctx.hhmm() - m) % 1440) * 60) for m in plan_times)
            escalate = age >= VACUUM_UNAVAIL_ESCALATE_SECS or due_now
            if escalate and not (recent_inc or recent_fail):
                why = "к времени плана" if due_now and age < VACUUM_UNAVAIL_ESCALATE_SECS else "дольше 6 ч"
                N.alert(key_u, f"HA: {label} недоступен {int(age // 60)} мин ({why}), без предшествующего инцидента", tier="B")
        else:
            N.clear(key_u, "")
            N.clear(f"ha-vacuum-unavail-quiet-{key}", "")

        # B: plan did not finish / > 4 h, unless an incident (stuck >= 30 min) already told Ivan about it
        key_f, key_h = f"ha-vacuum-plan-failed-{key}", f"ha-vacuum-plan-hung-{key}"
        runs = recent_runs(ctx, script, 12 * 3600)
        last = runs[-1] if runs else None
        if last:
            start, end = last
            had_incident = any(e >= start for _s, e in ctx.periods(vac, start - 12 * 3600, VACUUM_STUCK_STATES + ("unavailable",), VACUUM_STUCK_SECS))
            if end is None and ctx.now - start > PLAN_HUNG_SECS and not had_incident:
                N.alert(key_h, f"HA: сценарий пылесоса ({label}) идёт уже {int((ctx.now - start) // 3600)} ч, возможно завис", tier="B")
            else:
                N.clear(key_h, "")
            if end is not None and ctx.now - end >= 600 and not ctx.plan_done(logname, start, end):
                if had_incident:
                    N.alert(key_f + "-quiet", f"HA: сценарий пылесоса ({label}) от {hm(ctx, start)} не дошёл до конца (инцидент уже отправлен)", tier="C")
                else:
                    N.alert(key_f, f"HA: сценарий пылесоса ({label}), запущенный в {hm(ctx, start)}, не дошёл до конца (смотри уведомления HA и журнал)", tier="B")
            else:
                N.clear(key_f, "")
                N.clear(key_f + "-quiet", "")
        else:
            for k in (key_f, key_f + "-quiet", key_h):
                N.clear(k, "")

        # C: consumables < 10 % (heartbeat line only)
        for suffix, what in VACUUM_CONSUMABLES:
            val, _ = ctx.state(f"sensor.{prefix}_{suffix}")
            key_c = f"ha-consumable-{key}-{suffix}"
            try:
                pct = float(val)
            except (TypeError, ValueError):
                continue
            if pct < FILTER_LOW_PCT:
                N.alert(key_c, f"HA: {label}: {what} {pct:.0f}%, пора заменить", tier="C")
            else:
                N.clear(key_c, "")


def evaluate(ctx, N):
    check_plugs(ctx, N)
    check_purifiers(ctx, N)
    check_vacuums(ctx, N)


def main():
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    except Exception as exc:
        watchnotify.alert("ha-alerts-db", f"ha-alerts: не могу открыть HA DB: {exc}")
        return 1
    evaluate(Ctx(conn), watchnotify)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
