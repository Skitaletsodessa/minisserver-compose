"""Task 34: shared camera access for camera_pull.py and camera_health.py.
ONE DVRIP session at a time from these jobs: every session sits under the same flock (state dir). go2rtc's live sessions are
NOT serialised with it (they cannot be); they stay under the camera's TCPMaxConn=10 (3 live + 1 job + the web UI)."""
import contextlib
import fcntl
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))          # reviewed copy of OpenIPC/python-dvr dvrip.py (vendor/SOURCE.txt)
sys.path.insert(0, "/srv/compose/_system/lib")            # watchnotify
from dvrip import DVRIPCam, SomethingIsWrongWithCamera as DVRIPSomethingWrong  # noqa: E402

STATE_DIR = os.environ.get("CAMERA_STATE_DIR", "/var/lib/camera-pull")
LOCK_PATH = os.path.join(STATE_DIR, "dvrip.lock")


class CameraBusy(Exception):
    pass


class CameraLoginFailed(Exception):
    pass


def load_env(path=None):
    path = path or os.path.join(HERE, ".env")
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


@contextlib.contextmanager
def dvrip_session(env, wait=600, lock_path=None, cam_factory=DVRIPCam):
    """Lock, log in, yield the camera, always log out and unlock. CameraBusy if the lock is not free within `wait` seconds."""
    lp = lock_path or LOCK_PATH
    os.makedirs(os.path.dirname(lp), exist_ok=True)
    fd = open(lp, "a+")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                fd.close()
                raise CameraBusy("another camera job holds the DVRIP lock")
            time.sleep(0.2)          # short poll: the pull job frees the lock only for a moment between clips
    cam = None
    try:
        cam = cam_factory(env["CAM_HOST"], user=env["CAM_USER"], password=env["CAM_PASS"])
        if not cam.login():
            raise CameraLoginFailed("login refused")
        yield cam
    finally:
        try:
            if cam is not None:
                cam.close()
        except Exception:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
