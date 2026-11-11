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


class LiveStream:
    """A go2rtc response plus an optional companion that must stay open as long as it does (the plain consumer that keeps a cold source alive).
    drain=True reads and discards the response in a thread until it is closed or ends."""
    def __init__(self, resp, companion, drain=False):
        self.resp, self.companion, self.closed = resp, companion, False
        if drain:
            threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        try:
            while not self.closed and self.resp.read(CHUNK):
                pass
        except (OSError, ValueError):
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
    def __init__(self, go2rtc_url, archive_dir, channels, clock, opener=None):
        self.base = (go2rtc_url or "").rstrip("/")
        self.archive = archive_dir
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

    def _open_plain(self, c):
        """Opens the camera's own (H.265) stream, with retries."""
        for attempt in (1, 2, 3):
            try:
                return self.opener.open(urllib.request.Request(self._url("/api/stream.mp4", c["src"])), timeout=OPEN_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    break
                time.sleep(RETRY_PAUSE)
            except (urllib.error.URLError, OSError, ValueError):
                break
        raise GwError(503, "camera_unavailable", "the camera did not answer")

    def open_live(self, cid, codec):
        """Opens the live stream (fragmented MP4) and returns an object with read()/close() (blocking: call in an executor).
        h265 = the camera's own stream. h264 = go2rtc's ffmpeg transcode, which (measured 2026-11-11, cam1 and cam2) FAILS to open while the source is cold
        ("Invalid data found") and works once a consumer already holds the source: so the gateway first attaches a plain consumer, waits for its first bytes
        (a cold DVRIP source needs 5-20 s), keeps it attached and drained for as long as the h264 stream runs, and only then opens the transcode."""
        c = self.get(cid)
        if codec not in CODECS:
            raise GwError(422, "invalid_codec", "codec must be h265 or h264")
        if codec == "h265":
            return LiveStream(self._open_plain(c), None)
        holder = self._open_plain(c)
        try:
            if not holder.read(1):                       # first byte = the source is running
                raise OSError("empty stream")
        except (OSError, ValueError):
            holder.close()
            raise GwError(503, "camera_unavailable", "the camera did not answer")
        drain = LiveStream(holder, None, drain=True)
        src = c["src"] + CODECS[codec]
        for attempt in (1, 2, 3):
            try:
                return LiveStream(self.opener.open(urllib.request.Request(self._url("/api/stream.mp4", src)), timeout=OPEN_TIMEOUT), drain)
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
        path = os.path.join(self.archive, "index.sqlite")
        if not os.path.exists(path):
            return None
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

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
            q, a = "SELECT rowid AS id, ch, kind, begin, end, size, dur FROM clips WHERE substr(begin,1,10)=?", [day]
            if c:
                q += " AND ch=?"
                a.append(c["channel"])
            rows = db.execute(q + " ORDER BY begin, ch", a).fetchall()
        finally:
            db.close()
        names = {x["channel"]: x["id"] for x in self.channels}
        return [{"id": r["id"], "camera": names.get(r["ch"], "cam%d" % r["ch"]), "kind": r["kind"], "begin": r["begin"], "end": r["end"],
                 "size": r["size"], "duration_s": round(r["dur"] or 0, 1)} for r in rows]

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
