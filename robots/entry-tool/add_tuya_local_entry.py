#!/usr/bin/env python3
"""Task 32: add ONE tuya_local config entry to Home Assistant's storage OFFLINE (HA stopped), the way Task 25 fixed `.storage/http`.
Used only because there is no HA admin token and the config flow's cloud login needs a human; the device id, host, local key and protocol version come
from /var/lib/task32/tiger.env (mode 600) and are never printed.

  python3 add_tuya_local_entry.py --type eta_tiger_vacuum --title "ETA Tiger" [--dry-run]

Refuses to run while the homeassistant container is running, if the storage file is not the expected shape, if an entry with the same unique id exists,
or if the device config file for --type is not installed in tuya_local's devices directory. Makes a backup of the storage file first and writes the new
content IN PLACE (never mv/cp over a bind mount).
"""
import copy
import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys

STORAGE = "/srv/apps/homeassistant/.storage/core.config_entries"
DEVICES = "/srv/apps/homeassistant/custom_components/tuya_local/devices"
ENV = "/var/lib/task32/tiger.env"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_entry_id(now_ms=None):
    """A ULID-like 26 character id (10 time characters + 16 random), the shape HA gives its own entries."""
    t = int(now_ms if now_ms is not None else datetime.datetime.now().timestamp() * 1000)
    head = "".join(CROCKFORD[(t >> (5 * i)) & 31] for i in range(9, -1, -1))
    return head + "".join(secrets.choice(CROCKFORD) for _ in range(16))


def read_env(path):
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


def add_entry(doc, cfg, config_type, title, now_iso=None, entry_id=None):
    """Return a NEW document (the input is not modified) with one more tuya_local entry shaped like the existing ones."""
    if doc.get("key") != "core.config_entries" or "entries" not in doc.get("data", {}):
        raise SystemExit("unexpected storage file shape")
    entries = doc["data"]["entries"]
    templates = [e for e in entries if e.get("domain") == "tuya_local"]
    if not templates:
        raise SystemExit("no tuya_local entry to copy the shape from")
    if any(e.get("unique_id") == cfg["DEVICE_ID"] for e in templates):
        raise SystemExit("an entry for this device already exists")
    now = now_iso or datetime.datetime.now(datetime.timezone.utc).isoformat()
    e = copy.deepcopy(templates[0])
    e.update({"entry_id": entry_id or new_entry_id(), "title": title, "unique_id": cfg["DEVICE_ID"], "created_at": now, "modified_at": now,
              "disabled_by": None, "options": {}, "source": "user", "discovery_keys": {}, "subentries": []})
    e["data"] = {"device_id": cfg["DEVICE_ID"], "host": cfg["HOST"], "local_key": cfg["LOCAL_KEY"], "poll_only": False,
                 "protocol_version": float(cfg["VERSION"]) if cfg["VERSION"] != "auto" else "auto", "type": config_type}
    out = copy.deepcopy(doc)
    out["data"]["entries"].append(e)
    return out


def ha_running():
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "homeassistant"], capture_output=True, text=True)
    return r.stdout.strip() == "true"


def main(argv):
    dry = "--dry-run" in argv
    ctype = argv[argv.index("--type") + 1]
    title = argv[argv.index("--title") + 1]
    if not os.path.exists(os.path.join(DEVICES, ctype + ".yaml")):
        raise SystemExit("device config %s.yaml is not installed in %s" % (ctype, DEVICES))
    if ha_running() and not dry:
        raise SystemExit("homeassistant is running: stop it first (the restart conditions of Task 32 apply)")
    cfg = read_env(ENV)
    doc = json.load(open(STORAGE, encoding="utf-8"))
    new = add_entry(doc, cfg, ctype, title)
    print("would add entry '%s' of type %s (%d -> %d entries)" % (title, ctype, len(doc["data"]["entries"]), len(new["data"]["entries"])))
    if dry:
        return 0
    backup = os.path.join(os.path.dirname(os.path.dirname(STORAGE)), "pre-task32-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(backup)
    shutil.copy2(STORAGE, os.path.join(backup, "core.config_entries"))
    with open(STORAGE, "w", encoding="utf-8") as f:                       # in place
        json.dump(new, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("done; backup in", backup)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
