# Host-level configuration manifest

Copies for the record, not the live files — editing anything under `host/` changes
nothing on the running system. Live files are on `minisserver` at the paths this
tree mirrors. If a file changes on the box, copy the new version here in the same
commit and update its entry below.

**The test this tree is written against:** if the machine had to be rebuilt from a
fresh Debian install, this manifest plus the rest of `/srv/compose` should be
enough to reach the same behaviour. This exact test was run for real on
2026-09-16 when the previous boot SSD died and took the original `/srv/compose`
(and its git history) with it — this tree, and this manifest, are the rebuild.

---

## `etc/systemd/system.conf.d/no-reboot-watchdog.conf`

**What it does:** sets `RebootWatchdogSec=0`, disabling systemd's use of the
hardware watchdog when performing a `reboot`.

**Why it exists:** this board's AMD FCH SP5100 TCO hardware watchdog gets
armed by systemd's default `RebootWatchdogSec=10min` before every reboot, and
the `sp5100_tco` kernel driver cannot cleanly stop it once armed. Without this
fix, every `reboot`/`shutdown` leaves the machine unreachable for ~11 minutes
until the hardware timer itself forces a reset.

**Added:** originally 2026-09-11 (Task 01), lost with the boot disk, re-created
2026-09-16 (Task 04, Section 3). Same fix, same reasoning — this hardware quirk
doesn't change across a reinstall. **If a reboot ever hangs again, check this
drop-in still exists** before looking anywhere else.

---

## `etc/udev/rules.d/60-seagate-apm.rules`

**What it does:** runs `hdparm -B 254 -S 242 /dev/%k` whenever a block device
with `ID_SERIAL_SHORT=WDE6X73A` (the Seagate ST1000LM035) is added or changed.

**Why it exists:** the Seagate's load/unload cycle count is pinned at its
normalized floor (SMR head-park wear). APM (Advanced Power Management) is
volatile — it resets on every power cycle — so without this rule, aggressive
head parking re-arms on every boot and the counter keeps climbing. Keyed on
the drive's own serial via udev, deliberately not on `/dev/sdX` (device
letters shuffle between boots on this machine, confirmed) or a `by-id` path
(the `by-id` name itself changed when the drive moved from the USB dock to
the native SATA bay — `usb-ST1000LM_...` became `ata-ST1000LM035-1RK172_...`).

**Added:** 2026-09-11 (Task 02b), applied but deliberately left non-persistent
pending the move to native SATA. Made persistent 2026-09-16 (Task 04, Section
2.4) now that the drive is actually on native SATA. Verified via
`udevadm trigger --action=change /dev/sdb` → `hdparm -B` reads `APM_level = 254`.
**Not yet "proven"** — the load/unload counter needs to be confirmed stopped
climbing after about a week of uptime with this rule in place.

---

## `etc/fstab`

**What it does:** current filesystem table.

| Mount | Device | UUID | Notes |
|---|---|---|---|
| `/` | NVMe p2 | `ed96e0cc-6c6b-49ed-b81e-701192752d71` | |
| `/boot/efi` | NVMe p1 | `580A-A44A` | |
| swap | NVMe p3 | `7c09276d-d793-4d04-aac4-9561299b1815` | disk swap, priority -2 — behind zram (priority 100) |
| `/srv/apps` | NVMe p4 | `b62812fe-9430-44d1-b4e9-52e4a5e2ad15` | `errors=remount-ro` |
| `/srv/data` | NVMe p5 | `83004607-fc8f-44c4-aec1-8aaff844350b` | `errors=remount-ro`; Tier-1 lives here until an NVMe with more headroom is dedicated |
| `/srv/library` | Seagate p1 (WDE6X73A) | `ba42438a-b394-41f6-bb46-322f092f3e48` | `nofail`, `errors=remount-ro` — survived the boot-disk death, remounted not reformatted |
| `/srv/vault` | Seagate p2 (WDE6X73A) | `9699630f-f5ea-436e-b1d1-5c6dc9c272fe` | `nofail`, `errors=remount-ro` |
| `/srv/staging` | hynix p1 (EI74N00071140435W) | `6923f79b-bc3b-4c6b-bdb9-1a814fbfea43` | `nofail`, `errors=remount-ro` — torrent staging, most-worn flash in the machine, deliberately holding only transient data |

