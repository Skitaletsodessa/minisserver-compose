"""Task 32: plan-less robot (ETA Tiger) card in the gateway, behind a feature-flag file. Applied to a SCRATCH copy of the gateway tree (argv[1]); asserts every anchor."""
import sys

ROOT = sys.argv[1]


def patch(rel, pairs):
    p = ROOT + "/" + rel
    s = open(p, encoding="utf-8").read()
    for old, new in pairs:
        assert s.count(old) == 1, (rel, old[:60], s.count(old))
        s = s.replace(old, new)
    open(p, "w", encoding="utf-8").write(s)


# ── config ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
patch("gw/config.py", [
    ("import os\n", "import json\nimport os\nimport re\n"),
    ('HA_ALLOWED_SERVICES = {"switch": {"turn_on", "turn_off"}, "script": {"turn_on", "turn_off"}, "vacuum": {"pause", "start", "return_to_base"}}',
     'HA_ALLOWED_SERVICES = {"switch": {"turn_on", "turn_off"}, "script": {"turn_on", "turn_off"}, "vacuum": {"pause", "start", "return_to_base", "locate"}}'),
    ("# keyword -> icon key", '''# Task 32: robots WITHOUT a plan script (the ETA Tiger). They exist only when the operator writes the feature-flag file (default: <data>/robots.json, or $GW_ROBOTS_FILE):
#   {"tiger": {"enabled": true, "vacuum": "vacuum.<entity id of the robot in Home Assistant>"}}
# No plan script, no schedule, no scenario beyond one plain "start" made by a person; the card also gets a "locate" button.
_VACUUM_ENTITY = re.compile(r"^vacuum\\.[a-z0-9_]{1,64}$")


def vacuum_seed(data_dir=None):
    out = dict(VACUUM_SEED)
    path = os.environ.get("GW_ROBOTS_FILE") or os.path.join(data_dir or DATA, "robots.json")
    try:
        with open(path, encoding="utf-8") as f:
            flag = json.load(f)
    except (OSError, ValueError):
        return out
    t = flag.get("tiger") if isinstance(flag, dict) else None
    if isinstance(t, dict) and t.get("enabled") is True and isinstance(t.get("vacuum"), str) and _VACUUM_ENTITY.match(t["vacuum"]):
        out["tiger"] = {"name": "Пылесос — ETA Tiger", "vacuum": t["vacuum"], "script": None, "schedule_id": None, "icon": "vacuum", "can_locate": True,
                        "scenarios": {"clean": {"name": "Уборка", "description": "Обычная уборка. Запускает только человек: расписаний у этого робота нет.", "variables": {}}}}
    return out


# keyword -> icon key'''),
])

