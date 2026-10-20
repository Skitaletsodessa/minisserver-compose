#!/usr/bin/env python3
"""Resilience proof against a REAL Home Assistant, without touching production (Task 28 Phase J). Run on the server:
    python3 tools/resilience_scratch.py
It builds a throw-away stack on its own docker network: the production HA image (template switches named like the six real plugs, no devices), the
production schedule-service image (real seed), and the production gateway image. Then it checks, with timestamps:
  1. baseline: link ws, six devices, a switch call confirmed by read-back;
  2. HA RESTARTED (docker restart): the gateway reports stale data and refuses to switch (503 ha_unavailable), an open event stream says so,
     and everything recovers by itself when HA is back;
  3. HA HUNG (docker pause: it accepts nothing and answers nothing): same expectations, then recovery after unpause;
  4. the gateway's non-admin HA user is refused an admin service in this HA too.
Never prints a token. Cleans everything up at the end (also on failure)."""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
S = "/srv/apps/portal/scratch-resilience"
NET = "rs-net"
IMG_HA = subprocess.run(["docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}"], capture_output=True, text=True).stdout.strip()
IMG_GW = "portal-gateway:local"
IMG_SCHED = "portal-schedules:local"
RES = []
T0 = time.time()


def log(*a):
    print("%6.1fs" % (time.time() - T0), *a, flush=True)


def check(ok, what, extra=""):
    RES.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", what, ("  -- " + extra) if extra else ""), flush=True)


def sh(*a, **kw):
    return subprocess.run(list(a), capture_output=True, text=True, **kw)


SWITCHES = ["bathroom_towels", "living_room_rack", "livingroom_rack_big", "kitchen_orchids", "minisserver", "bedroom_parrot"]

DRIVER = r'''
import json, sys, time, urllib.request, urllib.error
BASE = "http://127.0.0.1:17444"
TOK = "/tmp/tok"
def call(method, path, token=None, body=None, timeout=15):
    h = {}
    if token: h["Authorization"] = "Bearer " + token
    d = None
    if body is not None:
        d = json.dumps(body).encode(); h["Content-Type"] = "application/json"
    if method == "POST": h["Idempotency-Key"] = "rs-%d" % (time.time() * 1000)
    r = urllib.request.Request(BASE + path, data=d, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.status, json.loads(x.read() or "null")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, None
    except Exception as e:
        return None, str(e)[:80]
cmd = sys.argv[1]
if cmd == "enrol":
    st, b = call("POST", "/v1/enrol", None, {"code": sys.argv[2], "device": {"label": "resilience probe", "model": "script"}})
    open(TOK, "w").write(b["token"]); print(st)
elif cmd == "snap":
    t = open(TOK).read().strip()
    st, h = call("GET", "/v1/health", t)
    st2, d = call("GET", "/v1/devices", t)
    devs = d["devices"] if isinstance(d, dict) else []
    out = {"health": st, "ha_link": (h or {}).get("ha", {}).get("link") if isinstance(h, dict) else None, "status": (h or {}).get("status") if isinstance(h, dict) else None,
           "devices": len(devs), "stale": sum(1 for x in devs if x.get("stale")), "states": sorted({x.get("state") for x in devs})}
    print(json.dumps(out))
elif cmd == "switch":
    t = open(TOK).read().strip()
    st, b = call("POST", "/v1/devices/plant_rack1/switch", t, {"on": sys.argv[2] == "on"}, timeout=30)
    print(json.dumps({"http": st, "code": (b or {}).get("error", {}).get("code") if isinstance(b, dict) and "error" in b else None, "outcome": (b or {}).get("outcome") if isinstance(b, dict) else None,
                      "observed": (b or {}).get("observed") if isinstance(b, dict) else None}))
elif cmd == "sse":
    t = open(TOK).read().strip(); secs = float(sys.argv[2]); end = time.time() + secs
    r = urllib.request.Request(BASE + "/v1/events", headers={"Authorization": "Bearer " + t})
    try:
        x = urllib.request.urlopen(r, timeout=secs)
        ev = None
        while time.time() < end:
            line = x.readline().decode().strip()
            if line.startswith("event:"): ev = line[6:].strip()
            elif line.startswith("data:") and ev in ("link", "device", "vacuum", "service"):
                print("%.1f %s %s" % (time.time(), ev, line[5:80]), flush=True)
    except Exception as e:
        print("sse ended:", str(e)[:60])
'''


def drv(*args, stdin=DRIVER, timeout=60):
    r = subprocess.run(["docker", "exec", "-i", "rs-gateway", "python", "-"] + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip()


def snap():
    out, err = drv("snap")
    try:
        return json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (out + err)[-120:]}


def wait_for(pred, secs, every=2.0):
    end = time.time() + secs
    last = None
    while time.time() < end:
        last = snap()
        if pred(last):
            return last, time.time()
        time.sleep(every)
    return last, None


def cleanup():
    for c in ("rs-gateway", "rs-sched", "rs-ha", "rs-gw"):
        sh("docker", "rm", "-f", "-v", c)
    sh("docker", "network", "rm", NET)
    sh("sudo", "rm", "-rf", S)


