#!/usr/bin/env python3
"""Task 34 / D2, 34c: pull the camera's motion clips from its SD card over DVRIP into /srv/library/camera/, remuxed to mp4 (no re-encoding).
Idempotent (a clip already in the index is skipped, an interrupted run resumes), one DVRIP session at a time. Task 34c: every 15 minutes; FRESH clips first,
then BACKFILL oldest first (the card's loop deletes the oldest first); at night clips are staged on the NVMe and move to the library in the morning. Not continuous recording: `[R]` regular files are never pulled. Hard cap enforced BEFORE every write."""
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
MAX_RUN_SECONDS = int(os.environ.get("CAMERA_MAX_RUN_SECONDS", "720"))       # soft budget: no new clip after 12 min (the timer fires every 15 min, runs never stack)
SETTLE_SECONDS = 120                                                      # a clip that ended less than 2 min ago may still be open
YOUNG_DAYS = 7                                                            # forced deletion of clips younger than this = tier-B alert
FRESH_STALE_HOURS = 2                                                     # tier B when no successful FRESH run for this long (Task 34c; was 26 h for any run)
META = os.environ.get("CAMERA_META_DIR", "/var/lib/camera-staging")       # NVMe: index.sqlite, status.json, night staging (clips/), temporary downloads
NIGHT_FROM, NIGHT_TO = 21, 8                                              # clips pulled in [21:00, 08:00) go to the staging area; the library disk stays idle
STAGING_CAP_BYTES = int(float(os.environ.get("CAMERA_STAGING_CAP_GB", "10")) * 1e9)     # backfill stops at this much staged; fresh clips are never held back by it
ROOT_FREE_FLOOR_PCT = 20                                                  # staging never leaves / with less than this free
FRESH_MARGIN = timedelta(hours=3)                                         # fresh = newer than the newest archived clip minus this (catches an earlier failed clip)
BACKFILL_CACHE_H = 6                                                      # the card is re-listed for backfill at most this often
MAX_FAILS = 3                                                             # a clip that failed this many runs is given up (counted in the result)
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


def is_night(now):
    return now.hour >= NIGHT_FROM or now.hour < NIGHT_TO


def make_room(db, c_id, c_begin, incoming, archive, cap, disk, now, res):
    """Room on the library disk for one clip. 'ok' (old clips may have been deleted), 'drop' (the clip is older than what the cap would delete first: pulling it is pointless),
    'noroom' (even deleting everything allowed does not fit)."""
    total, _used, free = disk() if disk else shutil.disk_usage(archive)
    rows = [r for r in db.execute("SELECT id, begin, size FROM clips") if r[0] != c_id]
    dele, fits = plan_deletions(rows, incoming, cap, free, total, FLOOR_FREE_PCT, RETENTION_DAYS, now)
    begins = {r[0]: r[1] for r in rows}
    if any(begins[d] >= c_begin for d in dele):
        return "drop"
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
    return "ok" if fits else "noroom"


def move_staged(db, archive, stage_root, cap, disk, now, res):
    """Day time: clips staged on the NVMe overnight go to the library disk (copy, point the index at the copy, then delete the staged file: a viewer never meets a missing file)."""
    rows = list(db.execute("SELECT id, ch, begin, end, size, path FROM clips WHERE path LIKE ? ORDER BY begin", (stage_root.rstrip("/") + "/%",)))
    for cid, ch, begin, end, size, path in rows:
        if not os.path.exists(path):
            db.execute("DELETE FROM clips WHERE id=?", (cid,))
            db.commit()
            continue
        verdict = make_room(db, cid, begin, size, archive, cap, disk, now, res)
        if verdict == "drop":
            os.unlink(path)
            db.execute("DELETE FROM clips WHERE id=?", (cid,))
            db.commit()
            res["staged_dropped"] = res.get("staged_dropped", 0) + 1
            continue
        if verdict == "noroom":
            res["stopped"] = "no room (cap or free-space floor)"
            return
        dest = out_path(archive, ch, begin, end)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(path, dest + ".part")
        os.replace(dest + ".part", dest)
        db.execute("UPDATE clips SET path=?, size=? WHERE id=?", (dest, os.path.getsize(dest), cid))
        db.commit()
        os.unlink(path)
        res["moved"] = res.get("moved", 0) + 1
    for dp, dn, fn in os.walk(stage_root, topdown=False):
        if dp != stage_root and not os.listdir(dp):
            os.rmdir(dp)


