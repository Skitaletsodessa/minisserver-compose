#!/usr/bin/env python3
"""Task 32 capture logger: connects to the robot over the Tuya LAN protocol and appends EVERY data-point change to a JSONL file with a timestamp.

  python3 dp_logger.py [--seconds N]        credentials come from /var/lib/task32/tiger.env (DEVICE_ID, LOCAL_KEY, HOST, VERSION; mode 600)

The raw file (/var/lib/task32/captures/dp-YYYYMMDD.jsonl, mode 600) holds the real values and stays on the server. `redact.py` produces the shareable form
(long payloads reduced to length + first bytes). The logger writes only DP changes, never the key or the device id, and prints neither.
"""
import datetime
import json
import os
import sys
import time

ENV = "/var/lib/task32/tiger.env"
OUT_DIR = "/var/lib/task32/captures"


def load_env(path=ENV):
    cfg = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    for need in ("DEVICE_ID", "LOCAL_KEY", "HOST", "VERSION"):
        if not cfg.get(need):
            raise SystemExit("missing %s in %s" % (need, path))
    return cfg


def describe(value):
    """One JSON-able description of a DP value: scalars as they are, long strings with their length and a head, so raw frames stay inspectable."""
    if isinstance(value, str) and len(value) > 64:
        return {"type": "str", "len": len(value), "head": value[:64]}
    return {"type": type(value).__name__, "value": value}


class Recorder:
    """Turns successive DP dictionaries into change records. Pure logic: no network, no files."""

    def __init__(self, now=None):
        self.last = {}
        self.now = now or (lambda: datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"))

    def feed(self, dps, source="push"):
        records = []
        for dp, value in sorted(dps.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 10 ** 9):
            if dp in self.last and self.last[dp] == value and source != "full":
                continue
            records.append({"t": self.now(), "dp": str(dp), "src": source, "was": describe(self.last[dp]) if dp in self.last else None, **describe(value)})
            self.last[dp] = value
        return records


def append(records, out_dir=OUT_DIR):
    if not records:
        return
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    path = os.path.join(out_dir, "dp-%s.jsonl" % datetime.date.today().strftime("%Y%m%d"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(seconds):
    import tinytuya
    cfg = load_env()
    rec = Recorder()
    deadline = time.time() + seconds if seconds else None
    backoff = 2
    while deadline is None or time.time() < deadline:
        try:
            d = tinytuya.Device(cfg["DEVICE_ID"], cfg["HOST"], cfg["LOCAL_KEY"], version=float(cfg["VERSION"]))
            d.set_socketPersistent(True)
            d.set_socketTimeout(5)
            st = d.status()
            if isinstance(st, dict) and "dps" in st:
                append(rec.feed(st["dps"], "full"))
                backoff = 2
            else:
                append([{"t": rec.now(), "dp": None, "src": "error", "type": "status", "value": {k: v for k, v in (st or {}).items() if k != "dps"}}])
            last_hb = time.time()
            while deadline is None or time.time() < deadline:
                if time.time() - last_hb > 9:
                    d.heartbeat(nowait=True)
                    last_hb = time.time()
                data = d.receive()
                if isinstance(data, dict) and "dps" in data:
                    append(rec.feed(data["dps"], "push"))
                elif isinstance(data, dict) and data.get("Error"):
                    raise ConnectionError(str(data.get("Error"))[:80])
            d.close()
        except Exception as exc:      # reconnect with backoff; every failure is itself a record (reconnect behaviour is part of what Phase 2 documents)
            append([{"t": rec.now(), "dp": None, "src": "disconnect", "type": "error", "value": type(exc).__name__ + ": " + str(exc)[:80]}])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    secs = int(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 0
    run(secs)
