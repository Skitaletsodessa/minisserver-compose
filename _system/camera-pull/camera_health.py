#!/usr/bin/env python3
"""Task 34 phase 0(c): hourly health check of the front-yard camera over DVRIP (read-only except the clock).
tier B (one message, no repeat): camera unreachable twice in a row (not during the Saturday 03:00 auto-reboot), SD missing/error,
oldest clip on the card younger than 7 days. Clock: measured every run; a daily set_time over DVRIP when the drift is > 2 s
(the camera has no internet; no NTP listener is added)."""
import json
import os
import sys
from datetime import datetime, timedelta

import camlib

STATE_PATH = os.path.join(camlib.STATE_DIR, "health-state.json")
FMT = "%Y-%m-%d %H:%M:%S"
MIN_LOOP_DAYS = 7
DRIFT_LIMIT = 2.0
CLOCK_SET_EVERY = timedelta(hours=20)
# On 2026-11-11 16:00 the regular (R) recording of ch1/ch2 was switched off (Task 34 phase 0a); the card still holds ~6 days of old
# 19 GB/day footage, so "loop shorter than 7 days" is expected until the old footage is overwritten. Alert only after this date.
SHORT_LOOP_GRACE_UNTIL = datetime(2026, 11, 19)


def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE_PATH)


def in_reboot_window(now):
    """Saturday 02:55-03:30: the camera's own auto-reboot (Advanced -> AutoMaintain). Not counted as a failure."""
    if now.weekday() != 5:
        return False
    m = now.hour * 60 + now.minute
    return 2 * 60 + 55 <= m <= 3 * 60 + 30


def sd_findings(storage, now):
    """storage = the `StorageInfo` reply. Returns (problems, oldest_datetime_or_None)."""
    problems = []
    oldest = None
    parts = []
    for disk in storage if isinstance(storage, list) else []:
        parts += disk.get("Partition", [])
    rec = [p for p in parts if p.get("DirverType") == 0]
    if not rec:
        return ["no recording partition reported (SD card missing?)"], None
    for p in rec:
        if p.get("Status") != 0:
            problems.append(f"partition status {p.get('Status')} (not normal)")
        if int(str(p.get("TotalSpace", "0x0")), 16) == 0:
            problems.append("recording partition has zero size")
        try:
            t = datetime.strptime(p.get("OldStartTime"), FMT)
            oldest = t if oldest is None or t < oldest else oldest
        except (TypeError, ValueError):
            pass
    return problems, oldest


def run(env, now=None, cam_factory=None, notify=None):
    now = now or datetime.now()
    st = load_state()
    out = {"at": now.strftime(FMT)}
    try:
        with camlib.dvrip_session(env, wait=120, cam_factory=cam_factory or camlib.DVRIPCam) as cam:
            drift = (cam.get_time() - datetime.now()).total_seconds()
            out["drift_s"] = round(drift, 1)
            last_set = datetime.strptime(st["clock_set_at"], FMT) if st.get("clock_set_at") else None
            if abs(drift) > DRIFT_LIMIT and (last_set is None or now - last_set > CLOCK_SET_EVERY):
                cam.set_time()
                st["clock_set_at"] = now.strftime(FMT)
                out["clock_set"] = True
            storage = cam.get_command("StorageInfo", 1020)
        problems, oldest = sd_findings(storage, now)
        out["sd_problems"] = problems
        out["oldest_clip"] = oldest.strftime(FMT) if oldest else None
        st["fail_streak"] = 0
        st["last_ok"] = now.strftime(FMT)
        reachable = True
    except camlib.CameraBusy:
        out["skipped"] = "another camera job holds the lock"
        save_state(st)
        return out
    except Exception as e:      # connection refused, timeout, login refused
        reachable = False
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        if not in_reboot_window(now):
            st["fail_streak"] = st.get("fail_streak", 0) + 1
        problems, oldest = [], None
    out["fail_streak"] = st.get("fail_streak", 0)
    save_state(st)
    if notify is not None:
        if st.get("fail_streak", 0) >= 2:
            notify.alert("camera-unreachable", "Камера .95 недоступна два часовых опроса подряд (DVRIP). Проверьте питание и Wi-Fi.", tier="B")
        elif reachable:
            notify.clear("camera-unreachable", "")
        if reachable:
            if problems:
                notify.alert("camera-sd", "Камера .95: проблема с SD-картой — " + "; ".join(problems), tier="B")
            else:
                notify.clear("camera-sd", "")
            if oldest and (now - oldest) < timedelta(days=MIN_LOOP_DAYS) and now >= SHORT_LOOP_GRACE_UNTIL:
                notify.alert("camera-sd-short", f"Камера .95: самый старый клип на SD-карте от {oldest:%Y-%m-%d %H:%M} — карта хранит меньше {MIN_LOOP_DAYS} дней. Клипы нужно забирать чаще или писать меньше.", tier="B")
            else:
                notify.clear("camera-sd-short", "")
    return out


def main():
    env = camlib.load_env()
    import watchnotify as N
    print(json.dumps(run(env, notify=N)))


if __name__ == "__main__":
    main()