# ── core ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
patch("gw/core.py", [
    ('''        for vid, v in config.VACUUM_SEED.items():
            self.vacuums[vid] = v
            watch |= {v["vacuum"], v["script"]}
            ctl |= {v["vacuum"], v["script"]}
            self.eid_vacuum[v["vacuum"]] = vid
            self.eid_vacuum[v["script"]] = vid
''', '''        for vid, v in config.vacuum_seed(self.db.dir).items():
            self.vacuums[vid] = v
            ents_v = {v["vacuum"]} | ({v["script"]} if v.get("script") else set())      # a robot without a plan script (Task 32) has no script entity
            watch |= ents_v
            ctl |= ents_v
            for eid_v in ents_v:
                self.eid_vacuum[eid_v] = vid
'''),
    ('''            if eid == self.vacuums[vid]["script"] and old and old["state"] == "on" and new["state"] == "off":''',
     '''            if self.vacuums[vid].get("script") and eid == self.vacuums[vid]["script"] and old and old["state"] == "on" and new["state"] == "off":'''),
    ('''"script": self.ha.state(self.vacuums[vid]["script"])})''', '''"script": self._ss(self.vacuums[vid])})'''),
    ('''    @staticmethod
    def _norm(s):''', '''    def _ss(self, v):
        """State of the robot's plan script; None for a robot that has none."""
        return self.ha.state(v["script"]) if v.get("script") else None

    @staticmethod
    def _norm(s):'''),
    ('''        vc, sc = self.ha.get(v["vacuum"]), self.ha.get(v["script"])
''', '''        vc, sc = self.ha.get(v["vacuum"]), (self.ha.get(v["script"]) if v.get("script") else None)
'''),
    ('''                "schedule_id": v["schedule_id"], "can_start":''', '''                "schedule_id": v["schedule_id"], "can_locate": bool(v.get("can_locate")) and principal["role"] in ("admin", "household") and avail and not self.ha.stale(), "can_start":'''),
    ('''        vs, ss = self.ha.state(v["vacuum"]), self.ha.state(v["script"])
        tgt = "%s/%s" % (vid, scenario)''', '''        if not v.get("script"):
            return await self._start_direct(principal, vid, v, scenario)
        vs, ss = self.ha.state(v["vacuum"]), self.ha.state(v["script"])
        tgt = "%s/%s" % (vid, scenario)'''),
    ('''        if vs != "cleaning" and self.ha.state(v["script"]) == "on":''', '''        if vs != "cleaning" and self._ss(v) == "on":'''),
    ('''        await self.ha.call("vacuum", "pause", v["vacuum"])
        if self.ha.state(v["script"]) == "on":''', '''        await self.ha.call("vacuum", "pause", v["vacuum"])
        if self._ss(v) == "on":'''),
    ('''        vs, ss = self.ha.state(v["vacuum"]), self.ha.state(v["script"])
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs in (None, "unavailable", "unknown"):
            raise GwError(409, "robot_unavailable", "the robot is unavailable")
        if ss == "on":''', '''        vs, ss = self.ha.state(v["vacuum"]), self._ss(v)
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs in (None, "unavailable", "unknown"):
            raise GwError(409, "robot_unavailable", "the robot is unavailable")
        if ss == "on":'''),
    ('''        obs_s, _ = await self.ha.wait_state(v["script"], lambda s: s == "off", max(0.5, config.VACUUM_CONFIRM_S - (time.monotonic() - t0)))''',
     '''        if v.get("script"):
            obs_s, _ = await self.ha.wait_state(v["script"], lambda s: s == "off", max(0.5, config.VACUUM_CONFIRM_S - (time.monotonic() - t0)))
        else:
            obs_s = "off"                                                # no plan script: nothing to wait for'''),
    ('''                if self.ha.state(v["script"]) == "on":
                    return''', '''                if self._ss(v) == "on":
                    return'''),
    ('''    def _note_stop(self, principal, v, action):''', '''    async def _start_direct(self, principal, vid, v, scenario):
        """Task 32: start a robot that has no plan script: vacuum.start by a person, only from the dock, confirmed by the robot reporting "cleaning"."""
        vs = self.ha.state(v["vacuum"])
        tgt = "%s/%s" % (vid, scenario)
        reason = None
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable; nothing was started")
        if vs in (None, "unavailable", "unknown"):
            reason = ("robot_unavailable", "the robot is unavailable")
        elif vs != "docked":
            reason = ("not_docked", "the robot is not on its dock (%s)" % vs)
        if reason:
            self.db.audit("vacuum_start", target=tgt, before={"state": vs}, after={"outcome": "refused", "reason": reason[0]}, ok=False, **self._actor(principal))
            raise GwError(409, reason[0], reason[1], {"state": vs, "script": None})
        try:
            await self.ha.call("vacuum", "start", v["vacuum"])
        except GwError as e:
            self.db.audit("vacuum_start", target=tgt, before={"state": vs}, after={"outcome": "failed", "error": e.code}, ok=False, **self._actor(principal))
            raise
        obs, waited = await self.ha.wait_state(v["vacuum"], lambda s: s == "cleaning", config.VACUUM_CONFIRM_S)
        outcome = "confirmed" if obs == "cleaning" else "unconfirmed"
        self.db.audit("vacuum_start", target=tgt, before={"state": vs}, after={"outcome": outcome, "vacuum": obs}, ok=outcome == "confirmed", **self._actor(principal))
        return {"outcome": outcome, "vacuum_id": vid, "action": "start", "scenario": scenario, "observed": {"vacuum": obs}, "waited_ms": int(waited * 1000)}

    async def locate(self, principal, vid):
        """Make the robot beep. There is no read-back for it, so the honest outcome is always 'unconfirmed' (the command was accepted by Home Assistant)."""
        v = self._vac(principal, vid)
        if not v.get("can_locate"):
            raise GwError(404, "not_found", "this robot cannot be located from here", {"id": vid})
        vs = self.ha.state(v["vacuum"])
        if self.ha.stale():
            raise GwError(503, "ha_unavailable", "Home Assistant is not reachable")
        if vs in (None, "unavailable", "unknown"):
            raise GwError(409, "robot_unavailable", "the robot is unavailable")
        await self.ha.call("vacuum", "locate", v["vacuum"])
        self.db.audit("vacuum_locate", target=vid, before={"state": vs}, after={"outcome": "sent"}, ok=True, **self._actor(principal))
        return {"outcome": "unconfirmed", "vacuum_id": vid, "action": "locate", "scenario": None, "observed": {"vacuum": self.ha.state(v["vacuum"])}, "waited_ms": 0}

    def _note_stop(self, principal, v, action):'''),
])