OWNER = r'''
import asyncio, sys
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
async def main(cfg):
    hass = HomeAssistant(cfg)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await asyncio.gather(dr.async_load(hass), er.async_load(hass))
    hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    await hass.auth.async_create_user("owner-stand-in", group_ids=[GROUP_ID_ADMIN])
    st = hass.auth._store
    await st._store.async_save(st._data_to_save())
asyncio.run(main(sys.argv[1]))
'''


def main():
    cleanup()
    for d in ("ha-config", "ha-out", "gw-data", "gw-secrets", "sched-data"):
        os.makedirs(os.path.join(S, d), mode=0o700)
    cfg = "homeassistant:\n  name: resilience\n  time_zone: Europe/Prague\nhttp:\n  server_host: 0.0.0.0\napi:\nwebsocket_api:\ninput_boolean:\n"
    cfg += "".join("  %s:\n    name: %s\n" % (n, n) for n in SWITCHES) + "template:\n  - switch:\n"
    cfg += "".join("      - name: %s\n        state: \"{{ is_state('input_boolean.%s', 'on') }}\"\n        turn_on: {service: input_boolean.turn_on, target: {entity_id: input_boolean.%s}}\n"
                   "        turn_off: {service: input_boolean.turn_off, target: {entity_id: input_boolean.%s}}\n" % (n, n, n, n) for n in SWITCHES)
    open(S + "/ha-config/configuration.yaml", "w").write(cfg)
    open(S + "/owner.py", "w").write(OWNER)
    try:
        # HA user, as in production
        r = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/ha-config:/config", "-v", S + "/owner.py:/owner.py:ro", IMG_HA, "python", "/owner.py", "/config")
        check(r.returncode == 0, "scratch HA: owner stand-in created", r.stderr[-120:])
        r = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/ha-config:/config", "-v", S + "/ha-out:/out", "-v", HERE + "/ha_create_user.py:/ha_create_user.py:ro",
               IMG_HA, "python", "/ha_create_user.py", "/config", "/out/ha-token")
        check(r.returncode == 0, "scratch HA: dedicated non-admin user + token created by the production tool")
        sh("sudo", "chown", "-R", "1000:1000", S + "/ha-out")
        sh("docker", "network", "create", NET)
        sh("docker", "run", "-d", "--name", "rs-ha", "--network", NET, "--memory", "1024m", "-e", "TZ=Europe/Prague", "-v", S + "/ha-config:/config", IMG_HA)
        # netns holder for the gateway + schedule service (the schedule service listens on loopback only, like in production)
        sh("docker", "run", "-d", "--name", "rs-gw", "--network", NET, "--entrypoint", "sleep", IMG_GW, "infinity")
        sh("docker", "run", "-d", "--name", "rs-sched", "--network", "container:rs-gw", "-e", "TZ=Europe/Prague", "-e", "PORTAL_BIND=127.0.0.1", "-e", "PORTAL_PORT=16444",
           "-e", "PORTAL_DATA=/data", "-u", "1000:1000", "--read-only", "--tmpfs", "/tmp:size=16m", "-v", S + "/sched-data:/data", IMG_SCHED)
        time.sleep(6)
        for name, role, out in (("gw-admin", "admin", "sched-admin"), ("gw-household", "household", "sched-household")):
            r = sh("docker", "exec", "rs-sched", "python", "-m", "sched.admin", "token-create", name, role, "/data/out/" + out, "--gateway")
            check(r.returncode == 0, "scratch schedule service: token %s created" % name)
            os.rename(S + "/sched-data/out/" + out, S + "/gw-secrets/" + out)
        os.rename(S + "/ha-out/ha-token", S + "/gw-secrets/ha-token")
        for f in os.listdir(S + "/gw-secrets"):
            os.chmod(S + "/gw-secrets/" + f, 0o600)
        r = sh("docker", "run", "-d", "--name", "rs-gateway", "--network", "container:rs-gw", "-u", "1000:1000", "--read-only", "--tmpfs", "/tmp:size=16m", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--memory", "256m", "-e", "GW_BIND=127.0.0.1", "-e", "GW_PORT=17444", "-e", "GW_DATA=/data", "-e", "GW_HA_URL=http://rs-ha:8123",
               "-e", "GW_SCHED_URL=http://127.0.0.1:16444", "-v", S + "/gw-data:/data", "-v", S + "/gw-secrets:/secrets:ro", IMG_GW)
        check(r.returncode == 0, "scratch gateway started (loopback only inside its own network namespace)")
        # a person with a device token
        time.sleep(5)
        sh("docker", "exec", "rs-gateway", "python", "-m", "gw.ctl", "person", "add", "res-probe", "household")
        sh("docker", "exec", "rs-gateway", "python", "-m", "gw.ctl", "enrol-code", "res-probe")
        code = open(S + "/gw-data/out/enrol-res-probe.txt").read().strip()
        out, err = drv("enrol", code)
        check(out.strip() == "201", "scratch: device enrolled", out + err)
        log("waiting for the real HA to start and the gateway to connect ...")
        s, t = wait_for(lambda s: s.get("ha_link") == "ws" and s.get("devices") == 6 and s.get("stale") == 0, 300, 5)
        check(bool(t), "1. baseline: link ws, 6 devices, nothing stale, against the REAL HA", json.dumps(s))
        out, _ = drv("switch", "on")
        j = json.loads(out.splitlines()[-1])
        check(j.get("http") == 200 and j.get("outcome") == "confirmed", "1. switch confirmed by read-back on the real HA", out)
        time.sleep(2.2)                                           # the debounce (1.5 s) would answer 429 too_fast
        out, _ = drv("switch", "off")
        check('"confirmed"' in out, "1. and back off, confirmed", out)

        # event stream watcher
        sse_out = []
        def sse():
            o, _e = drv("sse", "260", timeout=300)
            sse_out.extend(o.splitlines())
        th = threading.Thread(target=sse, daemon=True)
        th.start()
        time.sleep(3)

        # 2. HA restarted
        log("RESTART of the real HA (docker restart rs-ha)")
        t_restart = time.time()
        threading.Thread(target=lambda: sh("docker", "restart", "-t", "20", "rs-ha"), daemon=True).start()
        s, t_stale = wait_for(lambda s: s.get("ha_link") == "down" or s.get("stale", 0) > 0 or s.get("health") != 200, 120, 1.0)
        check(bool(t_stale), "2. HA restarted: the gateway notices (stale or link down) after %.0f s" % ((t_stale or 0) - t_restart), json.dumps(s))
        out, _ = drv("switch", "on")
        j = json.loads(out.splitlines()[-1])
        check(j.get("http") == 503 or j.get("outcome") == "unconfirmed", "2. while HA is away a switch is refused (503) or honestly reported unconfirmed, never faked as done", out)
        s, t_back = wait_for(lambda s: s.get("ha_link") == "ws" and s.get("devices") == 6 and s.get("stale") == 0, 300, 3)
        check(bool(t_back), "2. HA back: the gateway recovered by itself after %.0f s (link ws, nothing stale)" % ((t_back or 0) - t_restart), json.dumps(s))
        out, _ = drv("switch", "on")
        check('"confirmed"' in out, "2. a switch works again and is confirmed", out)

        # 3. HA hung
        log("HANG of the real HA (docker pause rs-ha)")
        sh("docker", "pause", "rs-ha")
        t_pause = time.time()
        s, t_stale2 = wait_for(lambda s: s.get("ha_link") == "down" or s.get("stale", 0) > 0 or s.get("health") != 200, 150, 2)
        check(bool(t_stale2) and t_stale2 - t_pause < 75, "3. HA hung: the gateway notices after %.0f s (must be under 75 s)" % ((t_stale2 or 0) - t_pause), json.dumps(s))
        time.sleep(2)
        out, _ = drv("switch", "off", timeout=90)
        j = json.loads(out.splitlines()[-1])
        check(j.get("http") == 503 or j.get("outcome") == "unconfirmed", "3. a switch while HA hangs is refused or honestly 'unconfirmed'", out)
        sh("docker", "unpause", "rs-ha")
        t_un = time.time()
        s, t_back2 = wait_for(lambda s: s.get("ha_link") == "ws" and s.get("devices") == 6 and s.get("stale") == 0, 240, 3)
        check(bool(t_back2), "3. HA responsive again: recovered after %.0f s" % ((t_back2 or 0) - t_un), json.dumps(s))
        th.join(timeout=290)
        links = []
        for l in sse_out:
            parts = l.split(" ", 2)
            if len(parts) == 3 and parts[1] == "link":
                try:
                    links.append(json.loads(parts[2] if parts[2].endswith("}") else parts[2] + "}").get("ha"))
                except ValueError:
                    links.append(parts[2][:30])
        log("event stream, link events in order: %s ; other events: %d" % (links, sum(1 for l in sse_out if " link " not in l)))
        check(any(x in ("down", "poll") for x in links), "the open event stream carried a 'link' event saying HA was away", str(links))
        check(bool(links) and links[-1] == "ws", "and the last link event says ws again (recovered)", str(links))

        # 4. non-admin in this HA too (the token is read INSIDE the container from the mounted file, never put on a command line)
        code_ = ("import urllib.request as u, urllib.error as e\n"
                 "tok = open('/secrets/ha-token').read().strip()\n"
                 "r = u.Request('http://rs-ha:8123/api/services/homeassistant/check_config', data=b'{}', method='POST', headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})\n"
                 "try:\n    u.urlopen(r, timeout=15); print('NOT REFUSED')\nexcept e.HTTPError as x:\n    print('refused', x.code)\n")
        out, _ = (lambda r: (r.stdout.strip(), r.stderr))(subprocess.run(["docker", "exec", "rs-gateway", "python", "-c", code_], capture_output=True, text=True))
        check(out.startswith("refused"), "4. the gateway's HA user is refused an admin-only service", out)
    finally:
        cleanup()
    print("\n%d checks, %d failed" % (len(RES), RES.count(False)))
    sys.exit(1 if False in RES else 0)


if __name__ == "__main__":
    main()
