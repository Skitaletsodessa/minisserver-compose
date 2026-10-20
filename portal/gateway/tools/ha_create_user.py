"""Creates the gateway's dedicated NON-admin Home Assistant user and a long-lived access token, using HA's own AuthManager (Task 28 Phase 0 §3).

  docker stop homeassistant          # HA keeps .storage/auth in memory and rewrites it: it must not run while this script edits the store
  docker run --rm --network none -v /srv/apps/homeassistant:/config -v OUTDIR:/out -v THIS_FILE:/ha_create_user.py:ro IMAGE python /ha_create_user.py /config /out/ha-token
  docker start homeassistant

- the user is in group `system-users` (NOT `system-admin`), `local_only` (it can only log in from the local network), and has NO credentials at all:
  there is no password to leak; the only way in is the token
- the token is written to the given file with mode 600 and is NEVER printed
- an existing user of that name is refused (run once), so a second run cannot silently create a second token holder
"""
import asyncio
import os
import sys
from datetime import timedelta

NAME = "portal-gateway"


async def main(config_dir, out_path):
    from homeassistant.auth import auth_manager_from_config
    from homeassistant.auth.const import GROUP_ID_USER
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    hass = HomeAssistant(config_dir)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    dr.async_setup(hass)
    await asyncio.gather(dr.async_load(hass), er.async_load(hass))
    hass.auth = await auth_manager_from_config(hass, [{"type": "homeassistant"}], [])
    if [u for u in await hass.auth.async_get_users() if u.name == NAME]:
        print("a user named %s exists already; nothing done" % NAME)
        return 1
    if not any(u.is_owner for u in await hass.auth.async_get_users()):
        print("this Home Assistant has no owner yet; the first user created would become the OWNER (full admin). Refusing.")
        return 3
    user = await hass.auth.async_create_user(NAME, group_ids=[GROUP_ID_USER], local_only=True)
    if user.is_owner or user.is_admin:                            # defence in depth: never hand out a token for an admin
        await hass.auth.async_remove_user(user)
        print("the new user came out as owner/admin; removed it again. Refusing.")
        return 4
    refresh = await hass.auth.async_create_refresh_token(user, client_name="portal-gateway", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                                                         access_token_expiration=timedelta(days=3650))
    token = hass.auth.async_create_access_token(refresh)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    print("user %s created (group %s, local_only); token written to %s (mode 600)" % (NAME, ",".join(g.id for g in user.groups), out_path))
    store = hass.auth._store                                      # the auth store saves on a delay: write it now, then check it is really on disk
    await store._store.async_save(store._data_to_save())
    path = os.path.join(config_dir, ".storage", "auth")
    if not os.path.exists(path) or NAME not in open(path, encoding="utf-8").read():
        print("the auth store was not written; the token would not work")
        os.unlink(out_path)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