# ── api ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
patch("gw/api.py", [
    ('''            if action == "pause":
                return 200, await self.core.pause(p, vid)
''', '''            if action == "pause":
                return 200, await self.core.pause(p, vid)
            if action == "locate":
                return 200, await self.core.locate(p, vid)
'''),
    ('''    async def vac_dock(self, request):
        return await self.vac_action(request, "dock")
''', '''    async def vac_dock(self, request):
        return await self.vac_action(request, "dock")

    async def vac_locate(self, request):
        return await self.vac_action(request, "locate")
'''),
    ('''        Route("/v1/vacuums/{id}/dock", a.vac_dock, methods=["POST"]),
''', '''        Route("/v1/vacuums/{id}/dock", a.vac_dock, methods=["POST"]),
        Route("/v1/vacuums/{id}/locate", a.vac_locate, methods=["POST"]),
'''),
])

# ── UI ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
patch("gw/strings.py", [('"dock": "На базу",', '"dock": "На базу", "locate": "Найти",')])
patch("gw/templates/home.html", [('''      <button data-action="vac-dock"''', '''      {% if v.can_locate %}<button data-action="vac-locate">{{ t("locate") }}</button>{% endif %}
      <button data-action="vac-dock"''')])
patch("gw/static/app.js", [('''(act === "vac-pause" ? "/pause" : act === "vac-resume" ? "/resume" : "/dock")''', '''(act === "vac-pause" ? "/pause" : act === "vac-resume" ? "/resume" : act === "vac-locate" ? "/locate" : "/dock")''')])

# ── contract ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
patch("openapi.yaml", [
    ('''  /v1/schedules/{id}:
    get:
      operationId: getSchedule''', '''  /v1/vacuums/{id}/locate:
    post:
      operationId: locateVacuum
      summary: "Makes a robot that supports it beep (Task 32, only robots enabled by the robots.json feature flag). There is no read-back: the outcome is always unconfirmed"
      parameters: [{$ref: "#/components/parameters/Id"}, {$ref: "#/components/parameters/Idem"}]
      responses:
        "200": {description: result, content: {application/json: {schema: {$ref: "#/components/schemas/VacuumResult"}}}}
        "401": {$ref: "#/components/responses/Error"}
        "429": {$ref: "#/components/responses/Error"}
        "422": {$ref: "#/components/responses/Error"}
        "502": {$ref: "#/components/responses/Error"}
        "403": {$ref: "#/components/responses/Error"}
        "404": {$ref: "#/components/responses/Error"}
        "409": {$ref: "#/components/responses/Error"}
        "503": {$ref: "#/components/responses/Error"}
  /v1/schedules/{id}:
    get:
      operationId: getSchedule'''),
    ("        can_start: {type: boolean}\n    VacuumResult:", "        can_start: {type: boolean}\n        can_locate: {type: boolean}\n    VacuumResult:"),
    ("        action: {type: string, enum: [start, pause, resume, dock]}", "        action: {type: string, enum: [start, pause, resume, dock, locate]}"),
])
print("patched", ROOT)
