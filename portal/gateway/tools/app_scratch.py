#!/usr/bin/env python3
"""A SCRATCH gateway for testing the Android app in an emulator (Task 30): the real gateway and the real schedule service in-process, fake HA, synthetic people with
passwords (Webadmin = admin with an authenticator, Marta = household without one), behind a throw-away TLS proxy on 127.0.0.1:18443 (self-signed certificate for
localhost; the DEBUG build of the app trusts exactly that certificate). Nothing here touches the house.

  python tools/app_scratch.py CERT KEY OUTDIR        # writes OUTDIR/accounts.json (names, the test password, TOTP seeds) and keeps running
"""
import asyncio
import json
import os
import ssl
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from helpers import Env, PASSWORD  # noqa: E402
from gw import auth  # noqa: E402

A = {"person_id": "scratch", "phone_id": None, "ip": "scratch"}


def tls_proxy(cert, key, target_port, listen_port=18443):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def pipe(r, w):
        try:
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
        except Exception:                                       # noqa: BLE001
            pass
        finally:
            try:
                w.close()
            except Exception:                                   # noqa: BLE001
                pass

    async def handle(r, w):
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", target_port)
        except OSError:
            w.close()
            return
        await asyncio.gather(pipe(r, uw), pipe(ur, w))

    async def main():
        srv = await asyncio.start_server(handle, "127.0.0.1", listen_port, ssl=ctx)
        async with srv:
            await srv.serve_forever()
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


def main(cert, key, outdir):
    os.makedirs(outdir, exist_ok=True)
    e = Env()
    seeds = {}
    for name, role, totp in (("Webadmin", "admin", True), ("Marta", "household", False), ("Kid", "household", False)):
        e.people.create(A, name, role, True if name != "Kid" else False, web_login=True)
        pid = name.lower()
        raw, _ = e.people.create_setup_link(A, pid)
        _p, seed = e.people.setup_begin(raw)
        e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed) if totp else "", "1.1.1.1")
        seeds[pid] = seed if totp else None
    with open(os.path.join(outdir, "accounts.json"), "w") as f:
        json.dump({"password": PASSWORD, "seeds": seeds, "port": e.port}, f)
    os.chmod(os.path.join(outdir, "accounts.json"), 0o600)
    tls_proxy(cert, key, e.port)
    print("scratch gateway on 127.0.0.1:%d, TLS proxy on 127.0.0.1:18443; accounts in %s" % (e.port, outdir), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
