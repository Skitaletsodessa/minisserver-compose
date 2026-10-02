#!/bin/bash
# Run from the desktop (WSL, where `ssh rpis` and `ssh minis` both work). Copies the tracked /srv/compose/pi
# tree from the server to the Pi, applies the base (bootstrap-pi.sh), then brings up the AdGuard replica.
# Never overwrites the Pi's live conf/AdGuardHome.yaml, work/ or .env (they are not in git on purpose:
# the live config is regenerated from the origin by adguard-sync). Dead SD card -> reflash, run this,
# wait one sync.
set -euo pipefail
ssh minis 'tar -C /srv/compose/pi -cf - --exclude=adguard/conf/AdGuardHome.yaml --exclude=adguard/work --exclude=adguard/.env .' \
  | ssh rpis 'rm -rf ~/pi-deploy.new && mkdir ~/pi-deploy.new && tar -C ~/pi-deploy.new -xf - && rm -rf ~/pi-deploy && mv ~/pi-deploy.new ~/pi-deploy && chmod +x ~/pi-deploy/*.sh ~/pi-deploy/adguard/*.sh'
ssh rpis 'cd ~/pi-deploy && sudo ./bootstrap-pi.sh | grep -E "changed:|bootstrap:|MANUAL"'
# Telegram credentials for the Pi-side watchdog: only the two keys, straight from the server to the Pi (never printed)
ssh minis 'grep -E "^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=" /srv/compose/scrutiny/.env' | ssh rpis 'sudo install -d -m 755 /etc/piserver && sudo sh -c "umask 177 && cat > /etc/piserver/telegram.env"'
ssh rpis 'set -e; mkdir -p ~/adguard/conf ~/adguard/work
  cd ~/pi-deploy/adguard && install -m 644 compose.yaml .env.example setup-adguard.sh ~/adguard/
  install -m 644 conf/AdGuardHome.yaml.example ~/adguard/conf/
  chmod +x ~/adguard/setup-adguard.sh && ~/adguard/setup-adguard.sh
  cd ~/adguard && docker compose up -d'
