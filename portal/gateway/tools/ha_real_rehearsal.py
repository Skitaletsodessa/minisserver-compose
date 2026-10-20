#!/usr/bin/env python3
"""Rehearsal against a REAL Home Assistant (same image as production, --network none, template switches, no devices):
  1. the dedicated user is created with tools/ha_create_user.py exactly as it will be in production (HA not running),
  2. HA starts, the token works, and the user is NOT an admin (admin-only calls are refused),
  3. the gateway's own HA class (gw/ha.py) connects over the real WebSocket (subscribe_entities decoding), sees a change, calls a service through the
     allow-list and confirms it by read-back; the allow-list refuses a switch that is not on it.
Never prints the token. Run on the server:  /srv/apps/portal/venv-gw/bin/python tools/ha_real_rehearsal.py"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from gw.ha import HA  # noqa: E402
from gw.hub import Hub  # noqa: E402
from gw.auth import GwError  # noqa: E402

S = "/srv/apps/portal/scratch-ha-user"
NAME = "portal-ha-user-rehearsal"
RES = []


def check(ok, what, extra=""):
    RES.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", what, ("  -- " + extra) if extra else ""), flush=True)


def sh(*a, **kw):
    return subprocess.run(list(a), capture_output=True, text=True, **kw)


def dx(*a):
    return sh("docker", "exec", NAME, *a)


def http(method, path, token, body=None):
    """Runs INSIDE the container (it has no network namespace to the host): returns (status, json)."""
    code = ("import urllib.request as u, urllib.error as e, json, sys\n"
            "tok = open('/out/ha-token').read().strip()\n"
            "d = %r\n"
            "r = u.Request('http://127.0.0.1:8123' + %r, data=(json.dumps(d).encode() if d is not None else None), method=%r, headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})\n"
            "try:\n"
            "    x = u.urlopen(r, timeout=8); print(x.status); print(x.read().decode()[:2000])\n"
            "except e.HTTPError as x:\n"
            "    print(x.code); print(x.read().decode()[:200])\n") % (body, path, method)
    r = dx("python3", "-c", code)
    lines = r.stdout.split("\n", 1)
    try:
        return int(lines[0]), (json.loads(lines[1]) if len(lines) > 1 and lines[1].strip().startswith(("{", "[")) else lines[1] if len(lines) > 1 else None)
    except ValueError:
        return None, r.stdout + r.stderr


def main():
    img = sh("docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}").stdout.strip()
    sh("sudo", "rm", "-rf", S)
    os.makedirs(S + "/config", mode=0o700)
    os.makedirs(S + "/out", mode=0o700)
    names = ["bathroom_towels", "living_room_rack"]
    cfg = "homeassistant:\n  name: rehearsal\n  time_zone: Europe/Prague\nhttp:\n  server_host: 127.0.0.1\nwebsocket_api:\napi:\ninput_boolean:\n" + "".join("  %s:\n    name: %s\n" % (n, n) for n in names)
    cfg += "template:\n  - switch:\n" + "".join("      - name: %s\n        state: \"{{ is_state('input_boolean.%s', 'on') }}\"\n        turn_on: {service: input_boolean.turn_on, target: {entity_id: input_boolean.%s}}\n"
                                                "        turn_off: {service: input_boolean.turn_off, target: {entity_id: input_boolean.%s}}\n" % (n, n, n, n) for n in names)
    open(S + "/config/configuration.yaml", "w").write(cfg)
    try:
        # 0. a fresh HA has no owner: the tool must refuse (the first user would become the owner = full admin; found by this very rehearsal)
        r = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/config:/config", "-v", S + "/out:/out",
               "-v", os.path.join(HERE, "ha_create_user.py") + ":/ha_create_user.py:ro", img, "python", "/ha_create_user.py", "/config", "/out/ha-token")
        check(r.returncode == 3 and not os.path.exists(S + "/out/ha-token"), "with no owner present the tool refuses (the first user would become the owner)")
        owner = os.path.join(S, "mk_owner.py")
        open(owner, "w").write(MK_OWNER)
        r = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/config:/config", "-v", owner + ":/mk_owner.py:ro", img, "python", "/mk_owner.py", "/config")
        check(r.returncode == 0, "an owner user (stands for Ivan's account) exists", r.stderr.strip()[-150:] if r.returncode else "")
        # 1. create the user the production way (HA is not running here)
        r = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/config:/config", "-v", S + "/out:/out",
               "-v", os.path.join(HERE, "ha_create_user.py") + ":/ha_create_user.py:ro", img, "python", "/ha_create_user.py", "/config", "/out/ha-token")
        print("   create:", r.stdout.strip().replace("\n", " | ")[:200], r.stderr.strip()[-200:] if r.returncode else "")
        check(r.returncode == 0 and os.path.exists(S + "/out/ha-token"), "user and long-lived token created offline with HA's own AuthManager")
        mode = oct(os.stat(S + "/out/ha-token").st_mode & 0o777)
        check(mode == "0o600", "the token file is mode 600", mode)
        again = sh("docker", "run", "--rm", "--network", "none", "-v", S + "/config:/config", "-v", S + "/out:/out", "-v", os.path.join(HERE, "ha_create_user.py") + ":/ha_create_user.py:ro",
                   img, "python", "/ha_create_user.py", "/config", "/out/ha-token-2")
        check(again.returncode == 1 and not os.path.exists(S + "/out/ha-token-2"), "a second run is refused (no second token holder)")
        # 2. start HA, use the token
        sh("docker", "rm", "-f", "-v", NAME)
        r = sh("docker", "run", "-d", "--name", NAME, "--network", "none", "--memory", "1024m", "-e", "TZ=Europe/Prague", "-v", S + "/config:/config", "-v", S + "/out:/out:ro", img)
        assert r.returncode == 0, r.stderr
        end = time.time() + 240
        ok = False
        while time.time() < end:
            st, _ = http("GET", "/api/", None)
            if st == 200:
                ok = True
                break
            time.sleep(3)
        check(ok, "HA starts and accepts the token (GET /api/ -> 200)")
        for _ in range(60):                                       # the template entities appear a moment after the API answers
            st, body = http("GET", "/api/states/switch.living_room_rack", None)
            if st == 200 and isinstance(body, dict) and body.get("state") == "off":
                break
            time.sleep(1)
        check(st == 200 and isinstance(body, dict) and body.get("state") == "off", "the user can read states", "HTTP %s %s" % (st, str(body)[:150]))
        st, _ = http("POST", "/api/services/switch/turn_on", None, {"entity_id": "switch.living_room_rack"})
        check(st == 200, "the user can call switch.turn_on")
        time.sleep(1)
        st, body = http("GET", "/api/states/switch.living_room_rack", None)
        check(isinstance(body, dict) and body.get("state") == "on", "the call had an effect", str(body)[:150])
        for svc, label in (("homeassistant/restart", "homeassistant.restart"), ("automation/reload", "automation.reload"), ("homeassistant/reload_all", "reload_all")):
            st, _ = http("POST", "/api/services/" + svc, None, {})
            check(st in (401, 403), "admin-only call refused: " + label, "HTTP %s" % st)
        st, _ = http("GET", "/api/config/core/check_config", None)
        check(st in (401, 403, 404, 405), "admin-only REST endpoint refused: check_config", "HTTP %s" % st)
        # 3. the gateway's HA class against the real WebSocket: run it INSIDE the container network via docker exec? it needs the host to reach HA.
        #    --network none has no route, so the gateway class is exercised in the container using the image's python with the gw package mounted.
        script = os.path.join(HERE, "_ha_class_probe.py")
        open(script, "w").write(PROBE)
        r = sh("docker", "cp", script, NAME + ":/tmp/_probe.py")
        sh("docker", "cp", os.path.dirname(HERE) + "/gw", NAME + ":/tmp/gw")
        r = dx("sh", "-c", "cd /tmp && python3 /tmp/_probe.py")
        print(r.stdout)
        if r.stderr.strip():
            print("   stderr:", r.stderr.strip()[-400:])
        check("PROBE-OK" in r.stdout, "gateway HA class over the REAL websocket: subscribe, see a change, call through the allow-list, read-back confirms, a non-listed entity is refused")
    finally:
        sh("docker", "rm", "-f", "-v", NAME)
        sh("sudo", "rm", "-rf", S)
        try:
            os.remove(os.path.join(HERE, "_ha_class_probe.py"))
        except OSError:
            pass
    print("\n%d checks, %d failed" % (len(RES), RES.count(False)))
    sys.exit(1 if False in RES else 0)


MK_OWNER = r'''
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
    u = await hass.auth.async_create_user("owner-stand-in", group_ids=[GROUP_ID_ADMIN])
    assert u.is_owner
    st = hass.auth._store
    await st._store.async_save(st._data_to_save())

asyncio.run(main(sys.argv[1]))
'''

PROBE = r'''
import asyncio, sys
sys.path.insert(0, "/tmp")
from gw.ha import HA
from gw.auth import GwError

async def main():
    ha = HA("http://127.0.0.1:8123", "/out/ha-token")
    ha.set_scope({"switch.living_room_rack", "switch.bathroom_towels"}, {"switch.living_room_rack"})
    await ha.start()
    for _ in range(100):
        if ha.link == "ws":
            break
        await asyncio.sleep(0.2)
    print("link:", ha.link)
    assert ha.link == "ws", "websocket link not reached"
    assert ha.state("switch.living_room_rack") == "on", ha.state("switch.living_room_rack")
    await ha.call("switch", "turn_off", "switch.living_room_rack")
    obs, waited = await ha.wait_state("switch.living_room_rack", lambda s: s == "off", 6)
    print("read-back:", obs, "after %.2f s" % waited)
    assert obs == "off"
    try:
        await ha.call("switch", "turn_on", "switch.bathroom_towels")
        print("NOT REFUSED")
        return
    except GwError as e:
        assert e.code == "not_allowed"
    ev_before = ha.last_event
    await ha.call("switch", "turn_on", "switch.living_room_rack")
    obs, _ = await ha.wait_state("switch.living_room_rack", lambda s: s == "on", 6)
    assert obs == "on"
    print("PROBE-OK")
    await ha.stop()

asyncio.run(main())
'''

if __name__ == "__main__":
    main()
