"""Task 34: the camera module of the gateway (Phase 2/3). The gateway is the ONLY thing that talks to go2rtc (127.0.0.1) and the only reader of the clip
archive (mounted read-only); the browser/app never sees either. No camera login is ever held here: go2rtc has it. Nothing of this module writes anywhere.

  live      go2rtc `/api/stream.mp4?src=cam_chN[_h264]` relayed as an authenticated fragmented-MP4 response (<video src>), plus `/api/frame.jpeg` snapshots
  archive   /ro/camera/index.sqlite (written by the host job camera_pull.py) + the mp4 files it names; a file is served only if it is in the index and
            resolves inside the archive directory (a path from the database is never trusted blindly)
"""
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .auth import GwError

CHUNK = 64 * 1024
SNAPSHOT_TIMEOUT = 15
OPEN_TIMEOUT = 15
CODECS = {"h265": "", "h264": "_h264"}
RETRY_PAUSE = 4.0
AT_SLACK_S = 15


class LiveStream:
    """A go2rtc response plus an optional companion that must stay open as long as it does (the plain consumer that keeps a cold source alive).
    drain=True reads and discards the response in a thread until it is closed or ends."""
    def __init__(self, resp, companion, drain=False, ttl=None):
        self.resp, self.companion, self.closed = resp, companion, False
        if drain:
            threading.Thread(target=self._drain, daemon=True).start()
        if ttl:                                      # the reaper: whatever happens to the request, the stream is closed after `ttl` seconds
            t = threading.Timer(ttl, self.close)
            t.daemon = True
            t.start()

    def _drain(self):
        try:
            while not self.closed and self.resp.read(CHUNK):
                pass
        except (OSError, ValueError, AttributeError):         # AttributeError: close() from another thread empties the socket under read()
            pass

    def read(self, n):
        return self.resp.read(n)

    def close(self):
        self.closed = True
        for r in (self.resp, self.companion):
            try:
                if r is not None:
                    r.close()
            except (OSError, ValueError, AttributeError):
                pass


