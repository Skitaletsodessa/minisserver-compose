# piserver host mirror (Task 22)

Copies for the record - editing them changes nothing until `bootstrap-pi.sh` installs them.
Same convention as `/srv/compose/host/` on the server.

| File | Why |
|---|---|
| `etc/ssh/sshd_config.d/10-piserver.conf` | key-only SSH, no root login, `AllowUsers skit`. `10-` is read before the distro's `50-*`. |
| `etc/nftables.conf` | default-deny inbound; SSH from LAN and tailnet, ICMP, DHCP reply, **DNS 53 from the LAN (Task 21)**. Own table only, Docker's untouched. |
| `etc/NetworkManager/conf.d/10-piserver-dns.conf` | global DNS 1.1.1.1/8.8.8.8: the Pi's own resolver must not depend on the server (Task 21 rule 1). |
| `etc/systemd/journald.conf.d/10-sdwear.conf` | persistent but capped journal, 5-minute sync (SD wear). |
| `etc/apt/apt.conf.d/20auto-upgrades` | periodic apt off: no automatic update or restart (rule 5). |
| `usr/local/bin/pi-report` | read-only health report (throttle, temperature, SD writes, pending updates); run by the server's forced-command key `server-report-key.pub` (authorized in `~skit/.ssh/authorized_keys` with `restrict,command=`). |
| (not mirrored) `/etc/sudoers.d/skit-nopasswd` | `skit NOPASSWD:ALL`, as on the server: the SSH key is the security boundary. Created by `cleanup-pi.sh` / `bootstrap-pi.sh`. |
| (edited in place) `/boot/firmware/config.txt` | `dtparam=audio=off`, `camera_auto_detect=0`; I2C/SPI/1-wire left off (DC-UPS work is parked). |
| `usr/local/bin/adguard-watch-origin` + `etc/systemd/system/adguard-watch-origin.{service,timer}` | Task 21 mutual watchdog: every minute the Pi checks that the server's AdGuard really answers; 3 failures -> one Telegram message, recovery -> one. Needs `/etc/piserver/telegram.env` (mode 600, **not in git**, installed by `deploy-pi.sh` from the server's existing bot credentials). |