**Why it exists:** baseline layout from the 2026-09-16 rebuild (Task 04). The
NVMe partitions were laid out by Ivan manually at install time (Section 1 of
task-04.md is the authoritative record of sizes and rationale — LVM was
deliberately dropped this time). `nofail` on every non-boot, non-root entry is
non-negotiable on a headless machine: a missing disk without it drops to an
emergency shell, which on this box means a physical visit.

**Added:** 2026-09-16, Task 04.

---

## `etc/default/zramswap`

**What it does:** configures `zram-tools` — `PERCENT=50`, `PRIORITY=100`
(package defaults, unchanged).

**Why it exists:** zram swap ahead of the disk swap partition. `/sbin/swapon
--show` confirms the ordering: `zram0` at priority 100, the NVMe swap
partition at priority -2 — zram gets used first, the disk partition is a
backstop.

**Added:** 2026-09-16, Task 04, Section 3 (re-applying what Phase 2 originally
set up, lost with the boot disk).

---

## `/etc/sudoers.d/skit-nopasswd` — described here, NOT copied

**What it does:** grants user `skit` `NOPASSWD:ALL` sudo access.

**Why it exists:** non-interactive SSH automation cannot answer an
interactive sudo password prompt. The actual security boundary is the SSH
key: key-only authentication, passwords disabled, passphrase-protected
private key. Not mirrored verbatim here since sudoers content is sensitive to
typos and better inspected live with `visudo -c`.

**Added:** 2026-09-11 (Task 01), re-created 2026-09-16 by Ivan as part of
Section 0 of `tasks/task-04.md` (console access restore, done before the
agent could reach the machine at all). Do not remove without providing
automation another way to escalate.

---

## `~/.ssh/id_ed25519_srv_compose` (skit's home dir) — described here, NOT copied

**What it does:** dedicated ed25519 deploy key, used only for pushing `/srv/compose` to its GitHub remote (`Skitaletsodessa/minisserver-compose`, private). Referenced via an SSH config alias `github.com-srv-compose` in `~/.ssh/config` so it doesn't interfere with any other SSH identity on this box.

**Why it exists:** hard rule 9 — `/srv/compose` needs an off-machine remote, and this machine has no reverse network path to Ivan's desktop. A GitHub deploy key scoped to one repository (write access, nothing else) is a smaller blast radius than a personal access token tied to Ivan's whole account.

**Added:** 2026-09-16, Task 04, Section 4. The private key and `~/.ssh/config` entry are not mirrored here (same reasoning as the sudoers file below) — if the key is ever rotated, generate a new one, update the GitHub deploy-key setting, and update this description with the date.

---

## `etc/docker/daemon.json`

**What it does:** points Docker's storage (`data-root`) at `/srv/apps/docker` instead of the default `/var/lib/docker` (which would land on `/`), caps container log growth (`json-file`, 10m × 3 files per container), and explicitly sets `"live-restore": false`.

**Why it exists:** `/` is a small root partition by design; Docker images/containers/volumes are write-heavy and rebuildable, so they belong on `/srv/apps`, not root. Unbounded container logs are one of the two most common ways a box like this fills `/`.

**Added:** 2026-09-16, Task 04, Section 5 (re-applying the Task 01 convention after the rebuild).

**Updated, 2026-09-18, Task 07 Section 0.2:** added `"live-restore": false` explicitly. It turns out this key was never set before, meaning live-restore was already running at Docker's own default (`false`) the whole time — so Task 05's attribution of the qBittorrent port-binding bug to a live-restore quirk may have been wrong; the real cause was never conclusively identified. Made explicit here anyway so it's never ambiguous, and because this task removes the `qbittorrent-ensure.service` workaround that depended on it not mattering (see below) — verified with a real reboot that port bindings survive without the workaround, live-restore setting aside.

**Correction, 2026-09-18, same day, Task 07 Section 7:** the reboot verification above was real, but incomplete — it happened to land on a boot where `/srv/library` mounted before Docker started. A later reboot (still the same day, after Jellyfin/Navidrome were added) landed on the opposite timing and both `qbittorrent` and `jellyfin` came up with **empty, stale bind mounts** for `/srv/library` — confirmed via `docker exec jellyfin ls /media/video/movies/` returning nothing while the host's real directory had four real entries, and `stat` showing a different device/inode entirely. Root cause, confirmed directly from `journalctl -b`: `docker.service` has no dependency on `srv-library.mount`, and the fstab entry's `nofail` means systemd doesn't order them automatically — so on a boot where the Seagate's mount takes a few seconds longer than usual, Docker starts and bind-mounts containers against the empty pre-mount placeholder directory instead of the real filesystem. **The same class of race as Task 05's dhcpcd/Docker one, but for a disk mount instead of a network interface.** Real fix, same philosophy as that one (remove the race, don't out-wait it): `etc/systemd/system/docker.service.d/10-wait-for-library.conf` (see below), not a workaround on the container side. Verified by a further reboot showing the correct order in `journalctl -b` (`Mounted srv-library.mount` → `Starting docker.service` → containers start) and both mounts correct afterward.

