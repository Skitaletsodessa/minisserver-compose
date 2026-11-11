#!/usr/bin/env python3
"""Task 34 / D2: pull the camera's motion clips from its SD card over DVRIP into /srv/library/camera/, remuxed to mp4 (no re-encoding).
Idempotent (a clip already in the index is skipped, an interrupted run resumes), one DVRIP session at a time, oldest clips first (the
card's loop deletes the oldest first). Not continuous recording: `[R]` regular files are never pulled. Hard cap enforced BEFORE every write."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

import camlib

ARCHIVE = os.environ.get("CAMERA_ARCHIVE", "/srv/library/camera")
CAP_BYTES = int(float(os.environ.get("CAMERA_CAP_GB", "100")) * 1e9)      # proposed 2026-11-11: 100 GB keeps /srv/library <= ~78 % used (disk-space-watch alerts at 80 %)
RETENTION_DAYS = int(os.environ.get("CAMERA_RETENTION_DAYS", "30"))
FLOOR_FREE_PCT = float(os.environ.get("CAMERA_FLOOR_FREE_PCT", "15"))     # never write if the disk would keep < 15 % free
MAX_CLIP_KB = int(os.environ.get("CAMERA_MAX_CLIP_MB", "600")) * 1024     # download_file holds the whole clip in memory
MAX_RUN_SECONDS = int(os.environ.get("CAMERA_MAX_RUN_SECONDS", "3300"))
SETTLE_SECONDS = 120                                                      # a clip that ended less than 2 min ago may still be open
YOUNG_DAYS = 7                                                            # forced deletion of clips younger than this = tier-B alert
STALE_HOURS = 26
STATE_PATH = os.path.join(camlib.STATE_DIR, "pull-state.json")
CHANNELS = (0, 1, 2)
FMT = "%Y-%m-%d %H:%M:%S"
KIND_RE = re.compile(r"\[([A-Za-z])\]")


def parse_kind(file_name):
    m = KIND_RE.search(file_name)
    return m.group(1).upper() if m else "?"


def kb(hexstr):
    return int(hexstr, 16) if str(hexstr).startswith("0x") else int(hexstr)


def out_path(archive, ch, begin, end):
    b = datetime.strptime(begin, FMT)
    e = datetime.strptime(end, FMT)
    return os.path.join(archive, b.strftime("%Y-%m-%d"), f"ch{ch}", f"{b.strftime('%H%M%S')}-{e.strftime('%H%M%S')}.mp4")


def plan_deletions(rows, incoming, cap, free, total, floor_pct, retention_days, now):
    """rows = [(id, begin_str, size)]. Returns (ids_to_delete, fits): after deleting them, size+incoming <= cap and the free space
    after the write is >= floor_pct % of the disk. Retention-expired rows always go; then the oldest rows until it fits."""
    rows = sorted(rows, key=lambda r: r[1])
    cutoff = (now - timedelta(days=retention_days)).strftime(FMT)
    delete = [r[0] for r in rows if r[1] < cutoff]
    keep = [r for r in rows if r[1] >= cutoff]
    used = sum(r[2] for r in keep)
    free_now = free + sum(r[2] for r in rows if r[1] < cutoff)
    floor = total * floor_pct / 100.0
    i = 0
    while i < len(keep) and (used + incoming > cap or free_now - incoming < floor):
        used -= keep[i][2]
        free_now += keep[i][2]
        delete.append(keep[i][0])
        i += 1
    fits = used + incoming <= cap and free_now - incoming >= floor
    return delete, fits


def db_open(archive):
    os.makedirs(archive, exist_ok=True)
    db = sqlite3.connect(os.path.join(archive, "index.sqlite"))
    db.execute("""CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, ch INTEGER, kind TEXT, begin TEXT, end TEXT, cam_kb INTEGER,
                  size INTEGER, path TEXT, dur REAL, fps REAL, pulled_at TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS clips_begin ON clips(begin)")
    return db


