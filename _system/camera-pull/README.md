# camera-pull (Task 34)

Pulls motion clips of the front-yard camera (192.168.31.95) from its SD card over DVRIP into `/srv/library/camera/<date>/ch<N>/<HHMMSS>-<HHMMSS>.mp4` (remuxed, not re-encoded) and keeps `index.sqlite` there. Details, numbers and decisions: `docs/camera-front-yard.md`, `tasks/task-34.md`.

- `camera_pull.py` — timer 08,10,...,20:00. Idempotent; `[R]` regular files never; oldest clips first; per-clip hard cap (`CAMERA_CAP_GB`, default 100) + 30 days + 15 % free floor before every write. Alerts (tier B): no success for 26 h, cap forced deletion of clips younger than 7 days, no room.
- `camera_health.py` — hourly :20. Unreachable twice (not Sat 02:55-03:30), SD error, card loop < 7 days (after 2026-11-19), clock drift > 2 s -> daily `set_time`.
- `camlib.py` — ONE job session at a time (flock `/var/lib/camera-pull/dvrip.lock`); go2rtc's live sessions are outside it (camera `TCPMaxConn` = 10).
- `vendor/` — reviewed `dvrip.py` of OpenIPC/python-dvr (MIT), commit in `vendor/SOURCE.txt`. A newer version only after reading it.
- `.env` (git-ignored, mode 600): `CAM_HOST`, `CAM_USER`, `CAM_PASS` (plain, not URL-encoded). See `.env.example`.
- Tests: `python3 test_camera_pull.py` (fake camera, temp archive, no network, no Telegram).
- Not backed up by design (like the rest of `/srv/library`); restic and the library scanners do not read `/srv/library/camera`.
