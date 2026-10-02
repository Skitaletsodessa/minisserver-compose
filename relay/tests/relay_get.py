#!/usr/bin/env python3
"""Signed GET through the relay (Task 23) - reference client and manual test tool.
   usage: relay_get.py URL [--port 18090] [--cookie 'a=b'] [--body]
The secret is read from /srv/compose/relay/.env and never printed."""
import base64, hashlib, hmac, http.client, json, sys, time, urllib.parse

args = sys.argv[1:]
url = args[0]
port = int(args[args.index("--port") + 1]) if "--port" in args else 18090
cookie = args[args.index("--cookie") + 1] if "--cookie" in args else None
secret = next(l.split("=", 1)[1].strip() for l in open("/srv/compose/relay/.env") if l.startswith("RELAY_SECRET="))
ts = int(time.time())
sig = hmac.new(secret.encode(), f"{ts}\n{url}".encode(), hashlib.sha256).hexdigest()
c = http.client.HTTPConnection("127.0.0.1", port, timeout=40)
h = {"X-Relay-Ts": str(ts), "X-Relay-Sig": sig, "X-Relay-Caller": "relay_get", "X-Fwd-Accept-Language": "cs-CZ,cs;q=0.9"}
if cookie:
    h["X-Fwd-Cookie"] = cookie
t0 = time.time()
c.request("GET", "/fetch?url=" + urllib.parse.quote(url, safe=""), headers=h)
r = c.getresponse()
body = r.read()
print(f"status {r.status}  origin={r.getheader('X-Relay-Origin')}  reason={r.getheader('X-Relay-Reason')}  type={r.getheader('Content-Type')}  {len(body)} bytes  {time.time()-t0:.2f}s")
ck = r.getheader("X-Relay-Cookies")
if ck:
    print("cookie names returned:", [p.split("=")[0] for p in json.loads(base64.b64decode(ck))])
if "--body" in args:
    sys.stdout.write(body[:300].decode("utf-8", "replace") + "\n")
print("looks like:", "JSON" if body[:1] in (b"{", b"[") else ("HTML, has __NEXT_DATA__" if b"__NEXT_DATA__" in body else "other"))
sys.stdout.flush()
if "--buildid" in args:
    import re
    m = re.search(rb'"buildId":"([^"]+)"', body)
    print("buildId:", m.group(1).decode() if m else None)
