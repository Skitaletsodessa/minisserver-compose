#!/usr/bin/env python3
"""portal-watch (Task 27 Phase F): is the schedule service alive, honest, in sync with Home Assistant, and reachable only where it should be?

Runs every 5 min. All alerts go through watchnotify with a chosen tier (Task 26 rules):
  portal-exposure   A  the service or its Caddy site listens somewhere it must not (anything but 127.0.0.1:16444 / the tailnet address :16443)
  portal-audit-chain A the audit hash chain does not verify (tampering or corruption)
  portal-down       B  the service has not answered for 3 checks in a row (editing is unavailable; Home Assistant keeps running its loaded
                       schedules, so this is not an emergency of the house)
  portal-unsynced   B  the rendered schedule has not been applied/verified in Home Assistant for 15 min (the applier alerts on a failure
                       itself; this one covers "never ran / stuck")
  portal-publish    B  the service could not write its output files
  portal-watch-token B this watcher cannot read its read-only token (full checks skipped)
A missing run of this watcher itself is the 08:00 heartbeat's job (it is in its list).
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

URL = "http://127.0.0.1:16444"
TOKEN_FILE = "/home/skit/.config/portal-watch/token"
STATE_FILE = os.environ.get("PORTAL_WATCH_STATE", "/var/lib/watch-alerts/portal-watch.json")
DOWN_AFTER = 3
UNSYNCED_AFTER = 15 * 60


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
    res = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4:
            res.append(f[3])
    return res


def exposure_problems(addrs):
    """Every listener on our two ports must be the intended one."""
    bad = []
    for a in addrs:
        host, _, port = a.rpartition(":")
        if port == "16444" and host != "127.0.0.1":
            bad.append("%s (the service must listen on 127.0.0.1 only)" % a)
        if port == "16443" and not re.match(r"^100\.\d+\.\d+\.\d+$", host):
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


def evaluate(N, state, now, public, full, token_ok, addrs):
    """Pure decision logic. public: bool (the service answered), full: parsed /v1/health or None, addrs: listening sockets."""
    bad = exposure_problems(addrs)
    if bad:
        N.alert("portal-exposure", "portal: слушает не там, где должен: " + "; ".join(bad), tier="A")
    else:
        N.clear("portal-exposure", "portal: сокеты снова только loopback/tailnet")

    if public:
        state["down"] = 0
        N.clear("portal-down", "")
    else:
        state["down"] = state.get("down", 0) + 1
        if state["down"] >= DOWN_AFTER:
            N.alert("portal-down", "portal: сервис расписаний не отвечает уже %d проверок. Расписания не редактируются; Home Assistant "
                    "продолжает выполнять загруженные." % state["down"], tier="B")
        return state

    if not token_ok:
        N.alert("portal-watch-token", "portal-watch: не могу прочитать read-only токен (%s); полные проверки пропущены" % TOKEN_FILE, tier="B")
        return state
    N.clear("portal-watch-token", "")
    if full is None:
        return state

    if not full["audit_chain"]["ok"]:
        N.alert("portal-audit-chain", "portal: ЦЕПОЧКА АУДИТА НАРУШЕНА (первая плохая запись #%s). Это порча или подделка журнала изменений расписаний."
                % full["audit_chain"].get("first_bad_seq"), tier="A")
    else:
        N.clear("portal-audit-chain", "portal: цепочка аудита снова цела")
    if full.get("publish_error"):
        N.alert("portal-publish", "portal: сервис не может записать выходные файлы: %s" % full["publish_error"], tier="B")
    else:
        N.clear("portal-publish", "")

    if full["in_sync"] or not full["render"].get("set_hash"):
        state.pop("unsynced_since", None)
        N.clear("portal-unsynced", "")
    else:
        since = state.setdefault("unsynced_since", now)
        if now - since > UNSYNCED_AFTER:
            ap = full.get("apply", {})
            N.alert("portal-unsynced", "portal: расписание не применено в Home Assistant уже %d мин (результат: %s; %s)"
                    % ((now - since) // 60, ap.get("result"), (ap.get("detail") or "")[:120]), tier="B")
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
    st = evaluate(watchnotify, st, time.time(), public, full, token_ok, listeners())
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
