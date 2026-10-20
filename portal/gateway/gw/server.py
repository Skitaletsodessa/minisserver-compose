"""Entry point: python -m gw.server. Binds loopback only and refuses anything else (Caddy publishes the tailnet-only site)."""
import asyncio
import contextlib
import sys
import time

from . import agh as aghmod, api, config, core as coremod, db as dbmod, filterlists, ha as hamod, hub as hubmod, parental as parentalmod, people as peoplemod, sched as schedmod, web


def make(data_dir=None, ha_url=None, ha_token_file=None, sched_url=None, sched_admin=None, sched_household=None, clock=time.time, start_background=True,
         agh_url=None, agh_password_file=None, parity_state=None, oui_file=None, filter_opts=None):
    db = dbmod.DB(data_dir or config.DATA, clock=clock)
    hub = hubmod.Hub(clock=clock)
    ha = hamod.HA(ha_url or config.HA_URL, ha_token_file or config.HA_TOKEN_FILE, hub=hub, clock=clock)
    sched = schedmod.SchedClient(sched_url or config.SCHED_URL, sched_admin or config.SCHED_TOKEN_ADMIN_FILE, sched_household or config.SCHED_TOKEN_HOUSEHOLD_FILE, clock=clock)
    people = peoplemod.People(db, hub, clock=clock)
    core = coremod.Core(db, people, ha, sched, hub, clock=clock)
    core.seed()
    agh = aghmod.Agh(agh_url or config.AGH_URL, agh_password_file or config.AGH_PASSWORD_FILE)
    opts = {"allow_file": config.FILTER_FETCH_ALLOW_FILE, "parity_state": parity_state or config.PARITY_STATE_FILE, "origin_dns": config.ORIGIN_DNS, "replica_dns": config.REPLICA_DNS}
    opts.update(filter_opts or {})
    parental = parentalmod.Parental(sched, agh, None, db, clock=clock, parity_state=opts["parity_state"], oui_file=oui_file or config.OUI_FILE)
    filters = filterlists.FilterLists(agh, db, clock, children=parental.child_ips, **opts)
    parental.filters = filters
    app_obj = api.App(db, people, core, hub, sched, ha, clock=clock, parental=parental, filters=filters)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        tasks = []
        core.loop = asyncio.get_running_loop()
        if start_background:
            try:
                await core.sync_scope()
            except Exception:                                   # noqa: BLE001 - the schedule service may be down at boot: retried below
                pass
            await ha.start()
            try:
                await filters.recover()                         # a filter-list change that was running when the gateway stopped is put back
            except Exception:                                   # noqa: BLE001 - AdGuard may be down at boot; the row stays 'running' and is retried at the next start
                pass
            tasks.append(asyncio.create_task(core.probe_loop()))
            tasks.append(asyncio.create_task(_rescope(core)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await ha.stop()

    app = api.build(app_obj, extra_routes=web.routes(app_obj), lifespan=lifespan)
    app.state.objs = {"db": db, "people": people, "core": core, "hub": hub, "ha": ha, "sched": sched, "app": app_obj, "agh": agh, "parental": parental, "filters": filters}
    return app


async def _rescope(core):
    """Re-reads the device list from the schedule service every minute (a new plug appears without a restart); retries until the first success."""
    while True:
        try:
            await core.sync_scope()
        except asyncio.CancelledError:
            raise
        except Exception:                                       # noqa: BLE001
            pass
        await asyncio.sleep(60)


def main():
    import uvicorn
    if config.BIND not in ("127.0.0.1", "::1"):
        sys.exit("refusing to bind %r: the gateway listens on loopback only (Caddy publishes the tailnet-only site)" % config.BIND)
    uvicorn.run(make(), host=config.BIND, port=config.PORT, proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
