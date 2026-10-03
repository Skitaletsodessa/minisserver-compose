#!/usr/bin/env python3
"""Task 23 / 24 alerts for the relay (host side, every minute): the container holds no Telegram token.
State alerts (through watchnotify: first message, REMINDER every 6 h while it lasts, recovery with the duration):
  relay unhealthy (3 checks), Cloudflare tunnel disconnected (3 checks).
Event messages (one each, delivery logged and retried): any private-address attempt, circuit breaker opened, daily cap
at 80 %, sustained signature failures (>= 20 in 5 min, at most once per hour). Reads the events file the relay appends to.

Test mode: RELAY_WATCH_EVENTS=<other events file> turns on WATCH_TEST ("[TEST] " messages, separate alert state);
RELAY_WATCH_URL / RELAY_WATCH_TUNNEL_URL point the two health checks at a dead address."""
import json
import os
import sys
import time
import urllib.request

if os.environ.get("RELAY_WATCH_EVENTS"):
    os.environ.setdefault("WATCH_TEST", "1")
sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402

STATE_DIR = os.environ.get("RELAY_WATCH_STATE", "/var/lib/relay-watch")
EVENTS = os.environ.get("RELAY_WATCH_EVENTS", "/srv/apps/relay-state/events.jsonl")   # tests point this at the test instance
HEALTH_URL = os.environ.get("RELAY_WATCH_URL", "http://127.0.0.1:18090/healthz")
TUNNEL_URL = os.environ.get("RELAY_WATCH_TUNNEL_URL", "http://127.0.0.1:20241/ready")
PREFIX = "minisserver relay-watch: "


def load():
    try:
        return json.load(open(os.path.join(STATE_DIR, "state.json")))
    except (OSError, ValueError):
        return {}


def probe(url):
    try:
        urllib.request.urlopen(url, timeout=4).read()
        return True
    except Exception:  # noqa: BLE001
        return False


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    st = load()
    now = time.time()
    # 1. health of the relay itself
    if probe(HEALTH_URL):
        st["health_fail"] = 0
        watchnotify.clear("relay-health", PREFIX + "the relay answers again.")
    else:
        st["health_fail"] = st.get("health_fail", 0) + 1
        print("health check failed", st["health_fail"])
        if st["health_fail"] >= 3:
            watchnotify.alert("relay-health", PREFIX + f"the relay is not answering on {HEALTH_URL} ({st['health_fail']} checks).")
    # 1b. the Cloudflare tunnel: cloudflared answers /ready with 200 while it holds a connection to the edge
    if os.path.exists("/srv/compose/relay-tunnel/.env") or os.environ.get("RELAY_WATCH_TUNNEL_URL"):   # only once set up
        if probe(TUNNEL_URL):
            st["tunnel_fail"] = 0
            watchnotify.clear("relay-tunnel", PREFIX + "the Cloudflare tunnel is connected again.")
        else:
            st["tunnel_fail"] = st.get("tunnel_fail", 0) + 1
            if st["tunnel_fail"] >= 3:
                watchnotify.alert("relay-tunnel", PREFIX + "the Cloudflare tunnel (relay.ivandeliver.email) is not connected "
                                  f"({st['tunnel_fail']} checks): the Worker falls back to the paid providers.")
    # 2. events since the last offset
    off = st.get("offset", 0)
    try:
        size = os.path.getsize(EVENTS)
        if size < off:
            off = 0
        with open(EVENTS) as f:
            f.seek(off)
            new = f.read()
            off = f.tell()
        for line in new.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            k = e.get("kind")
            if k == "auth_fail":
                st.setdefault("auth", []).append(e["t"])
            elif k == "private_address" and now - st.get("last_private", 0) > 600:
                st["last_private"] = now
                watchnotify.send(PREFIX + f"REFUSED a private-address attempt: {e.get('host')} -> {e.get('addr')}")
            elif k == "breaker_open":
                watchnotify.send(PREFIX + f"circuit breaker OPENED for {e.get('host')} (repeated 403/429/challenge): the house IP may be blocked there. Cool-down {e.get('cooldown')} s.")
            elif k == "cap80":
                watchnotify.send(PREFIX + f"daily cap at 80 %: {e.get('used')} of {e.get('cap')} requests today.")
        if size > 2 * 1024 * 1024:
            os.replace(EVENTS, EVENTS + ".1")
            off = 0
    except OSError:
        pass
    st["offset"] = off
    st["auth"] = [t for t in st.get("auth", []) if now - t < 300]
    if len(st["auth"]) >= 20 and now - st.get("last_auth_alert", 0) > 3600:
        st["last_auth_alert"] = now
        watchnotify.send(PREFIX + f"{len(st['auth'])} signature failures in the last 5 minutes (somebody is probing the relay).")
    json.dump(st, open(os.path.join(STATE_DIR, "state.json"), "w"))


main()
