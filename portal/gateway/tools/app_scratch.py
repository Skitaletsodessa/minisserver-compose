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




def control_loop(e, outdir):
    """Task 30c: the emulator tests drive the scratch gateway by dropping a JSON file `cmd-*.json` into OUTDIR; the answer is written to `ans-<same name>`.
      {"op": "pin", "fp": "<sha256 of the signing certificate>"}                       trust that certificate (the debug key in the emulator tests)
      {"op": "add", "apk": "/path", "notes": "..."}                                    gwctl release add
      {"op": "force_add", "apk": "/path", "notes": "..."}                              store as pending WITHOUT the certificate check (an APK signed by another key)
      {"op": "publish", "code": 5, "min": 4}                                           what the «Опубликовать» button does (through the real API as the synthetic admin on the web)
      {"op": "stop"} / {"op": "resume"}                                                the kill switch
      {"op": "rewrite", "field": "apk_sha256", "value": "..", "resign": true}          change one field of the CURRENT manifest; re-sign with the test key or keep the old signature
      {"op": "corrupt_apk", "code": 5}                                                  append a byte to the stored APK (the server then refuses to serve it)
      {"op": "state"}                                                                  the releases list and the channel health
    """
    import base64
    import glob
    import hashlib
    import re
    import shutil
    from gw import apkinfo, ed25519, releases as relmod
    R = e.o["releases"]
    actor = {"person_id": "webadmin", "phone_id": None, "ip": "scratch"}
    seed = bytes(range(32))
    while True:
        for path in sorted(glob.glob(os.path.join(outdir, "cmd-*.json"))):
            name = os.path.basename(path)
            try:
                c = json.load(open(path))
                os.remove(path)
                op = c["op"]
                ans = {"ok": True}
                if op == "pin":
                    R.pin(c["fp"], actor)
                elif op == "add":
                    ans["info"] = {k: v for k, v in R.add(c["apk"], c.get("notes") or "Что нового: проверка", actor).items() if k != "certs"}
                elif op == "force_add":
                    info = apkinfo.inspect(c["apk"])
                    dest = R.apk_path(info["version_code"])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copyfile(c["apk"], dest)
                    e.db.x("INSERT INTO releases(version_code,version_name,size,sha256,cert_sha256,notes,state,added_by,added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (info["version_code"], info["version_name"], info["size"], info["sha256"], info["certs"][0], c.get("notes") or "forced", "pending", "scratch", time.time()))
                elif op == "publish":
                    R.publish(c["code"], actor, c.get("min"))
                elif op == "stop":
                    R.stop(actor)
                elif op == "resume":
                    R.resume(actor)
                elif op == "rewrite":
                    m = json.loads(e.db.meta_get("release_manifest"))
                    p = json.loads(m["payload"])
                    p[c["field"]] = c["value"]
                    payload = relmod.canonical(p)
                    m["payload"] = payload
                    if c.get("resign", True):
                        m["signature"] = base64.b64encode(ed25519.sign(seed, payload.encode("utf-8"))).decode()
                    e.db.meta_set("release_manifest", json.dumps(m))
                elif op == "corrupt_apk":
                    with open(R.apk_path(c["code"]), "ab") as f:
                        f.write(b"x")
                elif op == "state":
                    ans["releases"] = R.list()
                    ans["health"] = R.health()
                    ans["stopped"] = R.stopped()
                else:
                    ans = {"ok": False, "error": "unknown op"}
            except Exception as ex:                                  # noqa: BLE001
                ans = {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}
            json.dump(ans, open(os.path.join(outdir, "ans-" + name), "w"))
        time.sleep(0.3)


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
    threading.Thread(target=control_loop, args=(e, outdir), daemon=True).start()                  # Task 30c: release control for the emulator tests
    tls_proxy(cert, key, e.port)
    print("scratch gateway on 127.0.0.1:%d, TLS proxy on 127.0.0.1:18443; accounts in %s" % (e.port, outdir), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
