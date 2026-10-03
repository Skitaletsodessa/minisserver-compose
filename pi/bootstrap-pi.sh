#!/bin/bash
# bootstrap-pi.sh - base system for piserver (Task 22 B3). Run as root ON the Pi, from the directory
# that holds host/ (deploy-pi.sh copies it to /home/skit/pi-deploy). Idempotent: a second run changes
# nothing and says so. Starting point: Raspberry Pi OS Lite 32-bit trixie with user skit and the
# server's public key. The Tailscale login is a manual step (printed at the end).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
CHANGED=0
say()  { echo "== $*"; }
did()  { CHANGED=$((CHANGED+1)); echo "   changed: $*"; }

# --- install a mirrored file; report whether it changed -------------------------------------
put() { # put <relative path under host/> <mode>
  local src="$HERE/host/$1" dst="/$1"
  mkdir -p "$(dirname "$dst")"
  if ! cmp -s "$src" "$dst"; then install -m "$2" "$src" "$dst"; did "$dst"; return 0; fi
  return 1
}

say "safety: the server's key must be present before password login is disabled"
grep -q ' skit@minisserver$' /home/skit/.ssh/authorized_keys || { echo "FATAL: server key missing"; exit 1; }

say "identity"
want=piserver
if [ "$(hostnamectl --static)" != "$want" ]; then hostnamectl set-hostname "$want"; did hostname; fi
hosts_line='127.0.1.1 piserver.home.ivandeliver.email piserver'
if ! grep -qxF "$hosts_line" /etc/hosts; then
  sed -i '/^127\.0\.1\.1\b/d' /etc/hosts; echo "$hosts_line" >> /etc/hosts; did /etc/hosts
fi
[ -f /etc/sudoers.d/skit-nopasswd ] || { echo 'skit ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/skit-nopasswd
  chmod 440 /etc/sudoers.d/skit-nopasswd; visudo -cf /etc/sudoers.d/skit-nopasswd && did sudoers; }

say "apt: no automatic anything"
put etc/apt/apt.conf.d/20auto-upgrades 644
for t in apt-daily.timer apt-daily-upgrade.timer; do
  if systemctl is-enabled "$t" >/dev/null 2>&1; then systemctl disable --now "$t" >/dev/null 2>&1; did "disabled $t"; fi
done

say "packages: upgrade, remove what a DNS appliance does not need, add what it does"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
pending=$(apt-get -s full-upgrade | grep -c '^Inst ')
if [ "$pending" -gt 0 ]; then apt-get -y -qq -o Dpkg::Options::=--force-confold full-upgrade >/dev/null 2>&1; did "full-upgrade ($pending packages)"; fi
for p in avahi-daemon triggerhappy bluez cups modemmanager rpi-connect rpi-connect-lite ustreamer udisks2 dphys-swapfile cloud-init; do
  if dpkg -s "$p" 2>/dev/null | grep -q '^Status: install ok installed'; then apt-get -y -qq purge "$p" >/dev/null 2>&1; did "purged $p"; fi
done
apt-get -y -qq autoremove --purge >/dev/null
for p in nftables fake-hwclock python3-yaml curl bind9-dnsutils; do
  dpkg -s "$p" 2>/dev/null | grep -q '^Status: install ok installed' || { apt-get -y -qq install "$p" >/dev/null 2>&1; did "installed $p"; }
done

say "weekly report: forced-command key from the server (can run only pi-report)"
put usr/local/bin/pi-report 755
pub=$(cat "$HERE/server-report-key.pub")
line="restrict,command=\"/usr/local/bin/pi-report\" $pub"
grep -qxF "$line" /home/skit/.ssh/authorized_keys || { echo "$line" >> /home/skit/.ssh/authorized_keys; did "report key authorized"; }

say "SSH: key-only (validated before reload)"
if put etc/ssh/sshd_config.d/10-piserver.conf 644; then sshd -t && systemctl reload ssh || { echo "FATAL sshd config"; exit 1; }; fi

