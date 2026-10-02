#!/usr/bin/env python3
"""Task 23 Phase C: external verification through https://relay.ivandeliver.email (Cloudflare Access service token + HMAC).
Secrets are read from files and never printed."""
import hashlib, hmac, subprocess, sys, time, urllib.parse, urllib.request, urllib.error

def env(path, key):
    for l in open(path):
        if l.startswith(key + "="):
            return l.split("=", 1)[1].strip()
SECRET = env("/srv/compose/relay/.env", "RELAY_SECRET")
CF_ID, CF_SECRET = env("/srv/compose/relay/.access-test", "CF_ID"), env("/srv/compose/relay/.access-test", "CF_SECRET")
BASE = "https://relay.ivandeliver.email"

def call(url, access=True, sign=True, access_id=None, access_secret=None):
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}\n{url}".encode(), hashlib.sha256).hexdigest() if sign else "00" * 32
    h = {"X-Relay-Ts": str(ts), "X-Relay-Sig": sig, "X-Relay-Caller": "phase-c-check", "User-Agent": "phase-c-check"}
    if access:
        h["CF-Access-Client-Id"] = access_id or CF_ID
        h["CF-Access-Client-Secret"] = access_secret or CF_SECRET
    req = urllib.request.Request(BASE + "/fetch?url=" + urllib.parse.quote(url, safe=""), headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=40)
        return r.status, r.headers.get("X-Relay-Origin"), r.headers.get("X-Relay-Reason"), r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Relay-Origin"), e.headers.get("X-Relay-Reason"), e.headers.get("Content-Type"), e.read()

def show(label, res):
    st, org, why, ct, body = res
    print(f"{label:<72} -> {st} origin={org} reason={why} type={ct} {len(body)}B")
    return res

print("== (a) edge protection")
show("no Access token, no signature", call("https://www.sreality.cz/?noredirect=1", access=False, sign=False))
show("no Access token, valid signature", call("https://www.sreality.cz/?noredirect=1", access=False))
show("WRONG Access secret, valid signature", call("https://www.sreality.cz/?noredirect=1", access_secret="0" * 64))
print("== second lock: valid Access token, bad signature")
show("valid Access token, bad signature", call("https://www.sreality.cz/?noredirect=1", sign=False))
print("== (b) signed request for an allow-listed page")
b = show("Sreality ?noredirect=1", call("https://www.sreality.cz/?noredirect=1"))
print("      real page:", b[0] == 200 and b[1] == "target" and b"__NEXT_DATA__" in b[4])
time.sleep(3)
show("Bazos list reality.bazos.cz/prodam/dum/", call("https://reality.bazos.cz/prodam/dum/"))
print("== (c) signed request for anything else")
show("https://www.seznam.cz/ (not allow-listed)", call("https://www.seznam.cz/"))
show("https://192.168.31.1/ (the router)", call("https://192.168.31.1/"))
show("http://www.sreality.cz/ (not https)", call("http://www.sreality.cz/"))
print("== (d) kill switch end to end")
subprocess.run(["docker", "exec", "relay", "python", "/app/relayctl.py", "off"], capture_output=True)
t0 = time.time(); k = show("relay-off, then a valid signed request", call("https://www.sreality.cz/?noredirect=1"))
subprocess.run(["docker", "exec", "relay", "python", "/app/relayctl.py", "on"], capture_output=True)
time.sleep(2)
o = show("relay-on, same request", call("https://www.sreality.cz/?noredirect=1"))
print("kill switch OK:", k[0] == 503 and k[2] == "killed" and o[0] == 200)
