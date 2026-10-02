#!/usr/bin/env python3
"""Task 23 attack suite, part 2: black box against the RUNNING relay containers (loopback only).

  port 18090  production instance (allow-list: the real hosts)
  port 18091  test instance, same image, allow-list: example.com (IANA's test host) and *.nip.io (public DNS names
              that resolve to private addresses, to prove the private-address rule against real DNS)

Secrets are read from the .env files and never printed. Run on the server:  python3 tests/blackbox_attacks.py
"""
import hashlib
import hmac
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

ROOT = "/srv/compose/relay"


def env(path, key):
    for l in open(path):
        if l.startswith(key + "="):
            return l.split("=", 1)[1].strip()


PROD = ("127.0.0.1", 18090, env(f"{ROOT}/.env", "RELAY_SECRET"))
TEST = ("127.0.0.1", 18091, env(f"{ROOT}/.env.test", "RELAY_SECRET"))
results = []


def sign(secret, ts, url):
    return hmac.new(secret.encode(), f"{ts}\n{url}".encode(), hashlib.sha256).hexdigest()


def call(inst, url, ts=None, sig=None, extra=None, raw_path=None, timeout=30):
    host, port, secret = inst
    ts = int(time.time()) if ts is None else ts
    if sig is None:
        sig = sign(secret, ts, url)
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    h = {"X-Relay-Ts": str(ts), "X-Relay-Caller": "attack-suite"}
    if sig != "":
        h["X-Relay-Sig"] = sig
    h.update(extra or {})
    try:
        c.request("GET", raw_path or ("/fetch?url=" + urllib.parse.quote(url, safe="")), headers=h)
        r = c.getresponse()
        body = r.read()
        return r.status, r.getheader("X-Relay-Reason") or "", r.getheader("X-Relay-Origin") or "", len(body), r
    except (OSError, http.client.HTTPException) as e:
        return 0, type(e).__name__, "", 0, None
    finally:
        c.close()


def check(name, got, want_status, want_reason=None):
    status, reason = got[0], got[1]
    ok = status == want_status and (want_reason is None or reason == want_reason)
    results.append((ok, name, f"-> {status} {reason}".strip()))


# ---------------- authentication -------------------------------------------------------------------------------
U = "https://www.sreality.cz/"
now = int(time.time())
check("missing signature", call(PROD, U, sig=""), 401)
check("garbage signature", call(PROD, U, sig="deadbeef" * 8), 401)
check("signature of a different url", call(PROD, U, sig=sign(PROD[2], now, "https://www.sreality.cz/other")), 401)
check("expired timestamp (-120 s)", call(PROD, U, ts=now - 120), 401)
check("future timestamp (+120 s)", call(PROD, U, ts=now + 120), 401)
check("non-numeric timestamp", call(PROD, U, ts="abc", sig="aa" * 32), 401)
check("401 reveals no reason header", call(PROD, U, sig="00" * 32), 401, "")
t_r = int(time.time())
sig_r = sign(TEST[2], t_r, "https://example.com/")
first = call(TEST, "https://example.com/", ts=t_r, sig=sig_r)
second = call(TEST, "https://example.com/", ts=t_r, sig=sig_r)
results.append((first[0] == 200 and second[0] == 401, "replayed signature (same ts+sig twice)", f"first {first[0]}, second {second[0]}"))

