"""The gateway's domain logic: devices, switching with read-back, vacuums, service tiles and probes, capabilities.
Home Assistant remains the executor and the schedule service the only writer of schedules; this module reads, requests and confirms."""
import asyncio
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from . import config
from .auth import GwError
from .db import iso

PRESENT = ("on", "off")


class Core:
    def __init__(self, db, people, ha, sched, hub, clock=time.time):
        self.db, self.people, self.ha, self.sched, self.hub, self.clock = db, people, ha, sched, hub, clock
        self._eff = {}                    # device id -> (ts, effective)
        self._last_switch = {}            # device id -> monotonic ts of the last accepted change
        self.eid_device = {}              # HA entity id -> device id
        self.eid_vacuum = {}              # HA entity id -> vacuum id
        self.probes = {}                  # tile id -> result dict
        self.probe_ts = None
        self.ha.on_change = self._on_ha_change
        self._probe_task = None
        self.devices = {}
        self.vacuums = {}
        self._guards = {}

    # ── seed data (first start only; afterwards it is editable data) ──
    def seed(self):
        if not self.db.q1("SELECT 1 FROM device_meta LIMIT 1"):
            for did, m in config.DEVICE_SEED.items():
                self.db.x("INSERT INTO device_meta(device_id,kind,power_sensor,household_switchable,display_name,switch_mode) VALUES (?,?,?,?,?,?)", (did, m["kind"], m["power_sensor"], m["household_switchable"], m.get("name"), m.get("switch_mode", "plain")))
        for did, m in config.DEVICE_SEED.items():                  # a plug added AFTER the first start (e.g. the citrus plug, 2026-10-21) gets its meta row once; an existing row is never touched
            if not self.db.q1("SELECT 1 FROM device_meta WHERE device_id=?", (did,)):
                self.db.x("INSERT INTO device_meta(device_id,kind,power_sensor,household_switchable,display_name,switch_mode) VALUES (?,?,?,?,?,?)",
                          (did, m["kind"], m["power_sensor"], m["household_switchable"], m.get("name"), m.get("switch_mode", "plain")))
        for did, m in config.DEVICE_SEED.items():                  # Task 29: a database from before switch modes existed gets the seed default, once (NULL only, never an edit)
            self.db.x("UPDATE device_meta SET switch_mode=? WHERE device_id=? AND switch_mode IS NULL", (m.get("switch_mode", "plain"), did))
        for did, m in config.DEVICE_SEED.items():                  # databases created before display names existed: fill the empty ones only (never overwrite an edit)
            if m.get("name"):
                self.db.x("UPDATE device_meta SET display_name=? WHERE device_id=? AND display_name IS NULL", (m["name"], did))
        if not self.db.q1("SELECT 1 FROM icon_rules LIMIT 1"):
            for i, (kw, icon) in enumerate(config.ICON_SEED):
                self.db.x("INSERT INTO icon_rules(keyword,icon,priority) VALUES (?,?,?)", (kw, icon, i))
        if not self.db.q1("SELECT 1 FROM service_tiles LIMIT 1"):
            for i, (sid, name, icon, url, probe, vis) in enumerate(config.SERVICE_SEED):
                self.db.x("INSERT INTO service_tiles(id,name,icon,url,probe,visible_to,position) VALUES (?,?,?,?,?,?,?)", (sid, name, icon, url, probe, vis, i))

    def icon_for(self, *names):
        text = " ".join(n.lower() for n in names if n)
        for r in self.db.q("SELECT keyword, icon FROM icon_rules ORDER BY priority"):
            if r["keyword"].lower() in text:
                return r["icon"]
        return "plug"

    # ── scope: what HA may be asked about / told to do ──
    async def sync_scope(self):
        ents = await self.sched.entities(force=True)
        watch, ctl = set(), set()
        self.eid_device = {}
        self.devices = {}
        for e in ents:
            if e["kind"] == "plug" and e.get("ha_target", "").startswith("switch."):
                meta = self.db.q1("SELECT * FROM device_meta WHERE device_id=?", (e["id"],))
                self.devices[e["id"]] = {"entity": e, "kind": meta["kind"] if meta else "plug", "power_sensor": meta["power_sensor"] if meta else None,
                                         "name": (meta["display_name"] if meta else None) or e["name"],
                                         "switch_mode": (meta["switch_mode"] if meta else None) or "plain",
                                         "switchable": bool(meta["household_switchable"]) if meta else False}
                watch.add(e["ha_target"])
                ctl.add(e["ha_target"])
                self.eid_device[e["ha_target"]] = e["id"]
                if meta and meta["power_sensor"]:
                    watch.add(meta["power_sensor"])
        self.vacuums = {}
        self.eid_vacuum = {}
        sched_ids = {e["id"] for e in ents}
        for vid, v in config.VACUUM_SEED.items():
            self.vacuums[vid] = v
            watch |= {v["vacuum"], v["script"]}
            ctl |= {v["vacuum"], v["script"]}
            self.eid_vacuum[v["vacuum"]] = vid
            self.eid_vacuum[v["script"]] = vid
        self.ha.set_scope(watch, ctl)
        return len(self.devices), len(self.vacuums)

    # ── HA events -> hub ──
    def _on_ha_change(self, eid, old, new):
        if eid in self.eid_device:
            did = self.eid_device[eid]
            self._eff.pop(did, None)
            self.hub.publish("device", {"id": did, "state": self._norm(new["state"]), "stale": self.ha.stale()})
        elif eid in self.eid_vacuum:
            vid = self.eid_vacuum[eid]
            if eid == self.vacuums[vid]["script"] and old and old["state"] == "on" and new["state"] == "off":
                vs = self.ha.state(self.vacuums[vid]["vacuum"])
                res = "finished" if vs in ("docked", "charging", "charged") else "interrupted"
                self.db.x("INSERT INTO vacuum_runs(vacuum_id,last_result,last_result_ts) VALUES (?,?,?) ON CONFLICT(vacuum_id) DO UPDATE SET last_result=excluded.last_result, last_result_ts=excluded.last_result_ts",
                          (vid, res, self.clock()))
            self.hub.publish("vacuum", {"id": vid, "state": self.ha.state(self.vacuums[vid]["vacuum"]), "script": self.ha.state(self.vacuums[vid]["script"])})

    @staticmethod
    def _norm(s):
        return s if s in PRESENT else "unavailable"

    # ── permissions ──
    @staticmethod
    def can_switch(principal, dev):
        if principal["role"] == "admin":
            return True
        return principal["role"] == "household" and dev["switchable"]

    @staticmethod
    def can_edit_schedule(principal, entity):
        if principal["role"] == "admin":
            return True
        return principal["role"] == "household" and bool(entity.get("flags", {}).get("household_editable"))

    # ── devices ──
    async def _effective(self, did):
        c = self._eff.get(did)
        if c and self.clock() - c[0] < 10:
            return c[1]
        eff = None
        try:
            eff = await self.sched.effective(did)
        except GwError:
            pass
        self._eff[did] = (self.clock(), eff)
        return eff

    async def device_view(self, principal, did):
        d = self.devices[did]
        e = d["entity"]
        eid = e["ha_target"]
        c = self.ha.get(eid)
        state = self._norm(c["state"]) if c else "unavailable"
        stale = self.ha.stale() or c is None
        eff = await self._effective(did)
        source, manual_until, next_edge = "unknown", None, None
        if eff:
            ne = eff.get("next_edge")
            next_edge = {"at": ne["at"], "action": ne["action"]} if ne else None
            if state in PRESENT and eff.get("state") in PRESENT:
                source = "schedule" if state == eff["state"] else "manual"
                if source == "manual" and ne:
                    manual_until = ne["at"]
        power = None
        if d["power_sensor"]:
            pc = self.ha.get(d["power_sensor"])
            try:
                power = float(pc["state"]) if pc and pc["state"] not in (None, "unavailable", "unknown") else None
            except ValueError:
                power = None
        can_sw = self.can_switch(principal, d) and state in PRESENT and not self.ha.stale()
        run = self._run_view((await self.sched.active_runs()).get(did))
        if run and state == "on":
            source, manual_until = "run", None
        mx = e.get("guardrails", {}).get("max_on_minutes")
        presets = self.run_presets(did)
        return {"id": did, "name": d["name"], "kind": d["kind"], "icon": self.icon_for(e["name"], did), "state": state,
                "state_age_s": round(self.clock() - c["changed"], 1) if c and c["changed"] else None, "stale": stale, "source": source,
                "manual_until": manual_until, "next_edge": next_edge, "can_switch": can_sw, "can_edit_schedule": self.can_edit_schedule(principal, e),
                "schedule_id": did, "power_w": power, "guardrails": e.get("guardrails", {}),
                "switch_mode": d["switch_mode"], "run": run, "run_presets": presets, "run_max_minutes": mx,
                "can_run": bool(can_sw and state == "off" and not run)}

    async def devices_view(self, principal):
        return [await self.device_view(principal, did) for did in self.devices]

    async def switch(self, principal, did, on):
        if did not in self.devices:
            raise GwError(404, "not_found", "no such device", {"id": did})
        d = self.devices[did]
        if principal["role"] == "readonly":
            raise GwError(403, "forbidden", "a read-only person cannot switch devices")
        if not self.can_switch(principal, d):
            raise GwError(403, "not_switchable", "this device may not be switched by your role", {"device": did})
        if on and d["switch_mode"] == "timed":
            raise GwError(409, "timed_only", "this device is switched on for a limited time only: choose how long", {"device": did, "presets": self.run_presets(did)})
        eid = d["entity"]["ha_target"]
        target = "on" if on else "off"
        cur = self.ha.state(eid)
        actor = dict(person=principal["person_id"], phone=principal.get("phone_id"), ip=principal.get("ip"))
        if cur in ("unavailable", "unknown", None):
            self.db.audit("switch", target=did, before={"state": cur}, after={"requested": target, "outcome": "refused_unavailable"}, ok=False, **actor)
            raise GwError(409, "device_unavailable", "the device is unavailable in Home Assistant", {"state": cur})
        if cur == target:
            return {"outcome": "confirmed", "device_id": did, "requested": target, "observed": cur, "changed": False, "waited_ms": 0}
        last = self._last_switch.get(did)
        if last is not None and time.monotonic() - last < config.DEBOUNCE_S:
            raise GwError(429, "too_fast", "this device was switched a moment ago", {"retry_after_s": 2}, {"Retry-After": "2"})
        self._last_switch[did] = time.monotonic()
        try:
            await self.ha.call("switch", "turn_on" if on else "turn_off", eid)
        except GwError as e:
            self.db.audit("switch", target=did, before={"state": cur}, after={"requested": target, "outcome": "failed", "error": e.code}, ok=False, **actor)
            raise
        obs, waited = await self.ha.wait_state(eid, lambda s: s == target, config.SWITCH_CONFIRM_S)
        outcome = "confirmed" if obs == target else "unconfirmed"
        self._eff.pop(did, None)
        self.db.audit("switch", target=did, before={"state": cur}, after={"requested": target, "observed": obs, "outcome": outcome}, ok=outcome == "confirmed", **actor)
        if not on and outcome == "confirmed":
            await self._close_active_run(principal, did)
        return {"outcome": outcome, "device_id": did, "requested": target, "observed": self._norm(obs), "changed": True, "waited_ms": int(waited * 1000)}

    # ── timed runs (Task 29 Part 1) ──
    def _run_view(self, r):
        """The card's `run` object from the service's run, or None when there is none or it is over."""
        if not r:
            return None
        import datetime as _dt
        ends = _dt.datetime.fromisoformat(r["ends_at"]).timestamp()
        remaining = int(ends - self.clock())
        if remaining <= 0:
            return None
        return {"id": r["id"], "started_at": r["started_at"], "ends_at": r["ends_at"], "remaining_s": remaining, "started_by": r["started_by"],
                "armed": bool(r.get("armed")), "minutes_total": r["minutes_total"]}

    async def patch_device(self, principal, did, fields):
        """Admin: the editable device data - how its On button works (timed|plain) and whether the household may switch it."""
        if did not in self.devices:
            raise GwError(404, "not_found", "no such device", {"id": did})
        if not isinstance(fields, dict) or not fields or set(fields) - {"switch_mode", "household_switchable"}:
            raise GwError(400, "bad_body", "an object with switch_mode and/or household_switchable is required")
        d = self.devices[did]
        before = {"switch_mode": d["switch_mode"], "household_switchable": d["switchable"]}
        new = dict(before)
        if "switch_mode" in fields:
            if fields["switch_mode"] not in ("timed", "plain"):
                raise GwError(422, "bad_value", "switch_mode must be timed or plain")
            new["switch_mode"] = fields["switch_mode"]
        if "household_switchable" in fields:
            if not isinstance(fields["household_switchable"], bool):
                raise GwError(422, "bad_value", "household_switchable must be true or false")
            new["household_switchable"] = fields["household_switchable"]
        self.db.x("UPDATE device_meta SET switch_mode=?, household_switchable=? WHERE device_id=?", (new["switch_mode"], 1 if new["household_switchable"] else 0, did))
        d["switch_mode"], d["switchable"] = new["switch_mode"], new["household_switchable"]
        self.db.audit("device_patch", target=did, before=before, after=new, **self._actor(principal))
        self.hub.publish("device", {"id": did, "state": self._norm(self.ha.state(d["entity"]["ha_target"])), "stale": self.ha.stale()})
        return await self.device_view(principal, did)

    def run_presets(self, did):
        d = self.devices[did]
        mx = d["entity"].get("guardrails", {}).get("max_on_minutes")
        return [m for m in config.RUN_PRESETS.get(d["kind"], config.RUN_PRESETS["lamp"]) if mx is None or m <= mx]

    async def _close_active_run(self, principal, did):
        """After a CONFIRMED switch-off: an active run of the device is over (cancelled in the service). Idempotent; a failure here is harmless
        because Home Assistant ends the run at its end anyway."""
        try:
            run = (await self.sched.active_runs(force=True)).get(did)
            if run and self._run_view(run):
                role, acting = self._srv(principal)
                await self.sched.run_call("DELETE", "/v1/runs/" + run["id"], role, acting)
        except GwError:
            pass

    @staticmethod
    def _srv(principal):
        return ("admin" if principal["role"] == "admin" else "household",
                {"person": principal["person_id"], "phone": principal.get("phone_id") or "-"})

    def _run_device(self, principal, did):
        if did not in self.devices:
            raise GwError(404, "not_found", "no such device", {"id": did})
        d = self.devices[did]
        if principal["role"] == "readonly":
            raise GwError(403, "forbidden", "a read-only person cannot switch devices")
        if not self.can_switch(principal, d):
            raise GwError(403, "not_switchable", "this device may not be switched by your role", {"device": did})
        return d

    async def _wait_armed(self, rid, role):
        """Polls the service until Home Assistant has loaded the run's end (the applier verified it), up to RUN_ARM_WAIT_S. Returns the run or None."""
        t0 = time.monotonic()
        while True:
            st, js, _ = await self.sched.call("GET", "/v1/runs/" + rid, role)
            if st == 200 and js and js.get("armed"):
                return js
            if st == 200 and js and js.get("status") != "active":
                return None
            if time.monotonic() - t0 >= config.RUN_ARM_WAIT_S:
                return None
            await asyncio.sleep(config.RUN_ARM_POLL_S)

    async def start_run(self, principal, did, minutes):
        """Switch ON for N minutes. Order matters (Task 29): 1. the service accepts the run (guardrails) and renders its END into Home Assistant,
        2. the gateway waits until HA has loaded it (armed), 3. only then the plug is switched ON and read back. If the end cannot be armed, NOTHING is
        switched on and the run is withdrawn. From then on HA ends the run by itself, whatever happens to the gateway and the service."""
        d = self._run_device(principal, did)
        eid = d["entity"]["ha_target"]
        actor = self._actor(principal)
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable; nothing was started")
        cur = self.ha.state(eid)
        if cur in ("unavailable", "unknown", None):
            raise GwError(409, "device_unavailable", "the device is unavailable in Home Assistant", {"state": cur})
        if cur != "off":
            raise GwError(409, "already_on", "the device is already on", {"state": cur})
        last = self._last_switch.get(did)
        if last is not None and time.monotonic() - last < config.DEBOUNCE_S:
            raise GwError(429, "too_fast", "this device was switched a moment ago", {"retry_after_s": 2}, {"Retry-After": "2"})
        role, acting = self._srv(principal)
        try:
            run = await self.sched.run_call("POST", "/v1/runs", role, acting, {"entity_id": did, "minutes": minutes})
        except GwError as e:
            self.db.audit("run_start", target=did, after={"minutes": minutes, "outcome": "refused", "error": e.code}, ok=False, **actor)
            raise
        self._last_switch[did] = time.monotonic()                          # the debounce starts when a run was ACCEPTED (a rejected attempt can be corrected at once)
        armed = await self._wait_armed(run["id"], role)
        if not armed:
            try:
                await self.sched.run_call("DELETE", "/v1/runs/" + run["id"] + "?withdraw=1", role, acting)
            except GwError:
                pass
            self.db.audit("run_start", target=did, after={"minutes": minutes, "run": run["id"], "outcome": "not_armed"}, ok=False, **actor)
            raise GwError(503, "run_not_armed", "Home Assistant did not confirm the automatic switch-off in time; nothing was switched on")
        try:
            await self.ha.call("switch", "turn_on", eid)
        except GwError as e:
            try:
                await self.sched.run_call("DELETE", "/v1/runs/" + run["id"] + "?withdraw=1", role, acting)
            except GwError:
                pass
            self.db.audit("run_start", target=did, after={"minutes": minutes, "run": run["id"], "outcome": "failed", "error": e.code}, ok=False, **actor)
            raise
        obs, waited = await self.ha.wait_state(eid, lambda s: s == "on", config.SWITCH_CONFIRM_S)
        outcome = "confirmed" if obs == "on" else "unconfirmed"
        self._eff.pop(did, None)
        self.sched.drop_runs_cache()
        self.db.audit("run_start", target=did, before={"state": cur}, after={"minutes": minutes, "run": run["id"], "ends_at": run["ends_at"], "observed": obs, "outcome": outcome},
                      ok=outcome == "confirmed", **actor)
        self.hub.publish("device", {"id": did, "state": self._norm(obs), "stale": self.ha.stale()})
        return {"outcome": outcome, "device_id": did, "action": "start", "run": self._run_view(armed), "observed": self._norm(obs), "waited_ms": int(waited * 1000)}

    async def extend_run(self, principal, did, minutes):
        d = self._run_device(principal, did)
        run = (await self.sched.active_runs(force=True)).get(did)
        actor = self._actor(principal)
        if not run or not self._run_view(run):
            raise GwError(404, "no_run", "no timed run is active for this device")
        role, acting = self._srv(principal)
        try:
            ext = await self.sched.run_call("POST", "/v1/runs/%s/extend" % run["id"], role, acting, {"minutes": minutes})
        except GwError as e:
            self.db.audit("run_extend", target=did, after={"minutes": minutes, "run": run["id"], "outcome": "refused", "error": e.code}, ok=False, **actor)
            raise
        armed = await self._wait_armed(ext["id"], role)
        outcome = "confirmed" if armed else "unconfirmed"
        self.sched.drop_runs_cache()
        self.db.audit("run_extend", target=did, before={"ends_at": run["ends_at"]}, after={"minutes": minutes, "run": ext["id"], "ends_at": ext["ends_at"], "armed": bool(armed), "outcome": outcome},
                      ok=bool(armed), **actor)
        self.hub.publish("device", {"id": did, "state": self._norm(self.ha.state(d["entity"]["ha_target"])), "stale": self.ha.stale()})
        return {"outcome": outcome, "device_id": did, "action": "extend", "run": self._run_view(armed or ext) or self._run_view({**ext, "armed": False}),
                "observed": self._norm(self.ha.state(d["entity"]["ha_target"])), "waited_ms": 0}

    async def end_run(self, principal, did):
        """"Выключить": the plug is switched OFF and read back FIRST; only when it is really off the run is closed in the service. If the switch-off is
        unconfirmed the run stays: HA still ends it at its end."""
        d = self._run_device(principal, did)
        run = (await self.sched.active_runs(force=True)).get(did)
        if not run or not self._run_view(run):
            raise GwError(404, "no_run", "no timed run is active for this device")
        res = await self.switch(principal, did, False)
        if res["outcome"] == "confirmed":
            await self._close_active_run(principal, did)                         # idempotent: switch() already did it after a real OFF; this covers "was already off"
        self.db.audit("run_end", target=did, before={"run": run["id"]}, after={"outcome": res["outcome"], "observed": res["observed"]}, ok=res["outcome"] == "confirmed", **self._actor(principal))
        self.sched.drop_runs_cache()
        return {"outcome": res["outcome"], "device_id": did, "action": "end", "run": None, "observed": res["observed"], "waited_ms": res["waited_ms"]}

    # ── vacuums ──
    def vacuum_view(self, principal, vid):
        v = self.vacuums[vid]
        vc, sc = self.ha.get(v["vacuum"]), self.ha.get(v["script"])
        state = vc["state"] if vc else "unavailable"
        avail = state not in ("unavailable", "unknown")
        script_on = bool(sc and sc["state"] == "on")
        run = self.db.q1("SELECT * FROM vacuum_runs WHERE vacuum_id=?", (vid,))
        running = None
        if script_on:
            running = run["scenario"] if run and run["started"] and self.clock() - run["started"] < 6 * 3600 and run["scenario"] else "scheduled"
        busy = script_on or state in ("cleaning", "returning", "paused")
        last = None
        if run and run["last_result"]:
            last = "%s %s" % (run["last_result"], time.strftime("%H:%M", time.localtime(run["last_result_ts"])))
        return {"id": vid, "name": v["name"], "icon": v["icon"], "state": state, "battery": (vc["attrs"].get("battery_level") if vc else None), "available": avail,
                "stale": self.ha.stale() or vc is None, "running_scenario": running, "last_result": last,
                "scenarios": [{"id": sid, "name": s["name"], "description": s["description"]} for sid, s in v["scenarios"].items()],
                "schedule_id": v["schedule_id"], "can_start": principal["role"] in ("admin", "household") and avail and not busy and state == "docked" and not self.ha.stale()}

    def vacuums_view(self, principal):
        return [self.vacuum_view(principal, vid) for vid in self.vacuums]

    def _vac(self, principal, vid):
        if vid not in self.vacuums:
            raise GwError(404, "not_found", "no such vacuum", {"id": vid})
        if principal["role"] not in ("admin", "household"):
            raise GwError(403, "forbidden", "your role may not control the robots")
        return self.vacuums[vid]

    def _actor(self, principal):
        return dict(person=principal["person_id"], phone=principal.get("phone_id"), ip=principal.get("ip"))

    async def start_scenario(self, principal, vid, scenario):
        v = self._vac(principal, vid)
        if scenario not in v["scenarios"]:
            raise GwError(404, "not_found", "no such scenario", {"scenario": scenario})
        vs, ss = self.ha.state(v["vacuum"]), self.ha.state(v["script"])
        tgt = "%s/%s" % (vid, scenario)
        reason = None
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable; nothing was started")
        if vs in (None, "unavailable", "unknown"):
            reason = ("robot_unavailable", "the robot is unavailable")
        elif ss == "on":
            reason = ("busy", "a plan is already running")
        elif vs != "docked":
            reason = ("not_docked", "the robot is not on its dock (%s)" % vs)
        if reason:
            self.db.audit("vacuum_start", target=tgt, before={"state": vs, "script": ss}, after={"outcome": "refused", "reason": reason[0]}, ok=False, **self._actor(principal))
            raise GwError(409, reason[0], reason[1], {"state": vs, "script": ss})
        data = {"variables": v["scenarios"][scenario]["variables"]} if v["scenarios"][scenario]["variables"] else {}
        try:
            await self.ha.call("script", "turn_on", v["script"], data)
        except GwError as e:
            self.db.audit("vacuum_start", target=tgt, before={"state": vs, "script": ss}, after={"outcome": "failed", "error": e.code}, ok=False, **self._actor(principal))
            raise
        obs, waited = await self.ha.wait_state(v["script"], lambda s: s == "on", config.SWITCH_CONFIRM_S)
        outcome = "confirmed" if obs == "on" else "unconfirmed"
        if outcome == "confirmed":
            self.db.x("INSERT INTO vacuum_runs(vacuum_id,scenario,started,person) VALUES (?,?,?,?) ON CONFLICT(vacuum_id) DO UPDATE SET scenario=excluded.scenario, started=excluded.started, person=excluded.person",
                      (vid, scenario, self.clock(), principal["person_id"]))
        self.db.audit("vacuum_start", target=tgt, before={"state": vs, "script": ss}, after={"outcome": outcome, "script": obs}, ok=outcome == "confirmed", **self._actor(principal))
        return {"outcome": outcome, "vacuum_id": vid, "action": "start", "scenario": scenario, "observed": {"script": obs, "vacuum": self.ha.state(v["vacuum"])}, "waited_ms": int(waited * 1000)}

    async def pause(self, principal, vid):
        v = self._vac(principal, vid)
        vs = self.ha.state(v["vacuum"])
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs != "cleaning" and self.ha.state(v["script"]) == "on":
            # Found live 2026-10-20: the plan script is running but the robot has not left the dock yet (this robot reports "docked" for up to ~1 min
            # after the start). Pausing nothing would leave the plan running, so a pause here cancels the plan and sends the robot home (with the guard).
            res = await self.dock(principal, vid)
            res["action"] = "dock"
            return res
        if vs != "cleaning":
            raise GwError(409, "not_running", "the robot is not cleaning (%s)" % vs, {"state": vs})
        await self.ha.call("vacuum", "pause", v["vacuum"])
        if self.ha.state(v["script"]) == "on":
            self._note_stop(principal, v, "pause")
        obs, waited = await self.ha.wait_state(v["vacuum"], lambda s: s == "paused", config.VACUUM_CONFIRM_S)
        outcome = "confirmed" if obs == "paused" else "unconfirmed"
        self.db.audit("vacuum_pause", target=vid, before={"state": vs}, after={"outcome": outcome, "vacuum": obs}, ok=outcome == "confirmed", **self._actor(principal))
        return {"outcome": outcome, "vacuum_id": vid, "action": "pause", "scenario": None, "observed": {"vacuum": obs}, "waited_ms": int(waited * 1000)}

    async def resume(self, principal, vid):
        """Continue a PAUSED robot. Only vacuum.start on a robot HA reports as paused; the plan script that was running at the pause has ended by now
        (it waits for the robot to stop cleaning), so the robot finishes its current job by itself and returns to the dock on its own."""
        v = self._vac(principal, vid)
        vs = self.ha.state(v["vacuum"])
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs != "paused":
            raise GwError(409, "not_paused", "the robot is not paused (%s)" % vs, {"state": vs})
        old = self._guards.get(vid)
        if old and not old.done():
            old.cancel()                                                # a guard from an earlier stop must not send the resumed robot home again
        await self.ha.call("vacuum", "start", v["vacuum"])
        obs, waited = await self.ha.wait_state(v["vacuum"], lambda s: s == "cleaning", config.VACUUM_CONFIRM_S)
        outcome = "confirmed" if obs == "cleaning" else "unconfirmed"
        self.db.audit("vacuum_resume", target=vid, before={"state": vs}, after={"outcome": outcome, "vacuum": obs}, ok=outcome == "confirmed", **self._actor(principal))
        return {"outcome": outcome, "vacuum_id": vid, "action": "resume", "scenario": None, "observed": {"vacuum": obs}, "waited_ms": int(waited * 1000)}

    async def dock(self, principal, vid):
        v = self._vac(principal, vid)
        vs, ss = self.ha.state(v["vacuum"]), self.ha.state(v["script"])
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs in (None, "unavailable", "unknown"):
            raise GwError(409, "robot_unavailable", "the robot is unavailable")
        if ss == "on":
            self._note_stop(principal, v, "dock")
            await self.ha.call("script", "turn_off", v["script"])
        if vs not in ("docked",) or ss == "on":                       # a plan that was running may have a start command in flight even though HA still says docked
            await self.ha.call("vacuum", "return_to_base", v["vacuum"])
        if ss == "on" or vs == "cleaning":
            self._start_stop_guard(principal, vid, v)
        ok_states = ("returning", "docked")
        t0 = time.monotonic()
        obs_v, _ = await self.ha.wait_state(v["vacuum"], lambda s: s in ok_states, config.VACUUM_CONFIRM_S)
        obs_s, _ = await self.ha.wait_state(v["script"], lambda s: s == "off", max(0.5, config.VACUUM_CONFIRM_S - (time.monotonic() - t0)))
        outcome = "confirmed" if obs_v in ok_states and obs_s == "off" else "unconfirmed"
        if ss == "on" and outcome == "confirmed":
            self.db.x("INSERT INTO vacuum_runs(vacuum_id,last_result,last_result_ts) VALUES (?,?,?) ON CONFLICT(vacuum_id) DO UPDATE SET last_result='stopped', last_result_ts=excluded.last_result_ts", (vid, "stopped", self.clock()))
        self.db.audit("vacuum_dock", target=vid, before={"state": vs, "script": ss}, after={"outcome": outcome, "vacuum": obs_v, "script": obs_s}, ok=outcome == "confirmed", **self._actor(principal))
        return {"outcome": outcome, "vacuum_id": vid, "action": "dock", "scenario": None, "observed": {"vacuum": obs_v, "script": obs_s}, "waited_ms": int((time.monotonic() - t0) * 1000)}

    def _note_stop(self, principal, v, action):
        """A person stopped a running plan. The host-side alert watcher (ha-alerts) reads this small file so that a plan someone cancelled on purpose is a
        quiet line, not a 'plan did not finish' page. One JSON object per line, no secret in it; trimmed to the last 100 lines when it grows past 64 KB."""
        path = os.path.join(self.db.dir, "stops.jsonl")
        try:
            rec = {"ts": round(time.time(), 1), "script": v["script"], "person": principal["person_id"], "action": action}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if os.path.getsize(path) > 65536:
                keep = open(path, encoding="utf-8").read().splitlines()[-100:]
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + "\n")
        except OSError:
            pass                                                           # the marker is a courtesy to the alert watcher; never fail the stop because of it

    def _start_stop_guard(self, principal, vid, v):
        """After a stop of a running plan: for STOP_GUARD_S seconds, if the robot starts cleaning anyway (a command that was already on its way) and no new
        plan was started, send it home again (at most 3 times, 8 s apart). Every send is audited. A plan started on purpose ends the guard."""
        old = self._guards.get(vid)
        if old and not old.done():
            old.cancel()
        actor = self._actor(principal)

        async def run():
            end = time.monotonic() + config.STOP_GUARD_S
            sends, last = 0, -100.0
            while time.monotonic() < end:
                await asyncio.sleep(config.STOP_GUARD_POLL_S)
                if self.ha.stale():
                    continue
                if self.ha.state(v["script"]) == "on":
                    return
                if self.ha.state(v["vacuum"]) == "cleaning" and sends < 3 and time.monotonic() - last >= 8:
                    try:
                        await self.ha.call("vacuum", "return_to_base", v["vacuum"])
                        sends += 1
                        last = time.monotonic()
                        self.db.audit("vacuum_stop_guard", target=vid, after={"resent_return_to_base": sends}, **actor)
                    except GwError:
                        pass
        self._guards[vid] = asyncio.get_running_loop().create_task(run())

    # ── service tiles ──
    def tiles(self, principal):
        out = []
        for r in self.db.q("SELECT * FROM service_tiles ORDER BY position"):
            if r["visible_to"] == "admin" and principal["role"] != "admin":
                continue
            p = self.probes.get(r["id"], {})
            out.append({"id": r["id"], "name": r["name"], "icon": r["icon"], "url": r["url"], "badge": p.get("badge", "unknown"), "visible_to": r["visible_to"],
                        "last_ok": iso(p["last_ok"]) if p.get("last_ok") else None, "rtt_ms": p.get("rtt_ms"), "detail": p.get("detail")})
        return out

    @staticmethod
    def _http_probe(url):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "portal-gateway-probe"})
            with urllib.request.urlopen(req, timeout=3) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:                                                  # noqa: BLE001
            return None, int((time.monotonic() - t0) * 1000), e.__class__.__name__
        return code, int((time.monotonic() - t0) * 1000), None

    @staticmethod
    def _dns_probe(ip):
        t0 = time.monotonic()
        qid = int(time.time() * 1000) & 0xFFFF
        pkt = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            s.sendto(pkt, (ip, 53))
            data, _ = s.recvfrom(512)
            ok = len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid and (data[3] & 0x0F) == 0
            return ok, int((time.monotonic() - t0) * 1000), None if ok else "bad answer"
        except OSError as e:
            return False, int((time.monotonic() - t0) * 1000), e.__class__.__name__
        finally:
            s.close()

    async def probe_one(self, row):
        kind, _, target = row["probe"].partition(":")
        if kind == "http":
            code, rtt, err = await asyncio.to_thread(self._http_probe, target)
            if code is None:
                return {"badge": "down", "rtt_ms": rtt, "detail": err}
            if code >= 500:
                return {"badge": "degraded", "rtt_ms": rtt, "detail": "HTTP %d" % code, "last_ok": None}
            return {"badge": "degraded" if rtt > 1500 else "ok", "rtt_ms": rtt, "detail": "HTTP %d" % code, "last_ok": self.clock()}
        if kind == "dns":
            ok, rtt, err = await asyncio.to_thread(self._dns_probe, target)
            return {"badge": "ok" if ok else "down", "rtt_ms": rtt, "detail": None if ok else err, "last_ok": self.clock() if ok else None}
        if kind == "internal" and target == "ha":
            link = self.ha.link
            return {"badge": {"ws": "ok", "poll": "degraded", "down": "down"}[link], "rtt_ms": None, "detail": "link " + link, "last_ok": self.clock() if link != "down" else None}
        if kind == "internal" and target == "schedules":
            h = await self.sched.health()
            if h["link"] != "ok":
                return {"badge": "down", "rtt_ms": None, "detail": "schedule service unreachable"}
            if h.get("in_sync") is False:
                return {"badge": "degraded", "rtt_ms": None, "detail": "rendered config differs from the loaded one", "last_ok": self.clock()}
            return {"badge": "ok", "rtt_ms": None, "detail": "in sync" if h.get("in_sync") else None, "last_ok": self.clock()}
        return {"badge": "unknown", "rtt_ms": None, "detail": "no probe"}

    async def probe_all(self):
        for r in self.db.q("SELECT * FROM service_tiles"):
            res = await self.probe_one(r)
            old = self.probes.get(r["id"], {})
            res["last_ok"] = res.get("last_ok") or old.get("last_ok")
            if old.get("badge") != res["badge"]:
                self.hub.publish("service", {"id": r["id"], "badge": res["badge"]})
            self.probes[r["id"]] = res
        self.probe_ts = self.clock()

    async def probe_loop(self):
        while True:
            try:
                await self.probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:                                          # noqa: BLE001 - a probe bug must not stop the loop
                pass
            await asyncio.sleep(config.PROBE_EVERY_S)

    # ── capabilities ──
    def capabilities(self, principal):
        role, parent = principal["role"], principal["parent"]
        return {"person": {"id": principal["person_id"], "name": principal["name"], "role": role, "parent": parent},
                "tabs": ["home"] + (["parental"] if parent else []),
                "modules": dict(config.FEATURES),
                "limits": {"max_devices_total": config.MAX_PHONES_TOTAL, "max_devices_per_person": config.MAX_PHONES_PER_PERSON},
                "can": {"switch": role in ("admin", "household"), "edit_schedules": role in ("admin", "household"), "start_vacuum": role in ("admin", "household"),
                        "manage_people": role == "admin", "see_audit": True},
                "gateway": {"version": config.VERSION}}