def real_remux(raw, out, listed_seconds):
    pr = subprocess.run(["ffprobe", "-v", "error", "-f", "hevc", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                         "-of", "csv=p=0", raw], capture_output=True, text=True, timeout=60)
    fps = 12.0
    try:
        a, b = pr.stdout.strip().split("/")
        if float(b) > 0 and 1 <= float(a) / float(b) <= 60:
            fps = float(a) / float(b)
    except (ValueError, IndexError):
        pass
    part = out + ".part.mp4"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "hevc", "-framerate", f"{fps:g}", "-i", raw, "-c", "copy",
                        "-movflags", "+faststart", part], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        if os.path.exists(part):
            os.unlink(part)
        raise RuntimeError("remux failed: " + (r.stderr or "")[:200])
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", part],
                       capture_output=True, text=True, timeout=60)
    dur = float(d.stdout.strip() or 0)
    if dur <= 0:
        os.unlink(part)
        raise RuntimeError("remuxed file has no duration")
    os.replace(part, out)
    return dur, fps


GO2RTC_API = os.environ.get("CAMERA_GO2RTC_API", "http://127.0.0.1:18984/api/streams")
YIELD_POLL_S = 10
YIELD_MAX_WAIT_S = 900


def live_busy(url=GO2RTC_API):
    """True while anybody is WATCHING a lens (a consumer on any go2rtc stream). The camera's Wi-Fi link is the bottleneck: a backfill download starves a live view
    (measured 2026-11-11: HD live failed and cold sub streams took 17-25 s while the pull ran), so the pull waits between clips. An unreachable API means idle."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return False
    return any(v.get("consumers") for v in (data or {}).values() if isinstance(v, dict))


ABORT_POLL_S = 2


class Aborted(Exception):
    pass


def download_unless_watched(cam, c, raw, busy, poll=ABORT_POLL_S):
    """cam.download_file(...) that is cut off as soon as somebody starts watching a lens (the camera's Wi-Fi link is the bottleneck)."""
    import socket as _socket
    import threading
    done = threading.Event()
    hit = []

    def watch():
        while not done.wait(poll):
            if busy():
                hit.append(1)
                try:
                    cam.socket.shutdown(_socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                return
    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        cam.download_file(c["begin"], c["end"], c["name"], raw)
    except Exception:
        if hit:
            raise Aborted()
        raise
    finally:
        done.set()
    if hit:
        raise Aborted()


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


def list_candidates(cam, since, now, have_ids):
    """New non-regular clips of all channels, oldest first. One query per day and channel (the library pages past 64 results)."""
    out = []
    day = since.date()
    while day <= now.date():
        for ch in CHANNELS:
            files = cam.list_local_files(f"{day} 00:00:00", f"{day} 23:59:59", "h264", ch)
            for f in files:
                kind = parse_kind(f["FileName"])
                if kind == "R":
                    continue
                cid = f"{ch}|{f['FileName']}"
                if cid in have_ids:
                    continue
                end = datetime.strptime(f["EndTime"], FMT)
                if (now - end).total_seconds() < SETTLE_SECONDS or datetime.strptime(f["BeginTime"], FMT) < since:
                    continue
                out.append({"id": cid, "ch": ch, "kind": kind, "name": f["FileName"], "begin": f["BeginTime"], "end": f["EndTime"],
                            "kb": kb(f["FileLength"])})
        day += timedelta(days=1)
    out.sort(key=lambda c: (c["begin"], c["ch"]))
    return out


def run(env, now=None, cam_factory=None, remux=real_remux, archive=ARCHIVE, cap=None, disk=None, busy=None, sleep=time.sleep):
    now = now or datetime.now()
    cap = CAP_BYTES if cap is None else cap
    cam_factory = cam_factory or camlib.DVRIPCam
    st = load_state()
    db = db_open(archive)
    t0 = time.time()
    busy = busy or live_busy
    res = {"pulled": 0, "bytes": 0, "deleted": 0, "forced_young": 0, "errors": 0, "backlog": 0, "stopped": "", "yielded_s": 0}
    have = {r[0] for r in db.execute("SELECT id FROM clips")}
    last = st.get("last_list_ok")
    since = (datetime.strptime(last, FMT) - timedelta(hours=3)) if last else now - timedelta(days=8)
    since = max(since, now - timedelta(days=8)).replace(microsecond=0)
    try:
        with camlib.dvrip_session(env, cam_factory=cam_factory) as cam:
            cands = list_candidates(cam, since, now, have)
        listed_at = now.strftime(FMT)
    except camlib.CameraBusy:
        res["stopped"] = "camera busy"
        db.close()
        return res
    res["backlog"] = len(cands)
    for c in cands:
        if time.time() - t0 > MAX_RUN_SECONDS:
            res["stopped"] = "time budget"
            break
        waited = 0                                                             # a live view comes first: wait (between clips) while somebody watches
        while busy() and waited < YIELD_MAX_WAIT_S and time.time() - t0 < MAX_RUN_SECONDS:
            sleep(YIELD_POLL_S)
            waited += YIELD_POLL_S
        res["yielded_s"] += waited
        if c["kb"] > MAX_CLIP_KB:
            res["skipped_big"] = res.get("skipped_big", 0) + 1          # not an error: it must not hold the cursor back for ever
            print(f"skip {c['id']}: {c['kb'] // 1024} MB over the {MAX_CLIP_KB // 1024} MB limit")
            continue
        total, _used, free = disk() if disk else shutil.disk_usage(archive)
        rows = list(db.execute("SELECT id, begin, size FROM clips"))
        incoming = c["kb"] * 1024
        dele, fits = plan_deletions(rows, incoming, cap, free, total, FLOOR_FREE_PCT, RETENTION_DAYS, now)
        young_cut = (now - timedelta(days=YOUNG_DAYS)).strftime(FMT)
        young = [r for r in rows if r[0] in dele and r[1] >= young_cut]
        for cid in dele:
            row = db.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()
            if row and os.path.exists(row[0]):
                os.unlink(row[0])
            db.execute("DELETE FROM clips WHERE id=?", (cid,))
        db.commit()
        res["deleted"] += len(dele)
        res["forced_young"] += len(young)
        if not fits:
            res["stopped"] = "no room (cap or free-space floor)"
            break
        tmpdir = os.path.join(archive, ".tmp")
        os.makedirs(tmpdir, exist_ok=True)
        raw = os.path.join(tmpdir, f"{c['ch']}-{re.sub(r'[^0-9]', '', c['begin'])}.h265")
        out = out_path(archive, c["ch"], c["begin"], c["end"])
        try:
            with camlib.dvrip_session(env, cam_factory=cam_factory) as cam:
                download_unless_watched(cam, c, raw, busy)
            if os.path.getsize(raw) < c["kb"] * 1024 * 0.9:
                raise RuntimeError(f"short download {os.path.getsize(raw)} of {c['kb'] * 1024}")
            listed = (datetime.strptime(c["end"], FMT) - datetime.strptime(c["begin"], FMT)).total_seconds()
            dur, fps = remux(raw, out, listed)
            size = os.path.getsize(out)
            db.execute("INSERT OR REPLACE INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (c["id"], c["ch"], c["kind"], c["begin"], c["end"], c["kb"], size, out, dur, fps, now.strftime(FMT)))
            db.commit()
            res["pulled"] += 1
            res["bytes"] += size
        except camlib.CameraBusy:
            res["stopped"] = "camera busy"
            break
        except Aborted:                             # somebody started watching: not an error, the clip is simply retried
            res["aborted_for_live"] = res.get("aborted_for_live", 0) + 1
            if os.path.exists(out + ".part.mp4"):
                os.unlink(out + ".part.mp4")
        except Exception as e:                      # one bad clip must not stop the run; it is retried next time (not in the index)
            res["errors"] += 1
            print(f"error {c['id']}: {type(e).__name__}: {str(e)[:160]}")
            if os.path.exists(out + ".part.mp4"):
                os.unlink(out + ".part.mp4")
        finally:
            if os.path.exists(raw):
                os.unlink(raw)
    shutil.rmtree(os.path.join(archive, ".tmp"), ignore_errors=True)
    for dp, dn, fn in os.walk(archive, topdown=False):      # empty day/channel directories left by retention
        if dp != archive and not os.listdir(dp):
            os.rmdir(dp)
    st["last_attempt"] = now.strftime(FMT)
    if res["stopped"] in ("", "time budget"):
        st["last_success"] = now.strftime(FMT)
    if res["stopped"] == "" and res["errors"] == 0:
        st["last_list_ok"] = listed_at       # the listing cursor moves ONLY when every candidate of it was handled; otherwise the next run lists from the same point again
                                             # (found 2026-11-11: it moved after a run that stopped on its time budget and ~3200 clips on the card were never looked at again)
    st["last_result"] = res
    if res["forced_young"]:
        st["last_forced_young"] = now.strftime(FMT)
    save_state(st)
    db.close()
    try:
        write_status(archive, st, res, now)
    except OSError:
        pass                                     # the status file is a convenience for the page; never fail the run over it
    return res


def write_status(archive, st, res, now):
    """/srv/library/camera/status.json: what the gateway shows next to the archive ("part of the clips is not loaded from the camera yet"). Atomic, world-readable, no secret."""
    doc = {"updated": now.strftime(FMT), "last_success": st.get("last_success"), "listing_complete_until": st.get("last_list_ok"),
           "backlog": res.get("backlog", 0), "pulled_last_run": res.get("pulled", 0), "stopped": res.get("stopped", ""), "errors": res.get("errors", 0)}
    tmp = os.path.join(archive, ".status.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(archive, "status.json"))


def alerts(st, now, notify):
    ls = st.get("last_success")
    if ls and (now - datetime.strptime(ls, FMT)).total_seconds() > STALE_HOURS * 3600:
        notify.alert("camera-pull-stale", f"Камера: клипы не забирались с {ls} (больше {STALE_HOURS} ч). Проверьте камеру .95 и задачу camera-pull.", tier="B")
    else:
        notify.clear("camera-pull-stale", "")
    fy = st.get("last_forced_young")
    if fy and (now - datetime.strptime(fy, FMT)).total_seconds() < 24 * 3600:
        notify.alert("camera-pull-cap-young", "Камера: архив клипов упёрся в лимит места и удалил клипы моложе 7 дней. Нужен диск побольше или меньше записи.", tier="B")
    else:
        notify.clear("camera-pull-cap-young", "")
    if st.get("last_result", {}).get("stopped", "").startswith("no room"):
        notify.alert("camera-pull-noroom", "Камера: клипы не копируются — нет места (лимит архива или 15 % свободного на /srv/library).", tier="B")
    else:
        notify.clear("camera-pull-noroom", "")


def main():
    env = camlib.load_env()
    now = datetime.now()
    failed = False
    try:
        res = run(env, now)
    except (OSError, camlib.CameraLoginFailed, camlib.DVRIPSomethingWrong) as e:     # camera unreachable: not a unit failure
        res = {"stopped": f"camera unreachable: {type(e).__name__}"}            # (camera_health alerts after 2 polls, the 26 h stale alert is the backstop)
    except Exception as e:
        res = {"stopped": f"{type(e).__name__}: {str(e)[:120]}"}
        failed = True
    print(json.dumps(res))
    import watchnotify as N
    alerts(load_state(), now, N)
    sys.exit(1 if failed else 0)       # unreachable camera = failed unit (OnFailure Telegram); the 26 h stale alert is the quiet backstop


if __name__ == "__main__":
    main()
