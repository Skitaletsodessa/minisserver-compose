#!/usr/bin/env python3
"""Control and status for the relay (run inside the container; the host wrappers relay-off / relay-on /
relay-status / relay-reset call it through `docker exec`). Works on the shared state file only."""
import json, os, sys, time

STATE = os.environ.get("RELAY_STATE_DIR", "/state")


def load(name):
    try:
        return json.load(open(os.path.join(STATE, name)))
    except (OSError, ValueError):
        return {}


def save(st):
    tmp = os.path.join(STATE, "relay.json.tmp")
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, os.path.join(STATE, "relay.json"))


def report(days=3):
    """Requests per day / hour / caller / status from the persistent request log (state/requests-YYYY-MM-DD.jsonl)."""
    import collections
    import glob
    files = sorted(glob.glob(os.path.join(STATE, "requests-*.jsonl")))[-days:]
    if not files:
        print("no request log yet (it starts with the first request after the 2026-10-03 change)")
    for fn in files:
        day = os.path.basename(fn)[9:19]
        byhour, bycaller, status, refused, ms = collections.Counter(), collections.Counter(), collections.Counter(), collections.Counter(), []
        n = 0
        for line in open(fn):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            n += 1
            byhour[d["ts"][11:13]] += 1
            bycaller[d["caller"]] += 1
            status[d["status"]] += 1
            ms.append(d.get("ms", 0))
            if d.get("refused"):
                refused[d["refused"]] += 1
        print("== %s: %d requests, median %d ms" % (day, n, sorted(ms)[len(ms) // 2] if ms else 0))
        print("   per hour :", " ".join("%sh=%d" % kv for kv in sorted(byhour.items())))
        print("   callers  :", dict(bycaller.most_common()))
        print("   statuses :", dict(sorted(status.items())), "| refusals:", dict(refused) or "none")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    st = load("relay.json")
    if cmd == "report":
        report(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "off":
        st["killed"] = True; save(st); print("relay: OFF (answers 503 to everything)")
    elif cmd == "on":
        st["killed"] = False; save(st); print("relay: ON")
    elif cmd == "reset":
        st["breaker"] = {}; save(st); print("circuit breakers reset")
    else:
        d = st.get("daily", {})
        print("killed:", bool(st.get("killed")))
        print("today:", d.get("day"), "used", d.get("n", 0), "of", os.environ.get("RELAY_DAILY_CAP", "?"))
        for host, b in (st.get("breaker") or {}).items():
            left = int(b.get("open_until", 0) - time.time())
            print("breaker %s: %s (streak %s)" % (host, ("OPEN %ds left" % left) if left > 0 else "closed", b.get("streak", 0)))
        c = load("counters.json")
        print("refusals/counters since start:", json.dumps(c.get("since_start", {}), sort_keys=True))


main()
