# AdGuard Home replica on piserver (Task 21)

The server's AGH (`/srv/compose/adguard/`) is the **origin**; this is a **replica**. Nothing is configured on
the replica by hand: `adguard-sync` (`/srv/compose/adguard-sync/`, runs on the server) pushes the config via
AGH's API, live. Its live `conf/AdGuardHome.yaml`, `work/` (filter cache!) and `.env` are on the Pi only and
are not in git or in backups: they are rebuilt (`deploy-pi.sh` + one sync). Never put the filter cache on tmpfs:
a reboot would bring the replica up answering without lists (fail-open).

Intentional differences from the origin (the complete list; everything else must be equal and is checked
every 5 minutes by `_system/adguard-parity`): see the header of `conf/AdGuardHome.yaml.example`.
