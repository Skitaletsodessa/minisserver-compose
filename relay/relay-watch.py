#!/usr/bin/env python3
"""Task 23 alerts for the relay (host side, every minute): the container holds no Telegram token.
Alerts: relay unhealthy (3 checks), any private-address attempt, circuit breaker opened, daily cap at 80 %,
sustained signature failures (>= 20 in 5 min, once per hour). Reads the events file the relay appends to."""
import json, os, time, urllib.parse, urllib.request

STATE_DIR = os.environ.get("RELAY_WATCH_STATE", "/var/lib/relay-watch")
EVENTS = os.environ.get("RELAY_WATCH_EVENTS", "/srv/apps/relay-state/events.jsonl")   # tests point this at the test instance
TAG = "[TEST] " if os.environ.get("RELAY_WATCH_EVENTS") else ""
TG = "/srv/compose/scrutiny/.env"


def env(key):
    for l in open(TG):
        if l.startswith(key + "="):
            return l.split("=", 1)[1].strip()


def tg(text):
    data = urllib.parse.urlencode({"chat_id": env("TELEGRAM_CHAT_ID"), "text": TAG + "minisserver relay-watch: " + text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage", data), timeout=20)
    except Exception as e:  # noqa: BLE001
        print("telegram failed:", e)


def load():
    try:
        return json.load(open(os.path.join(STATE_DIR, "state.json")))
    except (OSError, ValueError):
        return {}


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    st = load()
    now = time.time()
    # 1. health
    try:
        urllib.request.urlopen("http://127.0.0.1:18090/healthz", timeout=4).read()
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    if ok:
        if st.get("alerted_health"):
            tg("the relay answers again.")
            st["alerted_health"] = False
        st["health_fail"] = 0
    else:
        st["health_fail"] = st.get("health_fail", 0) + 1
        print("health check failed", st["health_fail"])
        if st["health_fail"] >= 3 and not st.get("alerted_health"):
            tg("the relay is not answering on 127.0.0.1:18090 (3 checks).")
            st["alerted_health"] = True
    # 1b. the Cloudflare tunnel: cloudflared answers /ready with 200 while it holds a connection to the edge
    try:
        urllib.request.urlopen("http://127.0.0.1:20241/ready", timeout=4).read()
        tok = True
    except Exception:  # noqa: BLE001
        tok = False
    if os.path.exists("/srv/compose/relay-tunnel/.env"):      # only once the tunnel has been set up
        if tok:
            if st.get("alerted_tunnel"):
                tg("the Cloudflare tunnel is connected again.")
                st["alerted_tunnel"] = False
            st["tunnel_fail"] = 0
        else:
            st["tunnel_fail"] = st.get("tunnel_fail", 0) + 1
            if st["tunnel_fail"] >= 3 and not st.get("alerted_tunnel"):
                tg("the Cloudflare tunnel (relay.ivandeliver.email) is not connected (3 checks): the Worker falls back to the paid providers.")
                st["alerted_tunnel"] = True
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
                tg(f"REFUSED a private-address attempt: {e.get('host')} -> {e.get('addr')}")
            elif k == "breaker_open":
                tg(f"circuit breaker OPENED for {e.get('host')} (repeated 403/429/challenge): the house IP may be blocked there. Cool-down {e.get('cooldown')} s.")
            elif k == "cap80":
                tg(f"daily cap at 80 %: {e.get('used')} of {e.get('cap')} requests today.")
        if size > 2 * 1024 * 1024:
            os.replace(EVENTS, EVENTS + ".1")
            off = 0
    except OSError:
        pass
    st["offset"] = off
    st["auth"] = [t for t in st.get("auth", []) if now - t < 300]
    if len(st["auth"]) >= 20 and now - st.get("last_auth_alert", 0) > 3600:
        st["last_auth_alert"] = now
        tg(f"{len(st['auth'])} signature failures in the last 5 minutes (somebody is probing the relay).")
    json.dump(st, open(os.path.join(STATE_DIR, "state.json"), "w"))


main()