class Cameras:
    def __init__(self, go2rtc_url, archive_dir, channels, clock, opener=None, live_ttl=1800 + 60, meta_dir=None):
        self.live_ttl = live_ttl
        self.base = (go2rtc_url or "").rstrip("/")
        self.archive = archive_dir
        self.meta = meta_dir or archive_dir                 # index.sqlite, status.json and the night staging area `clips/` (NVMe; the library disk stays idle at night)
        self.stage_clips = os.path.join(meta_dir, "clips") if meta_dir else None
        self.channels = list(channels)
        self.clock = clock
        self.opener = opener or urllib.request.build_opener()
        self.sessions = {}                         # (person, camera) -> last time a snapshot was audited
        self.lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.base)

    def get(self, cid):
        for c in self.channels:
            if c["id"] == cid:
                return c
        raise GwError(404, "not_found", "no such camera", {"id": cid})

    def public_list(self):
        return [{"id": c["id"], "name": c["name"], "channel": c["channel"]} for c in self.channels]

    def audit_snapshot_due(self, person, cid, every=600):
        """A tile refreshes every few seconds: one audit row per person and camera per `every` seconds, not one per image."""
        now = self.clock()
        with self.lock:
            last = self.sessions.get((person, cid), 0)
            if now - last >= every:
                self.sessions[(person, cid)] = now
                return True
        return False

    # ── go2rtc ────────────────────────────────────────────────────────────────
    def _url(self, path, src):
        return "%s%s?src=%s" % (self.base, path, urllib.parse.quote(src, safe="_"))

    def snapshot(self, cid):
        """JPEG bytes of the current frame (blocking: call in an executor)."""
        c = self.get(cid)
        try:
            with self.opener.open(urllib.request.Request(self._url("/api/frame.jpeg", c["src"])), timeout=SNAPSHOT_TIMEOUT) as r:
                data = r.read(4 * 1024 * 1024)
        except (urllib.error.URLError, OSError, ValueError):
            raise GwError(503, "camera_unavailable", "the camera did not answer")
        if len(data) < 200:
            raise GwError(503, "camera_unavailable", "the camera did not answer")
        return data

    def _open_plain(self, src, attempts=3):
        """Opens the camera's own (H.265) stream `src` in go2rtc, with retries (the main stream gets ONE try: when the camera does not deliver it, the page falls back at once)."""
        for attempt in range(1, attempts + 1):
            try:
                return self.opener.open(urllib.request.Request(self._url("/api/stream.mp4", src)), timeout=OPEN_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == attempts:
                    break
                time.sleep(RETRY_PAUSE)
            except (urllib.error.URLError, OSError, ValueError):
                break
        raise GwError(503, "camera_unavailable", "the camera did not answer")

    def open_live(self, cid, codec, quality="sub"):
        """Opens the live stream (fragmented MP4) and returns an object with read()/close() (blocking: call in an executor).
        quality "sub" = the camera's sub stream (640x360, what the public door may carry); "hd" = its MAIN stream (720p H.264 / the camera's own H.265), TAILNET ONLY
        (the API refuses it through the door).
        h265 = the camera's own stream. h264 = go2rtc's ffmpeg transcode, which (measured 2026-11-11, cam1 and cam2) FAILS to open while the source is cold
        ("Invalid data found") and works once a consumer already holds the source: so the gateway first attaches a plain consumer, waits for its first bytes
        (a cold DVRIP source needs 5-20 s), keeps it attached and drained for as long as the h264 stream runs, and only then opens the transcode."""
        c = self.get(cid)
        if codec not in CODECS:
            raise GwError(422, "invalid_codec", "codec must be h265 or h264")
        if quality not in ("sub", "hd"):
            raise GwError(422, "invalid_quality", "quality must be sub or hd")
        base = c["src"] + ("_hd" if quality == "hd" else "")
        tries = 1 if quality == "hd" else 3
        if codec == "h265":
            return LiveStream(self._open_plain(base, tries), None, ttl=self.live_ttl)
        holder = self._open_plain(base, tries)
        try:
            if not holder.read(1):                       # first byte = the source is running
                raise OSError("empty stream")
        except (OSError, ValueError):
            holder.close()
            raise GwError(503, "camera_unavailable", "the camera did not answer")
        drain = LiveStream(holder, None, drain=True)
        src = base + CODECS[codec]
        for attempt in (1, 2, 3):
            try:
                return LiveStream(self.opener.open(urllib.request.Request(self._url("/api/stream.mp4", src)), timeout=OPEN_TIMEOUT), drain, ttl=self.live_ttl)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    break
                time.sleep(RETRY_PAUSE / 2)
            except (urllib.error.URLError, OSError, ValueError):
                break
        drain.close()
        raise GwError(503, "camera_unavailable", "the camera did not answer")

    def live_ok(self):
        try:
            with self.opener.open(urllib.request.Request(self.base + "/api"), timeout=3) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # ── archive ───────────────────────────────────────────────────────────────
    def _db(self):
        path = os.path.join(self.meta, "index.sqlite")
        if not os.path.exists(path):
            return None
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _ccol(db):
        """`corrupt` when the index has the column (camera-pull adds it, Task 35b), else a constant NULL: an old index keeps working."""
        has = any(r[1] == "corrupt" for r in db.execute("PRAGMA table_info(clips)"))
        return "corrupt" if has else "NULL AS corrupt"

    def days(self):
        db = self._db()
        if db is None:
            return []
        try:
            rows = db.execute("SELECT substr(begin,1,10) AS day, ch, count(*) AS n, sum(size) AS bytes FROM clips GROUP BY day, ch ORDER BY day DESC").fetchall()
        finally:
            db.close()
        out = {}
        for r in rows:
            d = out.setdefault(r["day"], {"day": r["day"], "clips": 0, "bytes": 0, "by_channel": {}})
            d["clips"] += r["n"]
            d["bytes"] += r["bytes"] or 0
            d["by_channel"]["cam%d" % r["ch"]] = r["n"]
        return list(out.values())

    def clips(self, day, cid=None):
        c = self.get(cid) if cid else None
        db = self._db()
        if db is None:
            return []
        try:
            q, a = "SELECT rowid AS id, ch, kind, begin, end, size, dur, %s FROM clips WHERE substr(begin,1,10)=?" % self._ccol(db), [day]
            if c:
                q += " AND ch=?"
                a.append(c["channel"])
            rows = db.execute(q + " ORDER BY begin, ch", a).fetchall()
        finally:
            db.close()
        names = {x["channel"]: x["id"] for x in self.channels}
        return [{"id": r["id"], "camera": names.get(r["ch"], "cam%d" % r["ch"]), "kind": r["kind"], "begin": r["begin"], "end": r["end"],
                 "size": r["size"], "duration_s": round(r["dur"] or 0, 1), "damaged": bool(r["corrupt"])} for r in rows]

    def at(self, moment, slack_s=AT_SLACK_S):
        """The synchronised view: for each lens the clip that covers `moment` ('YYYY-MM-DD HH:MM:SS') or None, with the offset into the clip.
        A lens whose clip starts up to `slack_s` seconds AFTER the moment (the lenses trigger a second or two apart) is returned too, offset 0 and delay_s = how much later it
        starts, so one event is one event (Ivan 2026-11-11: clips at 13:15:26 and 13:15:27 were not recognised as the same event)."""
        from datetime import datetime
        t = datetime.strptime(moment, "%Y-%m-%d %H:%M:%S")
        db = self._db()
        out = []
        cc = self._ccol(db) if db is not None else "NULL AS corrupt"
        for c in self.channels:
            clip = None
            if db is not None:
                r = db.execute("SELECT rowid AS id, kind, begin, end, size, dur, %s FROM clips WHERE ch=? AND begin<=? AND end>=? ORDER BY begin DESC LIMIT 1" % cc, (c["channel"], moment, moment)).fetchone()
                if r:
                    off = max(0.0, (t - datetime.strptime(r["begin"], "%Y-%m-%d %H:%M:%S")).total_seconds())
                    off = min(off, max(0.0, (r["dur"] or 0) - 0.5))
                    clip = {"id": r["id"], "camera": c["id"], "kind": r["kind"], "begin": r["begin"], "end": r["end"], "size": r["size"],
                            "duration_s": round(r["dur"] or 0, 1), "offset_s": round(off, 1), "delay_s": 0.0, "damaged": bool(r["corrupt"])}
                else:
                    r = db.execute("SELECT rowid AS id, kind, begin, end, size, dur, %s FROM clips WHERE ch=? AND begin>? AND begin<=datetime(?, ?) ORDER BY begin LIMIT 1" % cc,
                                   (c["channel"], moment, moment, "+%d seconds" % slack_s)).fetchone()
                    if r:
                        late = (datetime.strptime(r["begin"], "%Y-%m-%d %H:%M:%S") - t).total_seconds()
                        clip = {"id": r["id"], "camera": c["id"], "kind": r["kind"], "begin": r["begin"], "end": r["end"], "size": r["size"],
                                "duration_s": round(r["dur"] or 0, 1), "offset_s": 0.0, "delay_s": round(late, 1), "damaged": bool(r["corrupt"])}
            out.append({"camera": c["id"], "clip": clip})
        if db is not None:
            db.close()
        return out

    def step(self, moment, direction, gap_s=30):
        """The previous/next EVENT across all lenses: the nearest clip start more than `gap_s` seconds away (the three lenses often trigger together: one step = one event)."""
        if direction not in ("next", "prev"):
            raise GwError(400, "bad_query", "dir must be next or prev")
        db = self._db()
        if db is None:
            return None
        try:
            if direction == "next":
                r = db.execute("SELECT min(begin) AS t FROM clips WHERE begin > datetime(?, ?)", (moment, "+%d seconds" % gap_s)).fetchone()
            else:
                r = db.execute("SELECT max(begin) AS t FROM clips WHERE begin < datetime(?, ?)", (moment, "-%d seconds" % gap_s)).fetchone()
        finally:
            db.close()
        return r["t"] if r else None

    def status(self, now=None):
        """The pull job's status (status.json next to the index) plus the time of the next scheduled pull (timer: 08..20 every 2 h). None if the job never wrote one."""
        import json
        from datetime import datetime, timedelta
        now = now or datetime.fromtimestamp(self.clock())
        try:
            with open(os.path.join(self.meta, "status.json")) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return None
        until = None                                           # start of the newest archived or staged clip: what the list really reaches (newest-first pulling leaves holes behind it)
        db = self._db()
        if db is not None:
            try:
                row = db.execute("SELECT max(begin) AS t FROM clips").fetchone()
                until = row["t"] if row else None
            finally:
                db.close()
        until_label = None
        if until:
            until_label = until[11:16] if until[:10] == now.strftime("%Y-%m-%d") else "%s.%s %s" % (until[8:10], until[5:7], until[11:16])
        slot = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=15 * (now.minute // 15 + 1))      # the timer fires every 15 minutes, around the clock
        fresh = int(st.get("fresh_backlog") or 0)
        fresh_ok = st.get("fresh_ok")
        return {"backlog": int(st.get("backlog") or 0), "fresh_backlog": fresh, "loading": bool(st.get("backlog")) or bool(fresh) or st.get("stopped") == "time budget",
                "last_success": st.get("last_success"), "fresh_ok": fresh_ok, "fresh_ok_hhmm": fresh_ok[11:16] if fresh_ok else None, "staged": int(st.get("staged") or 0), "until": until, "until_label": until_label,
                "listing_complete_until": st.get("listing_complete_until"), "next_pull": slot.strftime("%H:%M")}

    def clip_file(self, clip_id):
        """The path of one clip, or GwError 404. Only a file named by the index that resolves inside the archive directory is served."""
        db = self._db()
        if db is None:
            raise GwError(404, "not_found", "no such clip")
        try:
            row = db.execute("SELECT rowid AS id, ch, begin, path FROM clips WHERE rowid=?", (clip_id,)).fetchone()
        finally:
            db.close()
        if not row:
            raise GwError(404, "not_found", "no such clip")
        if self.stage_clips and "/camera-staging/clips/" in row["path"]:        # staged overnight on the NVMe, not yet moved to the library
            root = os.path.realpath(self.stage_clips)
            rel = row["path"].split("/camera-staging/clips/", 1)[1]
        else:
            root = os.path.realpath(self.archive)
            # the index stores the host path (/srv/library/camera/...): the same tree is mounted at self.archive in the container
            rel = row["path"].split("/camera/", 1)[-1] if "/camera/" in row["path"] else os.path.basename(row["path"])
        real = os.path.realpath(os.path.join(root, rel))
        if not real.startswith(root + os.sep) or not real.endswith(".mp4") or not os.path.isfile(real):
            raise GwError(404, "not_found", "no such clip")
        return real, row

    def health(self):
        db = self._db()
        newest, count, total = None, 0, 0
        if db is not None:
            try:
                r = db.execute("SELECT max(begin) AS m, count(*) AS n, sum(size) AS s FROM clips").fetchone()
                newest, count, total = r["m"], r["n"], r["s"] or 0
            finally:
                db.close()
        return {"live": self.live_ok() if self.enabled else False, "archive_clips": count, "archive_bytes": total, "newest_clip": newest}