# ---------------- URL rules (correctly signed, so only the URL rules can refuse) -------------------------------
cases = [
    ("look-alike host sreality.cz.evil.example", "https://www.sreality.cz.evil.example/", 403, "host_not_allowlisted"),
    ("look-alike path ?www.sreality.cz on evil host", "https://evil.example/?www.sreality.cz", 403, "host_not_allowlisted"),
    ("userinfo: allow-listed name as the user", "https://www.sreality.cz@evil.example/", 403, "userinfo_in_url"),
    ("userinfo: allow-listed host after @", "https://evil.example@www.sreality.cz/", 403, "userinfo_in_url"),
    ("backslash trick", "https://evil.example\\@www.sreality.cz/", 400, "url_bad_chars"),
    ("encoded slash trick", "https://www.sreality.cz%2f@evil.example/", 403, None),
    ("http scheme", "http://www.sreality.cz/", 403, "scheme_not_https"),
    ("file scheme", "file:///etc/passwd", 403, "scheme_not_https"),
    ("ftp scheme", "ftp://www.sreality.cz/", 403, "scheme_not_https"),
    ("scheme-relative", "//www.sreality.cz/", 403, "scheme_not_https"),
    ("non-standard port 8443", "https://www.sreality.cz:8443/", 403, "port_not_allowed"),
    ("IDN homograph (Cyrillic a)", "https://www.sre\u0430lity.cz/", 403, "host_not_allowlisted"),
    ("whitespace in url", "https://www.sreality.cz/ x", 400, "url_bad_chars"),
    ("empty url", "", 403, "scheme_not_https"),
    ("127.0.0.1", "https://127.0.0.1/", 403, "ip_literal_host"),
    ("192.168.31.1 (router)", "https://192.168.31.1/", 403, "ip_literal_host"),
    ("192.168.31.2:3000 (AdGuard admin)", "https://192.168.31.2:3000/", 403, None),
    ("100.100.100.100 (tailnet DNS)", "https://100.100.100.100/", 403, "ip_literal_host"),
    ("[::1]", "https://[::1]/", 403, "ip_literal_host"),
    ("decimal-encoded 2130706433", "https://2130706433/", 403, "host_not_allowlisted"),
    ("hex-encoded 0x7f000001", "https://0x7f000001/", 403, "host_not_allowlisted"),
    ("localhost", "https://localhost/", 403, "host_not_allowlisted"),
    ("169.254.169.254 metadata", "https://169.254.169.254/latest/", 403, "ip_literal_host"),
    ("url over 2048 characters", "https://www.sreality.cz/" + "a" * 2100, 400, "url_too_long"),
]
for name, url, st, why in cases:
    check(name, call(PROD, url), st, why)
for name, url in (("uppercase host is normalised, not refused", "https://WWW.SREALITY.CZ/?noredirect=1"),
                  ("trailing dot is normalised, not refused", "https://www.sreality.cz./?noredirect=1")):
    got = call(PROD, url)
    results.append((got[0] in (200, 302) and got[2] == "target", name, f"-> {got[0]} origin={got[2]}"))
c = http.client.HTTPConnection("127.0.0.1", 18090, timeout=10)
c.request("POST", "/fetch", body=b"x")
r = c.getresponse()
results.append((r.status == 405, "POST is refused", f"-> {r.status}"))
c.close()
c = http.client.HTTPConnection("127.0.0.1", 18090, timeout=10)
c.request("GET", "/etc/passwd")
r = c.getresponse()
results.append((r.status == 404, "unknown path", f"-> {r.status}"))
c.close()

# ---------------- private addresses behind real DNS names (test instance, *.nip.io) ----------------------------
for name, url in (("name -> 127.0.0.1 (127.0.0.1.nip.io)", "https://127.0.0.1.nip.io/"),
                  ("name -> 192.168.31.1 (192.168.31.1.nip.io)", "https://192.168.31.1.nip.io/"),
                  ("name -> 192.168.31.2 (host)", "https://192.168.31.2.nip.io/"),
                  ("name -> 100.100.100.100 (tailnet DNS)", "https://100.100.100.100.nip.io/"),
                  ("name -> 169.254.169.254", "https://169.254.169.254.nip.io/")):
    check(name, call(TEST, url), 403, "private_address")

# ---------------- happy path, rate limit, daily cap (test instance) ---------------------------------------------
good = call(TEST, "https://example.com/")
results.append((good[0] == 200 and good[2] == "target", "allow-listed page is fetched (example.com via the test instance)", f"-> {good[0]} {good[3]} bytes"))
codes = []
t0 = time.time()
import concurrent.futures as cf  # noqa: E402
with cf.ThreadPoolExecutor(20) as ex:
    futs = [ex.submit(call, TEST, "https://example.com/?i=%d" % i) for i in range(40)]
    for f in futs:
        codes.append((f.result()[0], f.result()[1]))
n429 = sum(1 for c in codes if c[0] == 429)
results.append((n429 + sum(1 for c in codes if c[0] == 503) >= 20 and all(c[0] in (200, 429, 503) for c in codes), "request flood (40 parallel, 1 req/s per host): the excess is refused with 429/503, nothing crashed",
                f"200={sum(1 for c in codes if c[0]==200)} 429={n429} 503={sum(1 for c in codes if c[0]==503)} in {time.time()-t0:.0f}s"))

# ---------------- kill switch and daily cap (forced) ---------------------------------------------------------------
def ctl(container, cmd):
    return subprocess.run(["docker", "exec", container, "python", "/app/relayctl.py", cmd], capture_output=True, text=True).stdout.strip()