def run(env, now=None, cam_factory=None, remux=real_remux, archive=ARCHIVE, cap=None, disk=None, busy=None, sleep=time.sleep, meta=None, stage_disk=None):
    """One run (Task 34c). meta = the directory of index.sqlite, status.json, the staging area `clips/` and the temporary download (NVMe in production; None = the archive itself,
    no night staging). Order: (day) move staged clips -> FRESH (everything newer than the newest archived clip minus 3 h, oldest first) -> BACKFILL (the rest of the card,
    oldest first, only when no fresh clip is pending and time is left). Night: every clip goes to the staging area while it has room, so the library disk stays idle."""
    now = now or datetime.now()
    cap = CAP_BYTES if cap is None else cap
    cam_factory = cam_factory or camlib.DVRIPCam
    staging = meta is not None
    meta = meta or archive
    stage_root = os.path.join(meta, "clips")
    st = load_state()
    db = db_open(meta)
    t0 = time.time()
    busy = busy or live_busy
    night = staging and is_night(now)
    res = {"pulled": 0, "bytes": 0, "deleted": 0, "forced_young": 0, "errors": 0, "stopped": "", "yielded_s": 0, "fresh_backlog": 0, "fresh_pulled": 0, "backlog": 0, "night": night}
    if staging and not is_night(now):
        move_staged(db, archive, stage_root, cap, disk, now, res)
    have = {r[0] for r in db.execute("SELECT id FROM clips")}
    newest = db.execute("SELECT max(begin) FROM clips").fetchone()[0]
    floor = now - timedelta(days=8)
    fresh_cut = max((datetime.strptime(newest, FMT) - FRESH_MARGIN) if newest else floor, floor).replace(microsecond=0)
    fails = {k: v for k, v in st.get("fails", {}).items() if k not in have}

    def usable(c):
        if fails.get(c["id"], 0) >= MAX_FAILS:
            res["gave_up"] = res.get("gave_up", 0) + 1
            return False
        if c["kb"] > MAX_CLIP_KB:
            res["skipped_big"] = res.get("skipped_big", 0) + 1                 # not an error: it must not hold anything back for ever
            return False
        return True

    try:
        with camlib.dvrip_session(env, cam_factory=cam_factory) as cam:
            fresh = list_candidates(cam, fresh_cut, now, have)
    except camlib.CameraBusy:
        res["stopped"] = "camera busy"
        db.close()
        return res
    fresh = [c for c in fresh if usable(c)]
    res["fresh_backlog"] = len(fresh)

    def pull_loop(cands, backfill):
        """True when every candidate was handled; False when the run has to stop (res['stopped'] says why)."""
        for c in cands:
            if time.time() - t0 > MAX_RUN_SECONDS:
                res["stopped"] = "time budget"
                return False
            waited = 0                                                          # a live view comes first: wait (between clips) while somebody watches
            while busy() and waited < YIELD_MAX_WAIT_S and time.time() - t0 < MAX_RUN_SECONDS:
                sleep(YIELD_POLL_S)
                waited += YIELD_POLL_S
            res["yielded_s"] += waited
            incoming = c["kb"] * 1024
            dest = archive
            if night:
                s_total, _u, s_free = stage_disk() if stage_disk else shutil.disk_usage(meta)
                staged = sum(r[0] or 0 for r in db.execute("SELECT size FROM clips WHERE path LIKE ?", (stage_root.rstrip("/") + "/%",)))
                room = s_free - incoming >= s_total * ROOT_FREE_FLOOR_PCT / 100.0
                if room and (not backfill or staged + incoming <= STAGING_CAP_BYTES):
                    dest = stage_root
                elif backfill:
                    res["stopped"] = "staging full"                              # fresh clips go on to the library disk, backfill waits for the morning
                    return False
            if dest == archive:
                verdict = make_room(db, c["id"], c["begin"], incoming, archive, cap, disk, now, res)
                if verdict == "drop":
                    res["stopped"] = "backfill full"
                    return False
                if verdict == "noroom":
                    res["stopped"] = "no room (cap or free-space floor)"
                    return False
            tmpdir = os.path.join(meta, ".tmp")
            os.makedirs(tmpdir, exist_ok=True)
            raw = os.path.join(tmpdir, f"{c['ch']}-{re.sub(r'[^0-9]', '', c['begin'])}.h265")
            out = out_path(dest, c["ch"], c["begin"], c["end"])
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
                have.add(c["id"])
                fails.pop(c["id"], None)
                res["pulled"] += 1
                res["bytes"] += size
                if not backfill:
                    res["fresh_pulled"] += 1
            except camlib.CameraBusy:
                res["stopped"] = "camera busy"
                return False
            except Aborted:                             # somebody started watching: not an error, the clip is simply retried
                res["aborted_for_live"] = res.get("aborted_for_live", 0) + 1
                if os.path.exists(out + ".part.mp4"):
                    os.unlink(out + ".part.mp4")
            except Exception as e:                      # one bad clip must not stop the run; it is retried next time (a few times, then given up)
                res["errors"] += 1
                fails[c["id"]] = fails.get(c["id"], 0) + 1
                print(f"error {c['id']}: {type(e).__name__}: {str(e)[:160]}")
                if os.path.exists(out + ".part.mp4"):
                    os.unlink(out + ".part.mp4")
            finally:
                if os.path.exists(raw):
                    os.unlink(raw)
        return True

    fresh_done = pull_loop(fresh, False)
    bf = st.get("backfill")
    if fresh_done and time.time() - t0 <= MAX_RUN_SECONDS:
        retention_cut = (now - timedelta(days=RETENTION_DAYS)).strftime(FMT)
        if not bf or (now - datetime.strptime(bf["listed_at"], FMT)).total_seconds() > BACKFILL_CACHE_H * 3600:
            last = st.get("last_list_ok")
            since = max((datetime.strptime(last, FMT) - timedelta(hours=3)) if last else floor, floor).replace(microsecond=0)
            try:
                with camlib.dvrip_session(env, cam_factory=cam_factory) as cam:
                    items = list_candidates(cam, since, now, have)
                bf = {"listed_at": now.strftime(FMT), "items": [c for c in items if c["begin"] < fresh_cut.strftime(FMT)]}
            except camlib.CameraBusy:
                res["stopped"] = "camera busy"
                bf = None
        if bf is not None:
            cands = [c for c in bf["items"] if c["id"] not in have and c["begin"] >= retention_cut and usable(c)]
            res["backlog"] = len(cands)
            done = pull_loop(cands, True)
            left = [c for c in cands if c["id"] not in have]
            if done and not left:
                st["last_list_ok"] = bf["listed_at"]                         # the whole card was looked at and every clip handled
                bf = None
            else:
                res["backlog"] = len(left)
    st["backfill"] = bf
    shutil.rmtree(os.path.join(meta, ".tmp"), ignore_errors=True)
    roots = [stage_root] + ([archive] if (res["deleted"] or res.get("moved") or res.get("staged_dropped")) else [])     # the library disk is only walked when something changed there
    for root in roots:
        for dp, dn, fn in os.walk(root, topdown=False):      # empty day/channel directories left by retention
            if dp != root and not os.listdir(dp):
                os.rmdir(dp)
    st["last_attempt"] = now.strftime(FMT)
    if res["stopped"] in ("", "time budget", "backfill full", "staging full"):
        st["last_success"] = now.strftime(FMT)
        if res["fresh_backlog"] == 0 or res["fresh_pulled"] > 0:
            st["last_fresh_ok"] = now.strftime(FMT)
    st["unreachable"] = False
    st["fails"] = fails
    st["last_result"] = {k: v for k, v in res.items()}
    if res["forced_young"]:
        st["last_forced_young"] = now.strftime(FMT)
    save_state(st)
    staged_n = db.execute("SELECT count(*) FROM clips WHERE path LIKE ?", (stage_root.rstrip("/") + "/%",)).fetchone()[0]
    db.close()
    try:
        write_status(meta, st, res, now, staged_n)
    except OSError:
        pass                                     # the status file is a convenience for the page; never fail the run over it
    return res


