#!/usr/bin/env python3
"""gateway-watch (Task 28 Phase I): is the portal gateway alive, honest, connected to what it needs, and reachable only where it should be?

Runs every 5 min. All alerts go through watchnotify with a chosen tier (Task 26 rules). The gateway is a convenience layer: when it is down NOTHING in the
house changes (Home Assistant runs the schedules by itself), so "down" is tier B, not A.
  gateway-exposure     A  the gateway or its Caddy site listens somewhere it must not (anything but 127.0.0.1:17444 / the tailnet address :17443)
  gateway-audit-chain  A  the gateway's audit hash chain does not verify (tampering or corruption of the record of who did what)
  gateway-down         B  no answer for 3 checks in a row (the web page and the app cannot switch anything; the house keeps running)
  gateway-degraded     B  it answers but cannot reach Home Assistant or the schedule service for 3 checks in a row (the page would show stale data)
  gateway-watch-token  B  this watcher cannot read / is refused with its own read-only token (detailed checks skipped)
Its own liveness is the 08:00 heartbeat's job (this unit is in its list).
The watcher's identity is the synthetic person `gw-watch` (role readonly) with ONE enrolled device; its token lives in
/home/skit/.config/gateway-watch/token (mode 600) and is never printed.
"""
import os
import re
import sys
import json
import time
import subprocess
import urllib.request
import urllib.error

sys.path.insert(0, "/srv/compose/_system/lib")

URL = os.environ.get("GATEWAY_WATCH_URL", "http://127.0.0.1:17444")
TOKEN_FILE = os.environ.get("GATEWAY_WATCH_TOKEN", "/home/skit/.config/gateway-watch/token")
STATE_FILE = os.environ.get("GATEWAY_WATCH_STATE", "/var/lib/watch-alerts/gateway-watch.json")
DOWN_AFTER = 3
DEGRADED_AFTER = 3


def http_json(path, token=None):
    req = urllib.request.Request(URL + path, headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def listeners():
    out = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True).stdout
    return [f[3] for f in (line.split() for line in out.splitlines()) if len(f) >= 4]


def exposure_problems(addrs):
    bad = []
    for a in addrs:
        host, _, port = a.rpartition(":")
        if port == "17444" and host != "127.0.0.1":
            bad.append("%s (the gateway must listen on 127.0.0.1 only)" % a)
        if port == "17443" and not re.match(r"^100\.\d+\.\d+\.\d+$", host):
            bad.append("%s (the Caddy site must listen on the tailnet address only)" % a)
    return bad


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def evaluate(N, state, public, full, token_ok, addrs):
    """Pure decision logic. public: the gateway answered /v1/health; full: parsed authenticated /v1/health or None; addrs: listening sockets."""
    bad = exposure_problems(addrs)
    if bad:
        N.alert("gateway-exposure", "gateway: слушает не там, где должен: " + "; ".join(bad), tier="A")
    else:
        N.clear("gateway-exposure", "gateway: сокеты снова только loopback/tailnet")

    if public:
        state["down"] = 0
        N.clear("gateway-down", "")
    else:
        state["down"] = state.get("down", 0) + 1
        if state["down"] >= DOWN_AFTER:
            N.alert("gateway-down", "gateway: портал не отвечает уже %d проверок. Страница и приложение ничего не переключают; в доме всё "
                    "продолжает работать по расписанию Home Assistant." % state["down"], tier="B")
        return state

    if not token_ok:
        N.alert("gateway-watch-token", "gateway-watch: токен наблюдателя нечитаем или отклонён (%s); подробные проверки пропущены" % TOKEN_FILE, tier="B")
        return state
    N.clear("gateway-watch-token", "")
    if full is None:
        return state

    if not full.get("audit_chain", {}).get("ok", True):
        N.alert("gateway-audit-chain", "gateway: ЦЕПОЧКА АУДИТА НАРУШЕНА. Это порча или подделка журнала «кто что включил».", tier="A")
    else:
        N.clear("gateway-audit-chain", "gateway: цепочка аудита снова цела")

    # Task 29: an AdGuard filter-list change that was reverted automatically (the safety net did its job - Ivan should know once, nothing is broken). The gateway lists
    # such changes of the last 48 h in /v1/health; one message per change id, none repeated, cleared when the id leaves the list.
    now_ids = {r["id"]: r for r in full.get("filter_reverts") or []}
    for fid, r in now_ids.items():
        if fid not in state.get("fl_alerted", []):
            N.alert("gateway-filter-revert-" + fid, "AdGuard: изменение списка «%s» (%s) откатилось автоматически: %s. Ничего не сломано - списки такие же, как до попытки."
                    % (r.get("list_name") or "?", r.get("action"), (r.get("reason") or "")[:300]), tier="B")
    for fid in state.get("fl_alerted", []):
        if fid not in now_ids:
            N.clear("gateway-filter-revert-" + fid, "")
    state["fl_alerted"] = sorted(now_ids)

    if full.get("status") == "degraded":
        state["degraded"] = state.get("degraded", 0) + 1
        if state["degraded"] >= DEGRADED_AFTER:
            ha, sc = full.get("ha", {}).get("link"), full.get("schedule_service", {}).get("link")
            N.alert("gateway-degraded", "gateway: отвечает, но связь с Home Assistant: %s, с сервисом расписаний: %s. Страница показывает устаревшие данные."
                    % (ha, sc), tier="B")
    else:
        state["degraded"] = 0
        N.clear("gateway-degraded", "")
    return state


def main():
    import watchnotify
    st = load_state()
    s, pub = http_json("/v1/health")
    public = s == 200 and pub is not None
    full, token_ok = None, True
    if public:
        try:
            tok = open(TOKEN_FILE).read().strip()
        except OSError:
            token_ok, tok = False, None
        if tok:
            s2, full = http_json("/v1/health", tok)
            if s2 == 401:
                token_ok = False
    st = evaluate(watchnotify, st, public, full, token_ok, listeners())
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
