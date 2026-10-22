"""Entry point: python -m sched.server. Binds 127.0.0.1 only; refuses to start on anything else (ADR 1: host network, explicit bind)."""
import os
import sys
import uvicorn
from . import api, store

HOST = os.environ.get("PORTAL_BIND", "127.0.0.1")
PORT = int(os.environ.get("PORTAL_PORT", "16444"))
DATA = os.environ.get("PORTAL_DATA", "/data")


def main():
    if HOST not in ("127.0.0.1", "::1"):
        sys.exit("refusing to bind %r: the schedule service listens on loopback only (Caddy publishes it on the tailnet)" % HOST)
    st = store.Store(DATA)
    try:                                           # Task 31: Maps moved to the always-open set; idempotent, audited, a no-op once done
        done = st.migrate_maps_always_open()
        if done:
            print("maps-always-open migration applied: %s" % done, flush=True)
    except Exception as ex:                        # the service still starts; /v1/health shows the unpublished snapshot
        print("maps-always-open migration FAILED: %s: %s" % (type(ex).__name__, ex), file=sys.stderr, flush=True)
    seed = os.environ.get("PORTAL_SEED", "/app/seed/initial.json")
    if not st.entities() and os.path.exists(seed):
        st.seed(seed, {"token": "-", "name": "seed", "role": "admin"})
    else:
        st.publish()
    app = api.build(st)
    uvicorn.run(app, host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
