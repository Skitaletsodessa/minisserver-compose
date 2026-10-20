"""Entry point: python -m gw.server. Binds loopback only and refuses anything else (Caddy publishes the tailnet-only site)."""
import asyncio
import contextlib
import sys
import time

from . import api, config, core as coremod, db as dbmod, ha as hamod, hub as hubmod, people as peoplemod, sched as schedmod, web


def make(data_dir=None, ha_url=None, ha_token_file=None, sched_url=None, sched_admin=None, sched_household=None, clock=time.time, start_background=True):
    db = dbmod.DB(data_dir or config.DATA, clock=clock)
    hub = hubmod.Hub(clock=clock)
    ha = hamod.HA(ha_url or config.HA_URL, ha_token_file or config.HA_TOKEN_FILE, hub=hub, clock=clock)
    sched = schedmod.SchedClient(sched_url or config.SCHED_URL, sched_admin or config.SCHED_TOKEN_ADMIN_FILE, sched_household or config.SCHED_TOKEN_HOUSEHOLD_FILE, clock=clock)
    people = peoplemod.People(db, hub, clock=clock)
    core = coremod.Core(db, people, ha, sched, hub, clock=clock)
    core.seed()
    app_obj = api.App(db, people, core, hub, sched, ha, clock=clock)

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
            tasks.append(asyncio.create_task(core.probe_loop()))
            tasks.append(asyncio.create_task(_rescope(core)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await ha.stop()

    app = api.build(app_obj, extra_routes=web.routes(app_obj), lifespan=lifespan)
    app.state.objs = {"db": db, "people": people, "core": core, "hub": hub, "ha": ha, "sched": sched, "app": app_obj}
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
