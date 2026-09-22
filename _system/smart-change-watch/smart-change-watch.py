#!/usr/bin/env python3
"""Alert when a watched SMART raw value CHANGES, not while it merely sits in a bad state.

Why this exists (docs/incident-2026-09-18.md): Scrutiny 0.9.3 judges STATE - the Seagate's
historical, packed, non-moving Command_Timeout raw trips its Backblaze threshold and re-alerts
every collection, and Scrutiny cannot override one attribute. Ivan chose: keep Scrutiny quiet
(repeat_notifications off) and cover the disks with this change detector instead.

Reads smartctl directly, addresses disks by SERIAL (never /dev/sdX), never wakes a disk from
standby (-n standby), alerts once per change and then moves the baseline, so a value that
stays put is silent forever and one that moves again alerts again.
"""
import argparse
import glob
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASELINE = Path("/var/lib/smart-change-watch/baseline.json")
ENV_FILE = Path("/srv/compose/scrutiny/.env")

# serial -> (label, bus, {attribute: minimum change that alerts})
# A new disk in the machine is NOT watched until it is added here.
DISKS = {
    "WDE6X73A": ("Seagate ST1000LM035 (library + vault)", "ata",
                 {5: 1, 184: 1, 187: 1, 188: 1, 197: 1, 198: 1, 199: 1}),
    "S4ENNF1MA94910": ("Samsung PM981a NVMe (boot)", "nvme",
                       {"critical_warning": 1, "media_errors": 1}),
}


def smartctl_json(path):
    # -n standby: if the disk is spun down, do not wake it - skip this round
    p = subprocess.run(["smartctl", "-A", "-j", "-n", "standby", path],
                       capture_output=True, text=True, timeout=60)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def read_disk(serial, bus, watch):
    matches = glob.glob(f"/dev/disk/by-id/{bus}-*_{serial}")
    if not matches:
        return "missing", None
    data = smartctl_json(matches[0])
    if data is None:
        return "unreadable", None
    if bus == "ata":
        table = data.get("ata_smart_attributes", {}).get("table")
        if not table:
            msgs = " ".join(m.get("string", "") for m in data.get("smartctl", {}).get("messages", []))
            if any(w in msgs.upper() for w in ("STANDBY", "SLEEP", "IDLE")):
                return "standby", None
            return "unreadable", None
        by_id = {row["id"]: row["raw"]["value"] for row in table}
        return "ok", {str(a): by_id[a] for a in watch if a in by_id}
    log = data.get("nvme_smart_health_information_log")
    if not log:
        return "unreadable", None
    return "ok", {str(a): log[a] for a in watch if a in log}


def describe(serial, attr, value):
    if serial == "WDE6X73A" and attr == "188":  # Seagate packs three 16-bit counters
        return f"{value} ({value & 0xffff} total / {(value >> 16) & 0xffff} >5s / {(value >> 32) & 0xffff} >7.5s)"
    return str(value)


def telegram(message, dry_run):
    print("ALERT:", message)
    if dry_run:
        return
    creds = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            creds[k.strip()] = v.strip()
    url = f"https://api.telegram.org/bot{creds['TELEGRAM_BOT_TOKEN']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": creds["TELEGRAM_CHAT_ID"],
                                   "text": f"minisserver smart-change-watch: {message}"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
    except Exception as e:  # noqa: BLE001 - never crash the run on a send failure
        print("Telegram send failed:", e, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print alerts, send nothing, write no baseline")
    ap.add_argument("--baseline-file", default=str(BASELINE))
    ap.add_argument("--test-alert", action="store_true", help="send one delivery-check message and exit")
    args = ap.parse_args()
    if args.test_alert:
        telegram("TEST - delivery check, no attribute changed. Nothing to do.", args.dry_run)
        return

    path = Path(args.baseline_file)
    base = json.loads(path.read_text()) if path.exists() else {}
    unreadable = set(base.get("_unreadable", []))
    values = {k: v for k, v in base.items() if not k.startswith("_")}
    now_unreadable = set()
    seeded = 0

    for serial, (label, bus, watch) in DISKS.items():
        status, got = read_disk(serial, bus, watch)
        if status == "standby":
            print(f"{label}: in standby, skipped (not woken)")
            if serial in unreadable:
                now_unreadable.add(serial)
            continue
        if status != "ok":
            now_unreadable.add(serial)
            if serial not in unreadable:
                telegram(f"{label} [{serial}] cannot be read ({status}) - disk missing or SMART failing", args.dry_run)
            continue
        changes = []
        for attr, minimum in watch.items():
            a = str(attr)
            if a not in got:
                continue
            key, new = f"{serial}:{a}", got[a]
            old = values.get(key)
            if old is None:
                values[key] = new
                seeded += 1
            elif new != old and abs(new - old) >= minimum:
                changes.append(f"attr {a}: {describe(serial, a, old)} -> {describe(serial, a, new)}")
                values[key] = new
            # a change below the threshold leaves the baseline alone, so slow drift accumulates
            # until it crosses the threshold instead of being silently absorbed
        if changes:
            telegram(f"{label} [{serial}] CHANGED: " + "; ".join(changes), args.dry_run)
        else:
            print(f"{label}: no change")

    out = dict(values)
    out["_unreadable"] = sorted(now_unreadable)
    if not args.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1, sort_keys=True))
    if seeded:
        print(f"baseline: {seeded} new value(s) recorded (first sight, no alert)")


if __name__ == "__main__":
    main()