---

## `etc/samba/smb.conf`

**What it does:** standalone (not AD DC) Samba file server exporting only `/srv/library` as a read-only, authenticated share named `library`. SMB3 minimum (SMB1 explicitly refused), bound only to the `eno1` interface (`bind interfaces only = yes` — this also excludes loopback, so testing must use the LAN IP, not `localhost`).

**Why it exists:** MyHomeLib (Windows client) reads the fb2/ebook archive over SMB. Read-only removes the entire category of "a client deleted the archive"; authenticated (not guest) restricts it to a dedicated `smbshare` system user (`useradd -M -s /usr/sbin/nologin`, Samba password set via `smbpasswd`, not a real login account) rather than opening it to anyone on the LAN.

**Deliberately NOT containerized**, unlike every other service in this project. Samba's correctness here depends on host-level UID/GID semantics and file permissions on `/srv/library` in a way that adds real complexity in a container (UID mapping between host and container, socket/broadcast behavior for NetBIOS); it's also one of the most mature, heavily-used pieces of the Debian base system — about as "boring" and battle-tested a solution as exists for this exact job. Docker earns its place for app-shaped services (Scrutiny, qBittorrent); this is closer to core OS plumbing.

**`samba-ad-dc` was pulled in by the `samba` metapackage but explicitly disabled** (`systemctl disable --now samba-ad-dc`) — an Active Directory domain controller is not wanted or provisioned here; only `smbd`/`nmbd` (classic standalone file server) run.

**Added:** 2026-09-16, Task 03.

**Updated, 2026-09-18, Task 07 Section 1.3:** added a second share, `[dropbox]`, exporting only `/srv/library/downloads` (not the whole library), writable, same `eno1`-only binding and SMB3 minimum, same `valid users = smbshare`. `force user = skit` / `force group = skit` on this share specifically, so a file dropped via SMB lands with identical ownership (`skit:skit`) to a file qBittorrent completes (its container runs `PUID=1000`/`PGID=1000`, which is `skit`) — verified directly, not assumed: both a real qBittorrent download and a manual `smbclient put` produce byte-identical `uid:gid`. `create mask = 0664` / `directory mask = 0775`. Checked with `smbclient -L` that `/srv/data` and `/srv/vault` are still not exported by anything — only `library` and `dropbox` appear. `[library]` itself is untouched: still read-only, still the whole `/srv/library` tree, confirmed still refusing writes after this change.

---

## `etc/systemd/docker.service.d/10-wait-for-dhcp.conf`

**What it does:** adds a 5-second `ExecStartPre=/bin/sleep 5` delay before Docker starts.

