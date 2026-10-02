#!/usr/bin/env python3
"""Task 21: ask adguard-sync to push NOW. POSTs twice in a row: adguard-sync silently drops a request that
arrives while a sync pass is already running, and a pass that started before the origin change does not
contain it - the second POST (made after the first returned) is the one that is guaranteed to start after
the change. Detached from the caller by childpolicy.trigger_sync(); a failure is harmless (1-minute cron)."""
import base64
import urllib.request

env = {}
for line in open("/srv/compose/adguard-sync/.env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
auth = base64.b64encode(f"{env['SYNC_API_USER']}:{env['SYNC_API_PASSWORD']}".encode()).decode()
for _ in range(2):
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18086/api/v1/sync", data=b"", method="POST",
                               headers={"Authorization": "Basic " + auth}), timeout=90).read()
    except Exception:  # noqa: BLE001
        break
