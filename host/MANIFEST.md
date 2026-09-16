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

**What it does:** points Docker's storage (`data-root`) at `/srv/apps/docker` instead of the default `/var/lib/docker` (which would land on `/`), and caps container log growth (`json-file`, 10m × 3 files per container).

**Why it exists:** `/` is a small root partition by design; Docker images/containers/volumes are write-heavy and rebuildable, so they belong on `/srv/apps`, not root. Unbounded container logs are one of the two most common ways a box like this fills `/`.

**Added:** 2026-09-16, Task 04, Section 5 (re-applying the Task 01 convention after the rebuild).

---

## `etc/samba/smb.conf`

**What it does:** standalone (not AD DC) Samba file server exporting only `/srv/library` as a read-only, authenticated share named `library`. SMB3 minimum (SMB1 explicitly refused), bound only to the `eno1` interface (`bind interfaces only = yes` — this also excludes loopback, so testing must use the LAN IP, not `localhost`).

**Why it exists:** MyHomeLib (Windows client) reads the fb2/ebook archive over SMB. Read-only removes the entire category of "a client deleted the archive"; authenticated (not guest) restricts it to a dedicated `smbshare` system user (`useradd -M -s /usr/sbin/nologin`, Samba password set via `smbpasswd`, not a real login account) rather than opening it to anyone on the LAN.

**Deliberately NOT containerized**, unlike every other service in this project. Samba's correctness here depends on host-level UID/GID semantics and file permissions on `/srv/library` in a way that adds real complexity in a container (UID mapping between host and container, socket/broadcast behavior for NetBIOS); it's also one of the most mature, heavily-used pieces of the Debian base system — about as "boring" and battle-tested a solution as exists for this exact job. Docker earns its place for app-shaped services (Scrutiny, qBittorrent); this is closer to core OS plumbing.

**`samba-ad-dc` was pulled in by the `samba` metapackage but explicitly disabled** (`systemctl disable --now samba-ad-dc`) — an Active Directory domain controller is not wanted or provisioned here; only `smbd`/`nmbd` (classic standalone file server) run.

**Added:** 2026-09-16, Task 03.
