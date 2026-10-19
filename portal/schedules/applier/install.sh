#!/bin/bash
# Installs the applier's systemd units as symlinks (house pattern) and enables them. Idempotent.
set -euo pipefail
D=/srv/compose/portal/schedules/applier/systemd
for u in portal-apply.service portal-apply.path portal-apply.timer; do
    sudo ln -sfn "$D/$u" "/etc/systemd/system/$u"
done
sudo systemctl daemon-reload
sudo systemctl enable --now portal-apply.path portal-apply.timer     # enable alone does not start a timer (Task 24)
systemctl list-timers portal-apply.timer --no-pager | head -3
