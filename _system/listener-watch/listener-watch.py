#!/usr/bin/env python3
"""listener-watch (Task 27a item 3): a permanent watch on EVERY non-loopback listening socket of this machine.

Every 5 minutes it reads `ss -tulnpH`, drops loopback listeners, and compares the rest with allowlist.conf (next to this file, in git),
where each expected listener has a reason. It depends on neither Home Assistant nor the portal.
  * a listener that matches no allow-list entry            -> tier A, one alert per listener (Task 26 tiers: immediate, a reminder every 6 h while it
                                                              lasts, one recovery message when it is gone)
  * an expected (fixed-port) listener that is not there    -> tier C: a quiet line in the 08:00 heartbeat, no message
  * an entry with `until=YYYY-MM-DD` that is past          -> stops being allowed (a known fault that was meant to be fixed by then)

allowlist.conf fields (whitespace separated, `#` starts a comment):  PROTO  ADDRESS  PORT  PROCESS  [until=DATE]  REASON...
  PROTO tcp|udp; ADDRESS an IP, `*` (any address: 0.0.0.0, [::], *), or `lan`/`tailnet` classes are NOT used - addresses are exact;
  PORT a number or `ephemeral` (>= 32768, the port changes at every start: matched by protocol + address + process only);
  PROCESS the owning process name as ss prints it (`-` = do not care).
"""
import os
import re
import sys
import hashlib
import json
import time
import subprocess
import datetime

sys.path.insert(0, "/srv/compose/_system/lib")
HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWLIST = os.environ.get("LISTENER_ALLOWLIST", os.path.join(HERE, "allowlist.conf"))
EPHEMERAL_MIN = 32768


def parse_ss(text):
    """-> list of dicts {proto, addr, port, proc}. Loopback is NOT dropped here (see is_loopback)."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 5 or f[0] not in ("tcp", "udp"):
            continue
        local = f[4]
        host, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        host = re.sub(r"%.*$", "", host).strip("[]")   # [fe80::...]%eno1 -> fe80::...
        if host in ("0.0.0.0", "::", "*"):
            host = "*"
        m = re.search(r'\(\("([^"]+)"', line)
        out.append({"proto": f[0], "addr": host, "port": int(port), "proc": m.group(1) if m else "?"})
    return out


def is_loopback(addr):
    return addr == "::1" or addr.startswith("127.")


def normalise(listeners):
    """Drop loopback; the 'wildcard' forms of one socket pair (0.0.0.0 and ::) stay separate entries but share the key (proto, '*', port, proc)."""
    seen, out = set(), []
    for l in listeners:
        if is_loopback(l["addr"]):
            continue
        k = (l["proto"], l["addr"], l["port"], l["proc"])
        if k not in seen:
            seen.add(k)
            out.append(l)
    return out


def load_allowlist(path, today=None):
    today = today or datetime.date.today()
    entries = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            p = line.split()
            if len(p) < 5:
                raise ValueError("%s:%d: need PROTO ADDRESS PORT PROCESS REASON" % (path, n))
            proto, addr, port, proc = p[:4]
            rest = p[4:]
            until = None
            if rest and rest[0].startswith("until="):
                until = datetime.date.fromisoformat(rest[0][6:])
                rest = rest[1:]
            intermittent = bool(rest and rest[0] == "intermittent")          # listens only part of the time (a periodic scan): allowed, but its absence is normal
            if intermittent:
                rest = rest[1:]
            if proto not in ("tcp", "udp") or not (port == "ephemeral" or port.isdigit()) or not rest:
                raise ValueError("%s:%d: malformed entry: %s" % (path, n, line))
            entries.append({"proto": proto, "addr": "*" if addr in ("*", "0.0.0.0", "::") else addr, "port": port, "proc": proc,
                            "until": until, "intermittent": intermittent, "reason": " ".join(rest), "expired": bool(until and today > until)})
    return entries


def matches(entry, l):
    if entry["expired"] or entry["proto"] != l["proto"] or entry["addr"] != l["addr"]:
        return False
    if entry["proc"] != "-" and entry["proc"] != l["proc"]:
        return False
    if entry["port"] == "ephemeral":
        return l["port"] >= EPHEMERAL_MIN
    return int(entry["port"]) == l["port"]


def fmt(l):
    return "%s %s:%d (%s)" % (l["proto"], l["addr"], l["port"], l["proc"])


def evaluate(N, listeners, entries, state):
    """Pure decision logic. state: {"alerted": [keys]} persisted between runs. Returns the new state."""
    now = normalise(listeners)
    unknown = [l for l in now if not any(matches(e, l) for e in entries)]
    # Task 29a: ONE message per check for all unlisted sockets (a burst of 13 became 26 messages on 2026-10-20). The key is a hash of the SET of sockets: the same set
    # on the next run is the same alert (watchnotify dedups, 6 h reminder), a different set is a new alert and the old one is cleared.
    unknown_keys = {}
    if unknown:
        socks = sorted(fmt(l) for l in unknown)
        gkey = "listener-new-" + hashlib.sha1("\n".join(socks).encode()).hexdigest()[:10]
        unknown_keys[gkey] = socks
        listing = "\n".join("  " + s for s in socks[:25]) + ("\n  ... и ещё %d" % (len(socks) - 25) if len(socks) > 25 else "")
        N.alert(gkey, "listener-watch: НОВЫЕ слушающие сокеты вне белого списка (%d):\n%s\nЕсли это ожидаемо - добавить в "
                      "_system/listener-watch/allowlist.conf с причиной; если нет - выяснить, что это." % (len(socks), listing), tier="A")
    for key in state.get("alerted", []):
        if key not in unknown_keys:
            N.clear(key, "listener-watch: сокеты больше не слушают или внесены в белый список")
    # expected fixed-port listeners that are missing -> quiet
    missing_now = []
    for e in entries:
        if e["port"] == "ephemeral" or e["expired"] or e["until"] or e.get("intermittent"):        # a time-limited exception is not an expectation
            continue
        if not any(matches(e, l) for l in now):
            missing_now.append(e)
    miss_keys = {"listener-missing-%s-%s-%s" % (e["proto"], e["addr"], e["port"]): e for e in missing_now}
    for key, e in miss_keys.items():
        N.alert(key, "listener-watch: ожидаемый сокет не слушает: %s %s:%s (%s) - %s" % (e["proto"], e["addr"], e["port"], e["proc"], e["reason"][:60]), tier="C")
    for key in state.get("missing", []):
        if key not in miss_keys:
            N.clear(key, "")
    return {"alerted": sorted(unknown_keys), "missing": sorted(miss_keys), "checked": len(now), "at": int(time.time())}


STATE_FILE = os.environ.get("LISTENER_WATCH_STATE", "/var/lib/watch-alerts/listener-watch.json")


def main():
    import watchnotify
    r = subprocess.run(["sudo", "-n", "ss", "-tulnpH"], capture_output=True, text=True)      # process names of other users need root; the watcher itself runs as skit
    if r.returncode != 0 or not r.stdout.strip():
        watchnotify.alert("listener-watch-ss", "listener-watch: не могу прочитать список сокетов (ss rc=%s)" % r.returncode, tier="B")
        return 1
    watchnotify.clear("listener-watch-ss", "")
    try:
        entries = load_allowlist(ALLOWLIST)
    except (OSError, ValueError) as e:
        watchnotify.alert("listener-watch-allowlist", "listener-watch: белый список нечитаем: %s" % e, tier="B")
        return 1
    watchnotify.clear("listener-watch-allowlist", "")
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state = evaluate(watchnotify, parse_ss(r.stdout), entries, state)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