def write_status(meta, st, res, now, staged=0):
    """status.json next to the index: what the gateway shows next to the archive («обновлено HH:MM», "part of the clips is not loaded from the camera yet"). Atomic, world-readable, no secret."""
    doc = {"updated": now.strftime(FMT), "last_success": st.get("last_success"), "fresh_ok": st.get("last_fresh_ok"), "listing_complete_until": st.get("last_list_ok"),
           "backlog": res.get("backlog", 0), "fresh_backlog": res.get("fresh_backlog", 0), "pulled_last_run": res.get("pulled", 0), "stopped": res.get("stopped", ""),
           "errors": res.get("errors", 0), "staged": staged, "night": bool(res.get("night"))}
    tmp = os.path.join(meta, ".status.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(meta, "status.json"))


def alerts(st, now, notify):
    lf = st.get("last_fresh_ok")
    if lf and not st.get("unreachable") and (now - datetime.strptime(lf, FMT)).total_seconds() > FRESH_STALE_HOURS * 3600:
        notify.alert("camera-pull-stale", f"Камера: свежие клипы не забирались с {lf} (больше {FRESH_STALE_HOURS} ч). Проверьте задачу camera-pull и камеру .95.", tier="B")
    else:
        notify.clear("camera-pull-stale", "")        # an unreachable camera has its own alert (camera_health)
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
    import fcntl
    os.makedirs(camlib.STATE_DIR, exist_ok=True)
    lock = open(os.path.join(camlib.STATE_DIR, "run.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"stopped": "previous run still going"}))     # the 15-minute timer never stacks runs
        return
    env = camlib.load_env()
    now = datetime.now()
    failed = False
    try:
        res = run(env, now, archive=ARCHIVE, meta=META)
    except (PermissionError, FileNotFoundError) as e:                                 # our own files/directories, not the camera: a real failure (found 2026-11-12: it was reported as "camera unreachable")
        res = {"stopped": f"{type(e).__name__}: {str(e)[:120]}"}
        failed = True
    except (OSError, camlib.CameraLoginFailed, camlib.DVRIPSomethingWrong) as e:     # camera unreachable: not a unit failure (camera_health alerts)
        res = {"stopped": f"camera unreachable: {type(e).__name__}"}
        st = load_state()
        st["unreachable"] = True
        save_state(st)
    except Exception as e:
        res = {"stopped": f"{type(e).__name__}: {str(e)[:120]}"}
        failed = True
    print(json.dumps(res))
    import watchnotify as N
    alerts(load_state(), now, N)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