**Why it exists:** found during Task 05's reboot verification — `qbittorrent` (bound to the specific LAN IP `192.168.31.2:8081`, not `0.0.0.0`) failed to start after a reboot with "cannot assign requested address". Root cause: this box's DHCP client (`dhcpcd`, invoked by ifupdown's `dhcp` method) is not tracked as its own systemd unit and daemonizes quickly — `networking.service`/`network-online.target` complete once the interface is administratively up, not once `dhcpcd` has actually secured a lease and configured the address. `docker.service` already has `After=network-online.target`, which does not help here since that target fires too early for this specific DHCP setup. Docker's own container restart policy did not recover from this — the container ended up in a stable `Exited` state, not a retry loop.

**Verified:** reproduced the race directly (`journalctl -u docker` showed the exact `failed to bind host port 192.168.31.2:8081/tcp: cannot assign requested address` error at the boot immediately following this fix's predecessor state), then confirmed a clean reboot with this drop-in in place starts `qbittorrent` successfully with no manual intervention.

**Only affects binding-to-a-specific-IP services** (currently just qBittorrent, since Samba starts later via its own service and Scrutiny/Caddy bind to loopback/tailscale0 which come up on a different timeline than `eno1`'s DHCP lease) — but the fix is applied at the Docker daemon level since that's what actually races, not per-container.

**Added:** 2026-09-17, Task 05.

**Correction, 2026-09-17, same day:** the "confirmed" verification above was wrong — it checked `docker ps` status ("Up") but not the actual port binding, which was silently empty (`docker inspect ... NetworkSettings.Ports` → `{}`) on that boot. A second, independent reboot reproduced the exact same "cannot assign requested address" failure the sleep was supposed to fix — 5 seconds was not consistently enough, since dhcpcd's DHCPDISCOVER/OFFER/ACK cycle starts at a variable point after `networking.service` completes and takes a variable few seconds itself. **This file (`10-wait-for-dhcp.conf`) has been removed.** Superseded by a real fix: `eno1` moved to a static IP (see `etc/network/interfaces` below), which removes the DHCP negotiation — and therefore this whole race — entirely, rather than trying to out-wait it.

---
## `etc/network/interfaces` — `eno1` moved from DHCP to static

**What it does:** `eno1` now gets `192.168.31.2/24` via a static `iface eno1 inet static` stanza instead of `iface eno1 inet dhcp`, with `dns-nameservers 192.168.31.1` alongside it.

**Why it exists:** two reasons, one immediate and one precautionary, both from the same conversation. Immediate: DHCP was the direct cause of a boot-time race that killed qBittorrent's LAN-IP binding on every reboot (see the superseded `docker.service.d` entry above) — a sleep-based workaround proved unreliable because the DHCP negotiation's timing varies, so removing the negotiation entirely is the real fix, not a longer guess. Precautionary, per Ivan directly: even though the router has a DHCP reservation for this machine's MAC, a reservation is only as good as the router honoring it, and a headless box in a basement should not depend on that surviving a router reset/replacement/misconfiguration.

**Verified:** `ip route show default` unaffected (same gateway, same address), confirmed via a full reboot that `eno1` has `192.168.31.2` immediately with no DHCP log lines at all in that boot's journal.

**Added:** 2026-09-17, Task 05.

**Correction, 2026-09-17, same day:** the DHCP race was genuinely gone, but qBittorrent's port binding was *still* empty after that same verification reboot (`docker inspect qbittorrent --format '{{json .NetworkSettings.Ports}}'` → `{}`, confirmed via `ss -tlnp` showing no listener) — this time with **zero errors logged anywhere** in `journalctl -u docker -b`. The container's stored `HostConfig.PortBindings` was correct; the runtime forwarding silently wasn't set up on dockerd's live-restore. A separate bug from the DHCP race, coincidentally hitting the same container. See `qbittorrent/qbittorrent-ensure.service` below for the fix. **Second time in the same day this file's "verified" claim about qBittorrent was wrong because it checked `docker ps`/route state instead of the actual port binding — checking real listener state, not container status, is now the standing bar for this kind of verification.**

**Also caused a DNS regression, discovered when pushing this exact commit:** switching `eno1` to static removed dhcpcd from the boot process entirely, but nothing else on this box was reading the interfaces file's `dns-nameservers 192.168.31.1` line — the `resolvconf` package that ifupdown needs to actually apply it wasn't installed. Previously dhcpcd wrote `/etc/resolv.conf` directly; with dhcpcd gone, `/etc/resolv.conf` (and Tailscale's `/etc/resolv.pre-tailscale-backup.conf`, which it uses as the upstream for non-tailnet DNS) were left with dhcpcd's last, nameserver-less template — `tailscaled` logged "no upstream resolvers set, returning SERVFAIL" for every external hostname the moment Tailscale's DNS (`100.100.100.100`) tried to forward a non-tailnet query. Fixed by installing `resolvconf` (standard Debian package, `apt install resolvconf`) so ifupdown's `dns-nameservers` line is actually applied on every `ifup`; `tailscaled` was restarted afterward and now integrates with `resolvconf` directly instead of doing its own backup/restore dance on `/etc/resolv.conf`. **Verified:** `getent hosts github.com` and `getent hosts minisserver.tail6bf4d5.ts.net` both resolve correctly after the fix, and `git push` from `/srv/compose` (which needs `github.com` to resolve) succeeded.

---

## `qbittorrent/qbittorrent-ensure.service`

**What it does:** systemd oneshot unit, `After=docker.service`, runs `docker compose -f /srv/compose/qbittorrent/compose.yaml up -d --force-recreate` on every boot. Symlinked into `/etc/systemd/system/` and enabled.

**Why it exists:** dockerd's live-restore mechanism silently failed to restore qBittorrent's `192.168.31.2:8081` port forwarding on a reboot, even after the DHCP race (above) was independently fixed — the container came up `Up` in `docker ps` with the correct `HostConfig.PortBindings` stored, but no actual listener (`ss -tlnp` showed nothing, `docker inspect ... NetworkSettings.Ports` → `{}`), and nothing was logged in `journalctl -u docker -b` to explain it. Forcing a clean recreate every boot sidesteps whatever live-restore is getting wrong, rather than trying to diagnose dockerd internals further.

**Verified:** `sudo systemctl start qbittorrent-ensure.service` recreates the container cleanly; `docker inspect qbittorrent --format '{{json .NetworkSettings.Ports}}'` shows the real binding and `ss -tlnp` shows an active listener on `192.168.31.2:8081` immediately after. Full-reboot verification pending as of this entry — see `docs/measurements.md` for the outcome.

**Added:** 2026-09-17, Task 05.

**Removed, 2026-09-18, Task 07 Section 0.2.** The per-container `-ensure.service` pattern doesn't scale — Task 07 adds two more containers, Immich (task-08) adds four or more, and each would need its own copy of this workaround against a bug whose failure mode is silent. `/etc/docker/daemon.json` now sets `live-restore: false` explicitly instead (see above), and a real reboot with the ensure-service gone confirmed every container's port binding survives correctly via `docker inspect`/`ss -tlnp` — not just `qbittorrent`'s. Kept in this manifest as history rather than deleted outright, per the project's own convention for superseded fixes (see the `10-wait-for-dhcp.conf` entry above).

---

## `resolvconf` package (no config file of its own worth mirroring)

**What it does:** standard Debian package that lets ifupdown's `dns-nameservers`/`dns-search` options in `/etc/network/interfaces` actually populate `/etc/resolv.conf`, via the `/etc/network/if-up.d/000resolvconf` hook. Not present in the original DHCP-based setup because dhcpcd wrote `/etc/resolv.conf` directly and nothing else needed to.

**Why it exists:** moving `eno1` to a static IP (see above) removed dhcpcd, and with it the only thing that had ever populated `/etc/resolv.conf` on this box — a regression discovered only when `git push` failed with a DNS resolution error right after the static-IP change. Installing `resolvconf` is the boring, standard fix rather than hand-rolling a script to write `/etc/resolv.conf`.

**Verified:** `getent hosts github.com` resolves; Tailscale's MagicDNS (`*.tail6bf4d5.ts.net`) also still resolves, confirming `tailscaled` correctly picked up the `resolvconf` integration instead of falling back to its own direct-file-replacement mode.

**Added:** 2026-09-17, Task 05. Rebuild note: if this box is ever reinstalled from scratch, `apt install resolvconf` must happen *before* or alongside setting `eno1` to static, or DNS will silently break the same way.

---

## `etc/systemd/docker.service.d/10-wait-for-library.conf`

**What it does:** `RequiresMountsFor=/srv/library /srv/staging /srv/vault` — tells systemd `docker.service` may not start until all three of these filesystems are actually mounted.

**Why it exists:** on some boots, `/srv/library` (the Seagate, spinning disk, slower to enumerate than the NVMe) was still mounting when Docker started and bind-mounted containers against it — Docker had no ordering dependency on it at all, and the fstab entry's `nofail` (needed so a missing/failed disk doesn't block boot entirely) means systemd does not order dependents against it automatically either. Containers that started in that window got a bind mount pointing at the empty pre-mount placeholder directory instead of the real filesystem, silently — `docker inspect` still reported the mount as correct. Confirmed directly via `journalctl -b`: `srv-library.mount` finishing *after* `docker.service` had already started containers, on the reboot that exposed this. Same race shape as the `10-wait-for-dhcp.conf` / static-IP saga above (Task 05), but for a disk mount instead of a network interface — same fix philosophy too: make the real dependency explicit instead of guessing at a delay. `/srv/staging` and `/srv/vault` added alongside `/srv/library` pre-emptively, since they're mounted from the same physical disks and would race the same way.

**Verified:** a further reboot's `journalctl -b` shows the correct order (`Mounted srv-library.mount` → `Starting docker.service` → containers start), and both `qbittorrent`'s and `jellyfin`'s bind mounts were correct immediately, with no force-recreate needed.

**Added:** 2026-09-18, Task 07 Section 7.
