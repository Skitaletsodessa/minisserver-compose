#!/usr/bin/env python3
"""Run the camera's SAVED PTZ route (tour) ONCE (Ivan 2026-11-17: «единоразовый прогон сохранённого маршрута камеры под индексом 1»).

The camera keeps tours in its own config (`Uart` -> `PTZTour`, channel 0): each tour is a list of presets with a dwell time. `StartTour` makes it loop for ever (the camera's
`ManualPtzTour.AutoStopTime` is 0 and is NOT changed here), so one run = StartTour, wait for ONE cycle (sum of the dwell times + a travel margin per leg), StopTour (also on
any error). The camera stays on the last preset of the route. ONE DVRIP session under the shared lock (camera-pull waits for it, ~1 minute).

  camera_ptz.py tours                 list the saved tours (read only)
  camera_ptz.py tour N [--dry-run]    run tour N once now
  camera_ptz.py stop                  StopTour (a safety stop)
  camera_ptz.py run-request           (systemd path unit) run what the gateway asked for in /var/lib/camera-ptz/request.json

The gateway holds no camera login: it only writes request.json; this host job validates it (a tour that exists in the camera, not while another one runs, not stale), runs it
and writes status.json (state queued|running|done|failed, tour, by, started, ends_at, ended, message)."""
import json
import os
import sys
import time
from datetime import datetime

import camlib

PTZ_DIR = os.environ.get("CAMERA_PTZ_DIR", "/var/lib/camera-ptz")
TRAVEL_S = float(os.environ.get("CAMERA_PTZ_TRAVEL_S", "4"))          # margin per leg for the camera to turn to the next preset
STALE_S = 120                                                          # a request older than this is ignored (a path unit fired late or twice)
CHANNEL = 0
FMT = "%Y-%m-%d %H:%M:%S"


def param(tour):
    return {"AUX": {"Number": 0, "Status": "On"}, "Channel": CHANNEL, "MenuOpts": "Enter", "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0},
            "Pattern": "Start", "Preset": -1, "Step": 5, "Tour": tour}


def read_tours(cam):
    """{tour id: [(preset id, dwell seconds), ...]} from the camera's own config."""
    cfg = cam.get_command("Uart", 1042)
    out = {}
    for t in (cfg.get("PTZTour") or [[]])[0]:
        out[t["Id"]] = [(p["Id"], p.get("Time", 5)) for p in t.get("Tour", [])]
    return out


def cycle_seconds(points):
    """One pass of the route: the dwell at every preset plus the travel to each of them."""
    return sum(t for _p, t in points) + TRAVEL_S * len(points)


def command(cam, name, tour=0):
    r = cam.set_command("OPPTZControl", {"Command": name, "Parameter": param(tour)})
    return r.get("Ret") if isinstance(r, dict) else None


def run_tour(cam, tour, sleep=time.sleep, on_start=None):
    """StartTour, one cycle, StopTour. Returns (seconds, points). Raises KeyError for a tour the camera does not have, RuntimeError when it refuses to start."""
    tours = read_tours(cam)
    if tour not in tours or not tours[tour]:
        raise KeyError("the camera has no tour %s (it has %s)" % (tour, sorted(tours)))
    points = tours[tour]
    secs = cycle_seconds(points)
    ret = command(cam, "StartTour", tour)
    if ret not in (100, None):
        raise RuntimeError("the camera refused StartTour (Ret %s)" % ret)
    try:
        if on_start:
            on_start(secs, points)
        sleep(secs)
    finally:
        command(cam, "StopTour", tour)
    return secs, points


# ── the request/status files (the gateway side writes request.json, reads status.json) ──
def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def set_status(d, **kw):
    st = _read(os.path.join(d, "status.json")) or {}
    st.update(kw)
    _write(os.path.join(d, "status.json"), st)
    return st


def run_request(env, d=PTZ_DIR, now=None, cam_factory=camlib.DVRIPCam, sleep=time.sleep):
    now = now or datetime.now()
    req = _read(os.path.join(d, "request.json"))
    if not req or req.get("done"):
        return "no request"
    st = _read(os.path.join(d, "status.json")) or {}
    if st.get("request_id") == req.get("id") and st.get("state") in ("running", "done", "failed"):
        return "already handled"
    try:
        age = (now - datetime.strptime(req["at"], FMT)).total_seconds()
    except (KeyError, ValueError):
        age = 0
    if age > STALE_S:
        set_status(d, request_id=req.get("id"), state="failed", message="the request was too old and was ignored")
        return "stale"
    tour = req.get("tour")
    base = {"request_id": req.get("id"), "tour": tour, "by": req.get("by"), "started": None, "ends_at": None, "ended": None, "message": ""}
    try:
        with camlib.dvrip_session(env, wait=240, cam_factory=cam_factory) as cam:
            def started(secs, points):
                t0 = datetime.now()
                set_status(d, **dict(base, state="running", started=t0.strftime(FMT), ends_at=datetime.fromtimestamp(t0.timestamp() + secs).strftime(FMT), seconds=round(secs), presets=[p for p, _t in points]))
            set_status(d, **dict(base, state="queued"))
            run_tour(cam, tour, sleep=sleep, on_start=started)
        set_status(d, state="done", ended=datetime.now().strftime(FMT), message="the route ran once; the camera stays on its last preset")
        return "done"
    except camlib.CameraBusy:
        set_status(d, **dict(base, state="failed", message="the camera is busy with another job; try again in a minute"))
        return "busy"
    except KeyError as e:
        set_status(d, **dict(base, state="failed", message=str(e).strip("'\"")))
        return "no such tour"
    except Exception as e:  # noqa: BLE001 - the status file is how the page learns about it
        set_status(d, **dict(base, state="failed", ended=datetime.now().strftime(FMT), message="%s: %s" % (type(e).__name__, str(e)[:120])))
        return "failed"


def main(argv):
    if not argv or argv[0] not in ("tours", "tour", "stop", "run-request"):
        print(__doc__)
        return 2
    env = camlib.load_env()
    os.makedirs(PTZ_DIR, exist_ok=True)
    if argv[0] == "run-request":
        print(run_request(env))
        return 0
    with camlib.dvrip_session(env, wait=240) as cam:
        if argv[0] == "tours":
            for tid, pts in sorted(read_tours(cam).items()):
                print("tour %s: presets %s, one pass ~%d s" % (tid, [p for p, _t in pts], cycle_seconds(pts)))
        elif argv[0] == "stop":
            print("StopTour Ret", command(cam, "StopTour"))
        else:
            tid = int(argv[1])
            if "--dry-run" in argv:
                pts = read_tours(cam).get(tid)
                print("would run tour %s once: presets %s, ~%d s" % (tid, [p for p, _t in pts or []], cycle_seconds(pts or [])))
                return 0 if pts else 1
            secs, pts = run_tour(cam, tid, on_start=lambda s, p: print("started tour %s, presets %s, waiting %d s" % (tid, [x for x, _t in p], s), flush=True))
            print("done: one pass of tour %s (%d s), StopTour sent" % (tid, secs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
