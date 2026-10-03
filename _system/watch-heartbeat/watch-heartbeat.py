#!/usr/bin/env python3
"""Dead-man's heartbeat (Task 24 A4): once a day, one Telegram line:
   "Heartbeat 08:00 - 17/17 watchdogs ran on schedule, 0 alerts active."
It exists for the failure no watchdog can report about itself: a timer disabled, a script that stopped running, a host that
is off, a bot token that was revoked. Then the daily line does NOT arrive, and its absence is the alarm.

For every watchdog service it reads when the service last finished (systemd, unix timestamp) and compares with the longest
gap its timer allows. A watchdog that is overdue is named in the message AND raises its own alert through watchnotify
(so it nags every 6 h until fixed). The Pi-side watchdog is checked through the Pi's read-only report key.
Options: --dry-run (print, send nothing). WATCH_TEST=1 prefixes "[TEST] " and uses a separate alert state."""
import os
import subprocess
import sys
import time

sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402

# service, longest acceptable age in seconds (timer period x 3, rounded up; daily jobs 26-30 h, weekly 8 days)
WATCHERS = [
    ("adguard-watch.service", 180), ("adguard-watch-replica.service", 180), ("adguard-sync-watch.service", 180),
    ("pi-watch.service", 180), ("relay-watch.service", 180), ("adguard-policy.service", 180),
    ("adguard-parity.service", 900), ("disk-space-watch.service", 900), ("qbt-space-guard.service", 480),
    ("library-sort.service", 900), ("disk-error-watch.service", 3000), ("smart-change-watch.service", 12 * 3600),
    ("backup-freshness-watch.service", 30 * 3600), ("restic-backup.service", 30 * 3600),
    ("restic-backup-r2.service", 30 * 3600), ("pi-weekly.service", 8 * 24 * 3600),
]
PI_REPORT_KEY = "/home/skit/.ssh/piserver_report"


def last_finish(unit):
    r = subprocess.run(["systemctl", "show", unit, "-p", "ExecMainExitTimestamp", "--value", "--timestamp=unix"],
                       capture_output=True, text=True)
    v = r.stdout.strip()
    if v.startswith("@") and v[1:].isdigit():
        return int(v[1:])
    return None


def pi_watch_age():
    try:
        r = subprocess.run(["ssh", "-T", "-q", "-i", PI_REPORT_KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                            "-o", "ConnectTimeout=10", "skit@192.168.31.5"], capture_output=True, text=True, timeout=60)
        for line in r.stdout.splitlines():
            if line.startswith("origin_watch_age_s="):
                v = line.split("=", 1)[1]
                return int(v) if v.isdigit() else None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def human(sec):
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min" if sec >= 3600 else f"{sec // 60} min"


def main():
    dry = "--dry-run" in sys.argv
    now = time.time()
    overdue, ok = [], 0
    for unit, limit in WATCHERS:
        t = last_finish(unit)
        if t is None:
            # never ran: fine only while its TIMER is active and simply not due yet (e.g. the weekly Pi report before its first Monday);
            # a timer that is not even active is exactly the fault this heartbeat exists to find (found 2026-10-03: relay-watch.timer
            # had been enabled but never started)
            timer = unit.replace(".service", ".timer")
            active = subprocess.run(["systemctl", "is-active", timer], capture_output=True, text=True).stdout.strip()
            if active == "active":
                ok += 1
            else:
                overdue.append(f"{unit} (never ran and {timer} is {active})")
        elif now - t > limit:
            overdue.append(f"{unit} (last ran {human(int(now - t))} ago, limit {human(limit)})")
        else:
            ok += 1
    total = len(WATCHERS) + 1
    age = pi_watch_age()
    if age is None:
        overdue.append("Pi-side adguard-watch-origin (cannot read its age through the Pi report key)")
    elif age > 180:
        overdue.append(f"Pi-side adguard-watch-origin (last ran {human(age)} ago)")
    else:
        ok += 1
    active = [(k, s) for k, s, _d in watchnotify.active() if k != "watchdog-overdue"]
    when = time.strftime("%H:%M")
    line = f"minisserver heartbeat {when}: {ok}/{total} watchdogs ran on schedule, {len(active)} alert(s) active."
    if active:
        line += "\nActive: " + "; ".join(f"{k} since {time.strftime('%d.%m %H:%M', time.localtime(s))}" for k, s in active)
    if overdue:
        line += "\nOVERDUE: " + "; ".join(overdue)
    print(line)
    if dry:
        return 0
    if overdue:
        watchnotify.alert("watchdog-overdue", "minisserver watchdogs not running on schedule: " + "; ".join(overdue))
    else:
        watchnotify.clear("watchdog-overdue", "minisserver: every watchdog is running on schedule again.")
    watchnotify.send(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