say "host resolver: independent of the server (global DNS in NetworkManager)"
if put etc/NetworkManager/conf.d/10-piserver-dns.conf 644; then systemctl reload NetworkManager; sleep 2; fi

say "SD wear: journald"
if put etc/systemd/journald.conf.d/10-sdwear.conf 644; then systemctl restart systemd-journald; fi

say "firewall"
if put etc/nftables.conf 755; then
  # NOT "systemctl restart nftables": the Debian unit runs "nft flush ruleset" on stop and would wipe Docker's tables.
  nft -c -f /etc/nftables.conf && nft -f /etc/nftables.conf && systemctl enable nftables >/dev/null 2>&1 || { echo "FATAL nft"; exit 1; }
fi
if ! nft list table inet piserver >/dev/null 2>&1; then
  nft -c -f /etc/nftables.conf && nft -f /etc/nftables.conf && did "firewall table loaded into the running kernel"
fi
systemctl is-enabled nftables >/dev/null 2>&1 || { systemctl enable nftables >/dev/null 2>&1; did "enabled nftables"; }

say "clock: fake-hwclock saves hourly and at shutdown (the Pi has no RTC)"
# trixie ships fake-hwclock.service masked; the real units are -load (boot), -save.timer (hourly) and -save (shutdown)
for u in fake-hwclock-load.service fake-hwclock-save.timer fake-hwclock-save.service; do
  systemctl is-enabled "$u" >/dev/null 2>&1 || { systemctl enable "$u" >/dev/null 2>&1; did "enabled $u"; }
done
systemctl is-active fake-hwclock-save.timer >/dev/null 2>&1 || { systemctl start fake-hwclock-save.timer; did "started fake-hwclock-save.timer"; }
fake-hwclock save >/dev/null 2>&1 || true

say "hardware: audio and camera off, I2C/SPI/1-wire stay off"
cfg=/boot/firmware/config.txt
grep -q '^dtparam=audio=on' "$cfg"       && { sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$cfg"; did "audio off"; }
grep -q '^camera_auto_detect=1' "$cfg"   && { sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$cfg"; did "camera detect off"; }

say "mutual DNS watchdog (the Pi checks the server's AdGuard)"
put usr/local/lib/watchnotify.py 644      # shared alert helper (symlink to _system/lib in git; deploy-pi.sh dereferences it)
put usr/local/bin/adguard-watch-origin 755
put etc/systemd/system/adguard-watch-origin.service 644 && systemctl daemon-reload
put etc/systemd/system/adguard-watch-origin.timer 644 && systemctl daemon-reload
systemctl is-enabled adguard-watch-origin.timer >/dev/null 2>&1 || { systemctl enable --now adguard-watch-origin.timer >/dev/null 2>&1; did "enabled adguard-watch-origin.timer"; }

say "Tailscale serve: AdGuard admin UI to the tailnet only (loopback :3000 behind tailscale)"
if [ "$(tailscale status --json 2>/dev/null | grep -o '"BackendState": *"[A-Za-z]*"' | head -1 | cut -d'"' -f4)" = "Running" ]; then
  tailscale serve status 2>/dev/null | grep -q "127.0.0.1:3000" || { tailscale serve --bg --https=443 http://127.0.0.1:3000 >/dev/null; did "tailscale serve 443 -> 127.0.0.1:3000"; }
  tailscale set --accept-dns=false >/dev/null 2>&1
fi

say "result"
echo "   hostname -f: $(hostname -f)"
echo "   sshd: $(sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|allowusers) ' | tr '\n' ' ')"
echo "   throttled: $(vcgencmd get_throttled)"
if [ "$(tailscale status --json 2>/dev/null | grep -o '"BackendState": *"[A-Za-z]*"' | head -1 | cut -d'"' -f4)" != "Running" ]; then
  echo "   MANUAL STEP: sudo tailscale up --hostname=piserver   (open the printed URL in a browser;"
  echo "                then disable key expiry for 'piserver' in the Tailscale admin console)"
fi
if [ "$CHANGED" -eq 0 ]; then echo "bootstrap: nothing to change (idempotent)"; else echo "bootstrap: $CHANGED change(s)"; fi
