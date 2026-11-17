#!/usr/bin/env python3
"""Offline tests of camera_ptz.py: a fake camera that records every OPPTZControl, a temp request/status directory, no network. Run: python3 test_camera_ptz.py"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="ptztest-")
os.environ["CAMERA_STATE_DIR"] = os.path.join(TMP, "state")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import camlib  # noqa: E402
camlib.STATE_DIR = os.environ["CAMERA_STATE_DIR"]
camlib.LOCK_PATH = os.path.join(camlib.STATE_DIR, "dvrip.lock")
import camera_ptz as P  # noqa: E402

CONFIG = {"PTZTour": [[{"Id": 0, "Name": "", "Tour": [{"Id": 1, "Name": "", "Time": 3}, {"Id": 3, "Name": "", "Time": 3}]},
                       {"Id": 1, "Name": "", "Tour": [{"Id": 1, "Name": "", "Time": 5}, {"Id": 2, "Name": "", "Time": 5}, {"Id": 3, "Name": "", "Time": 5}]}]]}
ENV = {"CAM_HOST": "x", "CAM_USER": "u", "CAM_PASS": "p"}
NOW = datetime(2026, 11, 17, 15, 0, 0)


class FakeCam:
    def __init__(self, ret=100, boom_on_start=False):
        self.calls, self.ret, self.boom = [], ret, boom_on_start

    def __call__(self, host, user=None, password=None):
        return self

    def login(self):
        return True

    def close(self):
        pass

    def get_command(self, name, code=None):
        return CONFIG

    def set_command(self, name, data, code=None):
        self.calls.append((name, data["Command"], data["Parameter"]["Tour"], data["Parameter"]["Channel"]))
        if self.boom and data["Command"] == "StartTour":
            raise OSError("camera dropped the connection")
        return {"Ret": self.ret if data["Command"] == "StartTour" else 100}


def fresh():
    d = os.path.join(TMP, "ptz")
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    camlib.STATE_DIR = os.environ["CAMERA_STATE_DIR"]
    return d


def request(d, tour=1, at=NOW, rid="r1"):
    with open(os.path.join(d, "request.json"), "w") as f:
        json.dump({"id": rid, "tour": tour, "by": "ivan", "at": at.strftime(P.FMT)}, f)


def status(d):
    return json.load(open(os.path.join(d, "status.json")))


class Ptz(unittest.TestCase):
    def test_tours_are_read_from_the_camera_and_one_pass_is_dwell_plus_travel(self):
        t = P.read_tours(FakeCam())
        self.assertEqual(t[1], [(1, 5), (2, 5), (3, 5)])
        self.assertEqual(P.cycle_seconds(t[1]), 15 + 3 * P.TRAVEL_S)

    def test_a_tour_runs_once_start_wait_one_cycle_stop(self):
        cam, slept = FakeCam(), []
        secs, pts = P.run_tour(cam, 1, sleep=slept.append)
        self.assertEqual([c[1] for c in cam.calls], ["StartTour", "StopTour"])
        self.assertEqual({c[2] for c in cam.calls}, {1})                                  # the tour id goes to the camera
        self.assertEqual({c[3] for c in cam.calls}, {0})                                  # the PTZ lens (channel 0)
        self.assertEqual(slept, [secs])
        self.assertEqual(len(pts), 3)

    def test_stop_is_sent_even_when_waiting_fails(self):
        cam = FakeCam()

        def boom(_s):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            P.run_tour(cam, 1, sleep=boom)
        self.assertEqual(cam.calls[-1][1], "StopTour")

    def test_unknown_tour_is_refused_and_nothing_is_sent(self):
        cam = FakeCam()
        with self.assertRaises(KeyError):
            P.run_tour(cam, 7, sleep=lambda s: None)
        self.assertEqual(cam.calls, [])

    def test_a_refusal_of_the_camera_is_an_error_not_a_silent_wait(self):
        cam = FakeCam(ret=607)
        with self.assertRaises(RuntimeError):
            P.run_tour(cam, 1, sleep=lambda s: None)

    def test_request_runs_and_the_status_tells_the_page(self):
        d = fresh()
        request(d)
        cam = FakeCam()
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=cam, sleep=lambda s: None), "done")
        st = status(d)
        self.assertEqual((st["state"], st["tour"], st["by"], st["presets"]), ("done", 1, "ivan", [1, 2, 3]))
        self.assertTrue(st["started"] and st["ended"])
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=FakeCam(), sleep=lambda s: None), "already handled")     # the same request never runs twice

    def test_a_stale_request_is_ignored(self):
        d = fresh()
        request(d, at=NOW - timedelta(minutes=10))
        cam = FakeCam()
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=cam, sleep=lambda s: None), "stale")
        self.assertEqual(cam.calls, [])
        self.assertEqual(status(d)["state"], "failed")

    def test_a_tour_the_camera_does_not_have_fails_with_a_message(self):
        d = fresh()
        request(d, tour=9)
        cam = FakeCam()
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=cam, sleep=lambda s: None), "no such tour")
        self.assertEqual(status(d)["state"], "failed")
        self.assertEqual(cam.calls, [])

    def test_a_camera_error_ends_in_failed_and_stop_is_attempted(self):
        d = fresh()
        request(d)
        cam = FakeCam(boom_on_start=True)
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=cam, sleep=lambda s: None), "failed")
        self.assertEqual(status(d)["state"], "failed")

    def test_no_request_does_nothing(self):
        d = fresh()
        self.assertEqual(P.run_request(ENV, d, NOW, cam_factory=FakeCam(), sleep=lambda s: None), "no request")


if __name__ == "__main__":
    unittest.main()
