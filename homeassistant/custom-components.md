# Custom components in /srv/apps/homeassistant/custom_components (third-party code, full HA privileges - rule 7)

## xiaomi_miot (al-one/hass-xiaomi-miot) - added 2026-10-15

- Why: `zhimi.airp.cpa4` (Mi Purifier 4 Compact, 192.168.31.41) is not supported by core `xiaomi_miio` (HA 2026.10.1).
- Release: **v1.1.5** (2026-09-07), commit `d29e83e703c1c2bc4f1611c1119c8e516a2c961d`,
  source archive sha256 `b30efc59a1bdc766c0018667dd013930d89112a43b4f76287280182e66021447`
  (https://github.com/al-one/hass-xiaomi-miot/archive/refs/tags/v1.1.5.tar.gz).
  Manual copy of `custom_components/xiaomi_miot`, no HACS, no auto-update.
- Local patch: `patches/xiaomi_miot-v1.1.5-no-telemetry.patch` - `async_analytics_track_event()` returns immediately.
  Upstream otherwise POSTs model, firmware, property values, Wi-Fi RSSI, timezone and version to hacc.miot-spec.com
  when a local device is added; there is no opt-out. Re-apply the patch after ANY upgrade of this component.
- Reviewed before install: no eval/exec/subprocess, no file writes outside the config dir; network: Xiaomi cloud hosts,
  miot-spec.org / spec.miot-spec.com (device specs). Dependencies (python-miio, micloud, construct) are already
  satisfied by HA's pins.
- Configure with host + token only (no Xiaomi account, no cloud mode). The Lite stays on core `xiaomi_miio`.
- Upgrade = new tagged release, repeat the review (release notes, manifest, grep for new hosts), re-apply the patch,
  record tag + commit here.
- Rollback: `rm -rf /srv/apps/homeassistant/custom_components/xiaomi_miot`, delete the integration entry in the UI,
  `docker compose restart homeassistant`.
- Backup: covered by the `/srv/apps/homeassistant` line in `backup-set.conf` (custom_components is not excluded).
