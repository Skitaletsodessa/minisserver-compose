# auto-update — everything updates itself, and every update is checked (Task 35 C, Ivan 2026-11-13)

Replaces the old rule 5 ("no automatic updates that restart services"). Notify-only Diun stays and still announces tags the machine will not take by itself.

## What runs when

| What | When | By | Safety |
|---|---|---|---|
| OS packages (all Debian origins incl. -updates, Docker, Tailscale) | daily 03:30 server / 03:33 Pi | `unattended-upgrades` (`52minis-unattended-upgrades`) | no automatic reboot; previous kernel kept (`Remove-Unused-Kernel-Packages false`, `NeverAutoRemove`) |
| Restart if needed | **server Wed + Sat 04:30**, **Pi Tue + Fri 04:30** (retries every 30 min until 07:45) | `minis-reboot-if-needed.timer` -> `reboot-if-needed.sh` | only if `/var/run/reboot-required` exists AND the OTHER machine's AdGuard answers DNS; otherwise postponed + ONE tier-B message. The camera reboots Sat 03:00 (>= 1.5 h earlier) |
| Containers | **Sunday 11:00** (server only) | `minis-container-update.timer` -> `container-update.py` + `stacks.json` | one service at a time, health check, rollback at once, tier-B message; policies: patch / minor (Immich, after a DB dump) / digest / ha (x.y.1+, only after `restore-test-ha.sh` passes with the candidate) / build (own images on the new python base) / manual |
| Checks after an update or a start | after every apt pass that installed something, after every boot (+4 min server / +3 min Pi), after the container run | `post-update-run.sh` -> `post-update-check.sh` (server) / `pi-post-update-check` (Pi) | ONE summary line in `summary.log` (server, git) / `/var/log/minis-update-summary.log` (Pi, in `pi-report`); on any FAIL ONE tier-B message naming what was updated |

Not automated on purpose (policy `manual`, Diun announces): AdGuard origin + replica (must move the same day), Immich Postgres (pinned with its extension versions), qBittorrent (linuxserver tag scheme), the Caddy image (custom build).

## Look at it
- `tail /srv/compose/_system/auto-update/summary.log`, `container-updates.jsonl` (one JSON line per container outcome), `journalctl -u minis-container-update -u minis-reboot-if-needed -u minis-post-update-boot`.
- Plan without changing anything: `python3 container-update.py --dry-run [--only NAME]`.
- Pause everything: `sudo systemctl disable --now minis-container-update.timer minis-reboot-if-needed.timer` (and on the Pi the same timer; `APT::Periodic::Unattended-Upgrade "0"` stops the packages).

## Roll back (one command each)
- **A container the run updated:** the previous image is kept under the tag `<repo>:minis-prev` (the containerd store deletes untagged images at once). `git -C /srv/compose revert --no-edit <the "auto-update: NAME ..." commit>` then `cd /srv/compose/<stack> && docker compose up -d --no-deps --force-recreate <service>`. A rollback inside the run does this itself and sends a tier-B message.
- **One of our own images** (gateway, schedules, relay): `docker tag portal-gateway:minis-prev portal-gateway:local && cd /srv/compose/portal/gateway && docker compose up -d --no-deps --force-recreate portal-gateway` and `git revert` the Dockerfile commit; then `check-bind.sh`.
- **A package:** versions are in `/var/log/apt/history.log`: `sudo apt-get install <package>=<old version>`.
- **The kernel:** the previous kernel is still installed: `sudo grub-reboot "Advanced options for Debian GNU/Linux>Debian GNU/Linux, with Linux <old version>" && sudo reboot` (Pi: `kernel=` in `/boot/firmware/config.txt`, previous `kernel*.img` stays; check with a keyboard-less plan first).

## Tests
- `bash test-auto-update.sh` (restart guard + post-update run, fakes, no reboot, no Telegram, no git): 17 checks.
- `python3 test_container_update.py` (version choice, refs, compose rewrite, quiet hours): 13 tests.
- `bash live-test-container-update.sh` (needs docker; a scratch registry + toy images, never a real service): 25 checks: good update, dry-run, rollback of a bad update, floating tag, own-image rebuild incl. failing smoke test and failing health check. Re-run after ANY change to `container-update.py`.

## Traps found while building this
- The containerd image store removes an image the moment it loses its last tag: a rollback needs a tag (`minis-prev`) set BEFORE the build/pull.
- `digest` policy on an unpinned tag must compare with the digest of what RUNS, not with the compose file (there is none).
- `docker compose up` after a `build:` service: rollback must re-point the image tag, the Dockerfile text alone is not enough.
- Quoting: never nest `<<EOF` inside `ssh 'single quotes'` with an apostrophe in the text; write a file and `scp` it.
