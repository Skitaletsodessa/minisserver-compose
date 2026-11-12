# camera-pull (Task 34)

Pulls motion clips of the front-yard camera (192.168.31.95) from its SD card over DVRIP into `/srv/library/camera/<date>/ch<N>/<HHMMSS>-<HHMMSS>.mp4` (remuxed, not re-encoded) and keeps `index.sqlite` there. Details, numbers and decisions: `docs/camera-front-yard.md`, `tasks/task-34.md`.

- `camera_pull.py` — timer every 15 min, around the clock (Task 34c); one run <= 12 min (soft budget), runs never stack (run lock). Order: (day) staged clips move to the library -> FRESH (newer than the newest archived clip minus 3 h, oldest first) -> BACKFILL (rest of the card, oldest first; a clip older than what the cap would delete is not pulled). Night 21:00-08:00: clips go to `/var/lib/camera-staging/clips` (NVMe, cap 10 GB for backfill, never below 20 % free on /), the library disk stays idle; the morning run moves them. `index.sqlite`, `status.json` (with `fresh_ok`) and `pull-state.json` live on the NVMe too. A download is cut when go2rtc shows a viewer. A clip failing 3 runs is given up. `[R]` files never; cap (`CAMERA_CAP_GB`, 100) + 30 days + 15 % free floor before every library write. Alerts (tier B): no successful FRESH run for 2 h (not while the camera is unreachable), cap deleted clips younger than 7 days, no room.
- `camera_health.py` — hourly :20. Unreachable twice (not Sat 02:55-03:30), SD error, card loop < 7 days (after 2026-11-19), clock drift > 2 s -> daily `set_time`.
- `camlib.py` — ONE job session at a time (flock `/var/lib/camera-pull/dvrip.lock`); go2rtc's live sessions are outside it (camera `TCPMaxConn` = 10).
- `vendor/` — reviewed `dvrip.py` of OpenIPC/python-dvr (MIT), commit in `vendor/SOURCE.txt`. A newer version only after reading it.
- `.env` (git-ignored, mode 600): `CAM_HOST`, `CAM_USER`, `CAM_PASS` (plain, not URL-encoded). See `.env.example`.
- Tests: `python3 test_camera_pull.py` (fake camera, temp archive, no network, no Telegram).
- Not backed up by design (like the rest of `/srv/library`); restic and the library scanners do not read `/srv/library/camera`.
