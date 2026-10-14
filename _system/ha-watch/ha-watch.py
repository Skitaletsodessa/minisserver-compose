#!/usr/bin/env python3
"""Task 25: Home Assistant liveness watchdog.
Checks http://127.0.0.1:8123/api/ — 401 = alive. Three consecutive failures → Telegram alert.
Recovery → one message. Runs every 5 min (ha-watch.timer). WATCH_TEST=1 → print only."""
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402

STATE_FILE = "/var/lib/ha-watch/fail_count"
FAIL_THRESHOLD = 3
URL = "http://127.0.0.1:8123/api/"


def _read_fails():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_fails(n):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(str(n))


def _alive():
    try:
        urllib.request.urlopen(URL, timeout=10)
        return True  # 200 — first run before owner account created
    except urllib.error.HTTPError as e:
        return e.code == 401  # expected unauthenticated response
    except Exception:
        return False


def main():
    ok = _alive()
    fails = _read_fails()
    if ok:
        _write_fails(0)
        if fails >= FAIL_THRESHOLD:
            watchnotify.clear("ha-watch", "Home Assistant снова доступен.")
        return 0
    fails += 1
    _write_fails(fails)
    if fails >= FAIL_THRESHOLD:
        watchnotify.alert("ha-watch",
                          f"Home Assistant не отвечает ({URL}, {fails} попыток подряд)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
