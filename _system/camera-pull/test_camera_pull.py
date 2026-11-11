#!/usr/bin/env python3
"""Offline tests for camera_pull.py / camera_health.py: a fake camera, a temp archive, no network, no Telegram. Run: python3 test_camera_pull.py"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="camtest-")
os.environ["CAMERA_STATE_DIR"] = os.path.join(TMP, "state")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import camera_health as H  # noqa: E402
import camera_pull as P    # noqa: E402
import camlib              # noqa: E402

FMT = P.FMT
NOW = datetime(2026, 11, 12, 12, 0, 0)
ENV = {"CAM_HOST": "x", "CAM_USER": "u", "CAM_PASS": "p"}


def fname(ch, begin, end, kind="M", n=0):
    b, e = datetime.strptime(begin, FMT), datetime.strptime(end, FMT)
    return f"/idea0/{b:%Y-%m-%d}/00{ch + 1}/{b:%H.%M.%S}-{e:%H.%M.%S}[{kind}][@a{n:03x}][0].h264"


def entry(ch, begin, end, kb, kind="M", n=0):
    return {"BeginTime": begin, "EndTime": end, "FileLength": hex(kb), "FileName": fname(ch, begin, end, kind, n), "DiskNo": 0, "SerialNo": 0, "_ch": ch}


class FakeCam:
    def __init__(self, files, fail_names=()):
        self.files, self.fail_names, self.downloads, self.logins = files, set(fail_names), [], 0

    def __call__(self, host, user=None, password=None):
        return self

    def login(self):
        self.logins += 1
        return True

    def close(self):
        pass

    def list_local_files(self, start, end, ftype, ch):
        d = start[:10]
        return [{k: v for k, v in f.items() if k != "_ch"} for f in self.files if f["_ch"] == ch and f["BeginTime"].startswith(d)]

    def download_file(self, begin, end, name, target):
        if name in self.fail_names:
            raise OSError("camera dropped the connection")
        kb = next(int(f["FileLength"], 16) for f in self.files if f["FileName"] == name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"\0" * (kb * 1024))
        self.downloads.append(name)


def fake_remux(raw, out, listed):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(b"mp4" * 100)
    return float(listed), 12.0


def fresh():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    os.environ["CAMERA_STATE_DIR"] = os.path.join(TMP, "state")
    camlib.STATE_DIR = os.environ["CAMERA_STATE_DIR"]
    camlib.LOCK_PATH = os.path.join(camlib.STATE_DIR, "dvrip.lock")
    P.STATE_PATH = os.path.join(camlib.STATE_DIR, "pull-state.json")
    H.STATE_PATH = os.path.join(camlib.STATE_DIR, "health-state.json")
    return os.path.join(TMP, "archive")


GB = 10 ** 9
BIG_DISK = lambda: (700 * GB, 400 * GB, 300 * GB)   # noqa: E731


class Planning(unittest.TestCase):
    def test_kind_and_size_parsing(self):
        self.assertEqual(P.parse_kind("/idea0/2026-11-11/001/00.38.38-00.39.08[M][@a792][1].h264"), "M")
        self.assertEqual(P.parse_kind("/x/[R][@1][0].h264"), "R")
        self.assertEqual(P.parse_kind("/x/nothing.h264"), "?")
        self.assertEqual(P.kb("0x00001de6"), 7654)

    def test_retention_and_cap(self):
        rows = [("a", "2026-09-01 10:00:00", 10), ("b", "2026-11-01 10:00:00", 40), ("c", "2026-11-05 10:00:00", 40), ("d", "2026-11-10 10:00:00", 40)]
        dele, fits = P.plan_deletions(rows, 30, cap=100, free=10 ** 6, total=10 ** 6, floor_pct=15, retention_days=30, now=NOW)
        self.assertEqual(dele, ["a", "b", "c"])   # a: older than 30 days; 120+30 > 100 -> b goes (110 > 100) -> c goes -> 40+30 = 70
        self.assertTrue(fits)

    def test_floor_of_free_space(self):
        rows = [("a", "2026-11-10 10:00:00", 50), ("b", "2026-11-11 10:00:00", 50)]
        dele, fits = P.plan_deletions(rows, 20, cap=10 ** 9, free=120, total=1000, floor_pct=15, retention_days=30, now=NOW)
        self.assertEqual(dele, ["a"])             # free 120 - 20 = 100 < 150 floor -> delete the oldest (+50) -> 150 ok
        self.assertTrue(fits)
        dele, fits = P.plan_deletions(rows, 500, cap=10 ** 9, free=120, total=1000, floor_pct=15, retention_days=30, now=NOW)
        self.assertFalse(fits)                    # even after deleting everything the floor cannot be kept

    def test_single_clip_bigger_than_cap(self):
        dele, fits = P.plan_deletions([("a", "2026-11-11 10:00:00", 5)], 200, cap=100, free=10 ** 6, total=10 ** 6, floor_pct=15, retention_days=30, now=NOW)
        self.assertFalse(fits)


class Pull(unittest.TestCase):
    def files(self):
        return [entry(0, "2026-11-12 09:00:00", "2026-11-12 09:00:30", 2000, "M", 1),
                entry(1, "2026-11-12 09:10:00", "2026-11-12 09:10:20", 1000, "M", 2),
                entry(1, "2026-11-12 08:00:00", "2026-11-12 09:00:00", 90000, "R", 3),      # regular hourly file: never pulled
                entry(2, "2026-11-12 11:59:30", "2026-11-12 11:59:59", 500, "M", 4),         # ended 1 s ago: may still be open
                entry(2, "2026-11-12 10:00:00", "2026-11-12 10:00:40", 700 * 1024, "M", 5),  # 700 MB: over the memory limit
                entry(2, "2026-11-12 10:30:00", "2026-11-12 10:30:40", 800, "M", 6)]

    def test_pulls_only_complete_motion_clips_and_is_idempotent(self):
        arch = fresh()
        cam = FakeCam(self.files())
        r = P.run(ENV, NOW, cam_factory=cam, remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertEqual(r["pulled"], 3, r)
        self.assertEqual(r["errors"], 0)
        self.assertEqual(r["skipped_big"], 1)     # the 700 MB clip is skipped and counted, but it does not hold the cursor back
        names = " ".join(cam.downloads)
        self.assertNotIn("[R]", names)
        self.assertNotIn("11.59.30", names)
        self.assertTrue(os.path.exists(os.path.join(arch, "2026-11-12", "ch0", "090000-090030.mp4")))
        cam2 = FakeCam(self.files())
        r2 = P.run(ENV, NOW + timedelta(hours=2), cam_factory=cam2, remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertEqual(r2["pulled"], 1, r2)     # only the clip that was still open at the first run
        self.assertEqual(len(cam2.downloads), 1)
        self.assertEqual(len(os.listdir(arch)) >= 1, True)

    def test_the_listing_cursor_moves_only_when_everything_was_handled(self):
        arch = fresh()
        fs = self.files()
        P.MAX_RUN_SECONDS, keep = -1, P.MAX_RUN_SECONDS                                  # the time budget is over before the first clip
        try:
            r = P.run(ENV, NOW, cam_factory=FakeCam(fs), remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        finally:
            P.MAX_RUN_SECONDS = keep
        self.assertEqual((r["stopped"], r["pulled"]), ("time budget", 0), r)
        self.assertNotIn("last_list_ok", P.load_state())                                  # nothing was handled: the next run looks at the same clips again
        r = P.run(ENV, NOW + timedelta(minutes=5), cam_factory=FakeCam(fs), remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertEqual(r["pulled"], 4, r)                                               # the whole backlog is still found (the clip that was open a moment ago too)
        self.assertIn("last_list_ok", P.load_state())                                     # now everything was handled (the oversized clip is skipped, not an error)
        fresh()
        fs2 = self.files()
        r = P.run(ENV, NOW, cam_factory=FakeCam(fs2, fail_names=[fs2[0]["FileName"]]), remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertEqual(r["errors"], 1)
        self.assertNotIn("last_list_ok", P.load_state())                                  # a failed clip keeps the cursor where it was

    def test_failed_download_is_retried_and_does_not_stop_the_run(self):
        arch = fresh()
        fs = self.files()
        bad = fs[0]["FileName"]
        r = P.run(ENV, NOW, cam_factory=FakeCam(fs, fail_names=[bad]), remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertEqual(r["pulled"], 2, r)
        self.assertEqual(r["errors"], 1)                                         # the failed download (the oversized clip is skipped, not an error)
        self.assertEqual(os.listdir(os.path.join(arch, ".tmp")) if os.path.exists(os.path.join(arch, ".tmp")) else [], [])
        P.save_state({})                          # a retry must see the failed clip again (not in the index)
        r2 = P.run(ENV, NOW + timedelta(hours=2), cam_factory=FakeCam(fs), remux=fake_remux, archive=arch, cap=GB, disk=BIG_DISK)
        self.assertGreaterEqual(r2["pulled"], 1)

    def test_cap_deletes_oldest_first_and_flags_young_clips(self):
        arch = fresh()
        fs = [entry(0, f"2026-11-{d:02d} 09:00:00", f"2026-11-{d:02d} 09:00:30", 1000, "M", d) for d in (8, 9, 10, 11)]
        def one_mb(raw, out, listed):                              # every stored mp4 is 1 MB; incoming is the camera size, 1000 KB
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(b"\0" * 1_000_000)
            return float(listed), 12.0
        r = P.run(ENV, NOW, cam_factory=FakeCam(fs), remux=one_mb, archive=arch, cap=2_500_000, disk=BIG_DISK)
        self.assertEqual(r["pulled"], 4, r)
        left = sorted(P.db_open(arch).execute("SELECT begin FROM clips").fetchall())
        self.assertEqual([x[0][:10] for x in left], ["2026-11-10", "2026-11-11"])      # the two OLDEST were deleted, the newest survive
        self.assertEqual(r["deleted"], 2)
        self.assertEqual(r["forced_young"], 2)                                         # both were younger than 7 days: tier-B material
        self.assertFalse(os.path.exists(os.path.join(arch, "2026-11-08")))             # files and empty directories are gone

    def test_no_room_stops_the_run(self):
        arch = fresh()
        r = P.run(ENV, NOW, cam_factory=FakeCam(self.files()), remux=fake_remux, archive=arch, cap=10, disk=BIG_DISK)
        self.assertTrue(r["stopped"].startswith("no room"), r)
        self.assertEqual(r["pulled"], 0)

    def test_lock_prevents_two_sessions(self):
        fresh()
        with camlib.dvrip_session(ENV, cam_factory=FakeCam([])):
            with self.assertRaises(camlib.CameraBusy):
                with camlib.dvrip_session(ENV, wait=1, cam_factory=FakeCam([])):
                    pass

    def test_output_path_is_built_from_our_own_timestamps(self):
        p = P.out_path("/a", 2, "2026-11-12 09:05:07", "2026-11-12 09:06:00")
        self.assertEqual(p, "/a/2026-11-12/ch2/090507-090600.mp4")


class Recorder:
    def __init__(self):
        self.alerts, self.cleared = [], []

    def alert(self, key, text, tier="A"):
        self.alerts.append((key, tier))

    def clear(self, key, text):
        self.cleared.append(key)


class HealthCam(FakeCam):
    def __init__(self, drift=0.0, storage=None):
        super().__init__([])
        self.drift, self.storage, self.set_calls = drift, storage, 0

    def get_time(self):
        return datetime.now() + timedelta(seconds=self.drift)

    def set_time(self):
        self.set_calls += 1

    def get_command(self, name, code=None):
        return self.storage


def storage(status=0, total="0x0001c278", old="2026-10-01 10:00:00"):
    return [{"PartNumber": 1, "Partition": [{"DirverType": 0, "Status": status, "TotalSpace": total, "OldStartTime": old},
                                           {"DirverType": 4, "Status": 0, "TotalSpace": "0x405", "OldStartTime": "2026-11-11 08:00:00"}]}]


class Failing:
    def __call__(self, *a, **k):
        raise OSError("no route")


class Health(unittest.TestCase):
    def test_reboot_window(self):
        sat = datetime(2026, 11, 14, 3, 5)
        self.assertEqual(sat.weekday(), 5)
        self.assertTrue(H.in_reboot_window(sat))
        self.assertFalse(H.in_reboot_window(datetime(2026, 11, 14, 4, 0)))
        self.assertFalse(H.in_reboot_window(datetime(2026, 11, 13, 3, 5)))

    def test_unreachable_twice_alerts_once_not_in_reboot_window(self):
        fresh()
        n = Recorder()
        H.run(ENV, datetime(2026, 11, 13, 10, 0), cam_factory=Failing(), notify=n)
        self.assertEqual(n.alerts, [])
        H.run(ENV, datetime(2026, 11, 13, 11, 0), cam_factory=Failing(), notify=n)
        self.assertEqual(n.alerts, [("camera-unreachable", "B")])
        fresh()
        n = Recorder()
        for h in (3, 3):                          # Saturday 03:00 window: neither poll counts
            H.run(ENV, datetime(2026, 11, 14, h, 10), cam_factory=Failing(), notify=n)
        self.assertEqual(n.alerts, [])

    def test_recovery_clears(self):
        fresh()
        n = Recorder()
        H.run(ENV, datetime(2026, 11, 13, 10, 0), cam_factory=Failing(), notify=n)
        H.run(ENV, datetime(2026, 11, 13, 11, 0), cam_factory=Failing(), notify=n)
        H.run(ENV, datetime(2026, 11, 13, 12, 0), cam_factory=HealthCam(storage=storage()), notify=n)
        self.assertIn("camera-unreachable", n.cleared)

    def test_sd_problems(self):
        fresh()
        n = Recorder()
        H.run(ENV, datetime(2026, 11, 20, 10, 0), cam_factory=HealthCam(storage=storage(status=2)), notify=n)
        self.assertIn(("camera-sd", "B"), n.alerts)
        fresh()
        n = Recorder()
        H.run(ENV, datetime(2026, 11, 20, 10, 0), cam_factory=HealthCam(storage=[]), notify=n)
        self.assertIn(("camera-sd", "B"), n.alerts)

    def test_short_loop_alert_after_the_grace_period_only(self):
        fresh()
        n = Recorder()
        H.run(ENV, datetime(2026, 11, 12, 10, 0), cam_factory=HealthCam(storage=storage(old="2026-11-08 10:00:00")), notify=n)
        self.assertEqual(n.alerts, [])            # inside the grace period (record config changed 2026-11-11)
        H.run(ENV, datetime(2026, 11, 20, 10, 0), cam_factory=HealthCam(storage=storage(old="2026-11-18 10:00:00")), notify=n)
        self.assertIn(("camera-sd-short", "B"), n.alerts)

    def test_clock_is_set_once_when_drift_exceeds_two_seconds(self):
        fresh()
        cam = HealthCam(drift=-5.0, storage=storage())
        out = H.run(ENV, datetime(2026, 11, 20, 10, 0), cam_factory=cam, notify=Recorder())
        self.assertTrue(out.get("clock_set"))
        self.assertEqual(cam.set_calls, 1)
        cam2 = HealthCam(drift=-5.0, storage=storage())
        H.run(ENV, datetime(2026, 11, 20, 12, 0), cam_factory=cam2, notify=Recorder())
        self.assertEqual(cam2.set_calls, 0)       # not again within 20 h
        cam3 = HealthCam(drift=-1.0, storage=storage())
        H.run(ENV, datetime(2026, 11, 21, 12, 0), cam_factory=cam3, notify=Recorder())
        self.assertEqual(cam3.set_calls, 0)       # drift within the limit


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
