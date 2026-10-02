#!/bin/bash
# cleanup-pi.sh - ONE-TIME in-place cleanup of the old Pi (Task 22 B2). Run as root on the Pi.
# Ivan's decision 2026-10-02: nothing on the Pi has value; Home Assistant, the camera stack,
# the legacy automation key and every unknown secret are destroyed. Not part of bootstrap-pi.sh
# (a reflashed card never needs it). Kept in git as the record of what was destroyed.
set -u
say() { echo "== $*"; }

say "keep sudo for skit independent of cloud-init (before cloud-init is purged)"
echo 'skit ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/skit-nopasswd
chmod 440 /etc/sudoers.d/skit-nopasswd
visudo -cf /etc/sudoers.d/skit-nopasswd || exit 1

say "Home Assistant container, images, volumes, networks"
if command -v docker >/dev/null; then
  docker ps -aq | xargs -r docker rm -f
  docker system prune -af --volumes
fi
rm -rf /home/pi

say "camera stack, Raspberry Pi Connect, desktop/radio leftovers, cloud-init"
loginctl disable-linger skit 2>/dev/null || true
systemctl disable --now ustreamer.service 2>/dev/null || true
rm -f /etc/systemd/system/ustreamer.service
DEBIAN_FRONTEND=noninteractive apt-get purge -y ustreamer rpi-connect-lite rpi-connect avahi-daemon bluez \
  udisks2 cloud-init 2>&1 | tail -3
DEBIAN_FRONTEND=noninteractive apt-get autoremove -y --purge 2>&1 | tail -2
apt-get clean
rm -rf /etc/cloud /var/lib/cloud

say "Wi-Fi and bridge profiles (secrets), keep the wired one"
nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"||$2=="bridge"{print $1}' |
  while read -r n; do nmcli connection delete "$n"; done
rm -f /etc/wpa_supplicant/wpa_supplicant.conf

say "keys: only the server's key stays; root has none"
keep=$(grep ' skit@minisserver$' /home/skit/.ssh/authorized_keys || true)
[ -n "$keep" ] || { echo "FATAL: the server key is not in authorized_keys"; exit 1; }
printf '%s\n' "$keep" > /home/skit/.ssh/authorized_keys
chown skit:skit /home/skit/.ssh/authorized_keys; chmod 600 /home/skit/.ssh/authorized_keys
rm -f /root/.ssh/authorized_keys

say "histories, hand-installed scripts, user-service configs"
rm -f /home/skit/.bash_history /root/.bash_history /home/skit/.wget-hsts /home/skit/get-docker.sh \
      /home/skit/record_video.sh /home/skit/.sudo_as_admin_successful
rm -rf /home/skit/.config /home/skit/.cache /root/.cache

say "sweep for secret-shaped files outside .ssh (home, root, opt, srv, tmp): count before/after"
sweep() { find /home /root /opt /srv /tmp /var/tmp -xdev \( -path '*/.ssh' -prune \) -o -type f \
  \( -name '.env' -o -name '*.pem' -o -name 'id_*' -o -name '*.key' -o -name '.netrc' -o -name '.git-credentials' \
     -o -name 'secrets.yaml' -o -name '*.token' \) -print; }
echo "before: $(sweep | wc -l)"; sweep | xargs -r rm -f; echo "after: $(sweep | wc -l)"

say "rotate SSH host keys"
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A
systemctl restart ssh
say "done"