ctl("relay", "off")
killed = call(PROD, "https://www.sreality.cz/?noredirect=1")
ctl("relay", "on")
back = call(TEST, "https://example.com/?after-on=1")
results.append((killed[0] == 503 and killed[1] == "killed", "kill switch (relay-off): 503 killed, nothing fetched", f"-> {killed[0]} {killed[1]}"))
ctl("relay-test", "off")
k2 = call(TEST, "https://example.com/")
ctl("relay-test", "on")
k3 = call(TEST, "https://example.com/?x=2")
results.append((k2[0] == 503 and k3[0] in (200, 429), "kill switch off then on again restores service", f"off -> {k2[0]}, on -> {k3[0]}"))
seen503 = None
for i in range(80):
    g = call(TEST, "https://example.com/?cap=%d" % i)
    if g[0] == 503 and g[1] == "daily_cap":
        seen503 = i
        break
results.append((seen503 is not None, "daily cap reached: 503 daily_cap", f"after {seen503} more requests"))

# ---------------- hostile inbound connections -------------------------------------------------------------------
s = socket.create_connection(("127.0.0.1", 18090), timeout=25)
s.sendall(b"GET /fetch?url=x HTTP/1.1\r\nHost: a\r\n")
t0 = time.time()
closed = False
try:
    for _ in range(3):
        time.sleep(1.0)
        s.sendall(b"X-A: b\r\n")                                  # slow-loris: never finishes the headers
    s.settimeout(20)
    data = s.recv(100)
    closed = True
except OSError:
    closed = True
results.append((closed and time.time() - t0 < 20, "slow-loris (headers never finished): connection dropped", f"after {time.time()-t0:.0f}s"))
s.close()
c = http.client.HTTPConnection("127.0.0.1", 18090, timeout=10)
c.request("GET", "/fetch?url=x", headers={"X-Big": "B" * 70000})
r = c.getresponse()
results.append((r.status in (400, 431), "huge request header (70 KB)", f"-> {r.status}"))
c.close()
c = http.client.HTTPConnection("127.0.0.1", 18090, timeout=10)
c.request("GET", "/fetch?url=" + "a" * 20000)
r = c.getresponse()
results.append((r.status in (400, 414, 431), "huge request line (20 KB): refused before any work", f"-> {r.status}"))
c.close()
# repeated bad signatures from one source: 429 after the per-minute limit
codes = [call(PROD, U, sig="00" * 32)[0] for _ in range(14)]
results.append((401 in codes and 429 in codes, "repeated bad signatures are rate-limited (401 ... then 429)", f"{codes.count(401)}x401 {codes.count(429)}x429"))

# ---------------- network layer, from INSIDE the container ------------------------------------------------------
probe = ("import socket,sys\n"
         "ip,port=sys.argv[1],int(sys.argv[2])\n"
         "s=socket.socket(); s.settimeout(3)\n"
         "try:\n s.connect((ip,port)); print('CONNECTED')\nexcept Exception as e:\n print('blocked:',type(e).__name__)\n")
for label, ip, port in (("router 192.168.31.1:80", "192.168.31.1", 80), ("router DNS 192.168.31.1:53", "192.168.31.1", 53),
                        ("server LAN address 192.168.31.2:22", "192.168.31.2", 22), ("AdGuard 192.168.31.2:3000 via LAN ip", "192.168.31.2", 3000),
                        ("Pi 192.168.31.5:22", "192.168.31.5", 22), ("tailnet DNS 100.100.100.100:53", "100.100.100.100", 53),
                        ("tailnet peer 100.83.121.55:22", "100.83.121.55", 22), ("docker host gateway 172.30.0.1:22", "172.30.0.1", 22),
                        ("other docker stack 172.24.0.2:8080", "172.24.0.2", 8080), ("metadata 169.254.169.254:80", "169.254.169.254", 80)):
    out = subprocess.run(["docker", "exec", "relay", "python", "-c", probe, ip, str(port)], capture_output=True, text=True).stdout.strip()
    results.append((out.startswith("blocked"), f"inside the container -> {label}", out))
out = subprocess.run(["docker", "exec", "relay", "python", "-c", probe, "1.1.1.1", "443"], capture_output=True, text=True).stdout.strip()
results.append((out == "CONNECTED", "control: inside the container -> 1.1.1.1:443 (public) still works", out))

fails = [r for r in results if r and not r[0]]
w = max(len(r[1]) for r in results if r)
for r in results:
    if r:
        print(("PASS " if r[0] else "FAIL ") + r[1].ljust(w) + "  " + r[2])
print(f"\n{len([r for r in results if r]) - len(fails)}/{len([r for r in results if r])} passed")
sys.exit(1 if fails else 0)
