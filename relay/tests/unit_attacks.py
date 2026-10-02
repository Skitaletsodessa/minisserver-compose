#!/usr/bin/env python3
"""Task 23 attack suite, part 1: the relay's own logic attacked IN PROCESS against a local TLS "victim" server.

Why in process: the relay must never connect to a private address, so a local victim cannot be reached through
the real code path - the test patches only the two things that would send the traffic elsewhere (name resolution
and the TCP connect) and leaves every check in relay.py untouched. Nothing here talks to a third-party site.

Run on the server:  python3 tests/unit_attacks.py     (needs the openssl CLI for a throw-away certificate)
"""
import gzip
import http.server
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp(prefix="relay-unit-")
os.environ.update({"RELAY_STATE_DIR": TMP, "RELAY_SECRET": "x" * 40, "RELAY_ALLOWLIST": os.path.join(TMP, "allow.txt"),
                   "RELAY_RATE_PER_SEC": "1000", "RELAY_BURST": "1000", "RELAY_BREAKER_THRESHOLD": "5"})
open(os.environ["RELAY_ALLOWLIST"], "w").write(
    "victim.test cookies\nplain.test\npriv-name.test\nmixed.test\nv6only.test\nrebind.test\n")
import relay  # noqa: E402

relay.TOTAL_TIMEOUT = 4.0

# ---- local TLS victim ---------------------------------------------------------------------------------------
cert, key = os.path.join(TMP, "c.pem"), os.path.join(TMP, "k.pem")
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
                "-subj", "/CN=victim.test", "-addext", "subjectAltName=DNS:victim.test,DNS:plain.test,DNS:priv-name.test,DNS:mixed.test,DNS:rebind.test"],
               check=True, capture_output=True)
SERVER_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
SERVER_CTX.load_cert_chain(cert, key)
CLIENT_CTX = ssl.create_default_context(cafile=cert)


class Victim(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, status, body=b"", ctype="text/html", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/ok":
            self.reply(200, b"<html>hello</html>")
        elif p == "/cookie":
            self.reply(200, b"<html>c</html>", extra=[("Set-Cookie", "consent=yes; Path=/; Secure"), ("Set-Cookie", "ab=7; Path=/")])
        elif p == "/big":
            self.reply(200, b"A" * (6 * 1024 * 1024))
        elif p == "/bomb":
            blob = gzip.compress(b"\0" * (80 * 1024 * 1024), 9)
            self.reply(200, blob, extra=[("Content-Encoding", "gzip")])
        elif p == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            for _ in range(30):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(1)
        elif p == "/loop":
            self.reply(302, extra=[("Location", "https://victim.test/loop")])
        elif p == "/to-private-ip":
            self.reply(302, extra=[("Location", "https://192.168.31.1/")])
        elif p == "/to-loopback":
            self.reply(302, extra=[("Location", "https://127.0.0.1/")])
        elif p == "/to-evil":
            self.reply(302, extra=[("Location", "https://evil.example/")])
        elif p == "/to-userinfo":
            self.reply(302, extra=[("Location", "https://victim.test@evil.example/")])
        elif p == "/to-http":
            self.reply(302, extra=[("Location", "http://victim.test/ok")])
        elif p == "/to-private-name":
            self.reply(302, extra=[("Location", "https://priv-name.test/x")])
        elif p == "/to-ok":
            self.reply(302, extra=[("Location", "/ok")])
        elif p == "/403":
            self.reply(403, b"<html>nope</html>")
        elif p == "/json-html":
            self.reply(200, b"<html><title>Just a moment...</title></html>", ctype="application/json")
        elif p == "/image":
            self.reply(200, b"\x89PNG....", ctype="image/png")
        elif p == "/binary":
            self.reply(200, b"MZ....", ctype="application/octet-stream")
        elif p == "/hugeheader":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("X-Big", "B" * 300000)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.reply(404, b"<html>404</html>")


class QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


srv = QuietServer(("127.0.0.1", 0), Victim)
srv.socket = SERVER_CTX.wrap_socket(srv.socket, server_side=True)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---- the only two patches: where names resolve to and where TCP goes ---------------------------------------
PUBLIC = "93.184.216.34"
resolver = {"calls": [], "answers": {}}                       # name -> list of addresses, or callable


real_getaddrinfo = socket.getaddrinfo


def fake_getaddrinfo(host, port, family=0, type=0, *a, **k):
    if host == "127.0.0.1":                                   # the test harness's own connection to the victim
        return real_getaddrinfo(host, port, family, type, *a, **k)
    resolver["calls"].append(host)
    ans = resolver["answers"].get(host, [PUBLIC])
    if callable(ans):
        ans = ans(len([c for c in resolver["calls"] if c == host]))
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a_, port)) for a_ in ans]


connects = []
real_create_connection = socket.create_connection


def fake_create_connection(addr, timeout=None, *a, **k):
    connects.append(addr[0])
    return real_create_connection(("127.0.0.1", PORT), timeout=timeout)


socket.getaddrinfo = fake_getaddrinfo
socket.create_connection = fake_create_connection
ssl.create_default_context = lambda *a, **k: CLIENT_CTX


# ---- the cases ----------------------------------------------------------------------------------------------
results = []


def case(name, url, expect_reason=None, expect_status=None, check=None, headers=None):
    connects.clear()
    t0 = time.monotonic()
    try:
        res = relay.do_fetch(url, headers or {})
        got = ("ok", res["status"])
        ok = expect_reason is None and (expect_status is None or res["status"] == expect_status) and (check(res) if check else True)
        detail = f"status {res['status']}, {len(res['body'])} bytes"
    except relay.Refuse as r:
        got = (r.reason, r.status)
        ok = expect_reason is not None and r.reason.startswith(expect_reason)
        detail = f"refused {r.status} {r.reason}"
    results.append((ok, name, detail, round(time.monotonic() - t0, 1)))


V = "https://victim.test/"
case("normal page is fetched", V + "ok", expect_status=200)
case("cookies: pairs returned for a 'cookies' host, no attributes", V + "cookie", expect_status=200,
     check=lambda r: r["cookie_pairs"] == ["consent=yes", "ab=7"])
case("no cookies returned for a host without the flag", "https://plain.test/cookie", expect_status=200,
     check=lambda r: r["cookie_pairs"] == [])
case("oversize body (6 MB > 5 MB cap)", V + "big", expect_reason="body_too_large")
case("gzip bomb (80 MB of zeros in ~80 KB)", V + "bomb", expect_reason="body_too_large")
case("slow body (1 byte/s, 4 s deadline)", V + "slow", expect_reason="timeout_reading_body")
case("redirect loop", V + "loop", expect_reason="too_many_redirects")
case("redirect to private IP literal", V + "to-private-ip", expect_reason="redirect_refused:ip_literal_host")
case("redirect to loopback IP literal", V + "to-loopback", expect_reason="redirect_refused:ip_literal_host")
case("redirect to a non-allow-listed host", V + "to-evil", expect_reason="redirect_refused:host_not_allowlisted")
case("redirect with userinfo trick", V + "to-userinfo", expect_reason="redirect_refused:userinfo_in_url")
case("redirect downgrade to http", V + "to-http", expect_reason="redirect_refused:scheme_not_https")
case("legit relative redirect still works", V + "to-ok", expect_status=200)
case("content-type not allowed (image on a host without 'images')", V + "image", expect_reason="content_type_not_allowed")
case("content-type not allowed (octet-stream)", V + "binary", expect_reason="content_type_not_allowed")
case("300 KB response header (contained)", V + "hugeheader", expect_reason="upstream_error")

# DNS: allow-listed names whose answers are not public
resolver["answers"]["priv-name.test"] = ["192.168.31.1"]
case("allow-listed NAME resolving to 192.168.31.1", "https://priv-name.test/", expect_reason="private_address")
resolver["answers"]["priv-name.test"] = ["127.0.0.1"]
case("allow-listed NAME resolving to 127.0.0.1", "https://priv-name.test/", expect_reason="private_address")
for addr in ("100.100.100.100", "100.64.1.1", "169.254.169.254", "10.0.0.5", "172.16.0.9", "224.0.0.1", "0.0.0.0", "192.168.31.2"):
    resolver["answers"]["priv-name.test"] = [addr]
    case(f"allow-listed NAME resolving to {addr}", "https://priv-name.test/", expect_reason="private_address")
resolver["answers"]["mixed.test"] = [PUBLIC, "192.168.31.1"]
case("mixed answer (one public + one private) is refused as a whole", "https://mixed.test/", expect_reason="private_address")
resolver["answers"]["priv-name.test"] = ["192.168.31.1"]
case("redirect to an allow-listed NAME that resolves to a private address", V + "to-private-name", expect_reason="private_address")
resolver["answers"]["v6only.test"] = []
case("name with no IPv4 answer", "https://v6only.test/", expect_reason="dns_no_ipv4")

# DNS rebinding: the first answer is public, any later lookup would be private. The relay must look up ONCE
# per hop and connect to exactly the address it checked.
resolver["calls"].clear()
resolver["answers"]["rebind.test"] = lambda n: [PUBLIC] if n == 1 else ["127.0.0.1"]
case("DNS rebinding (public first, private second)", "https://rebind.test/ok", expect_status=200)
results.append((resolver["calls"].count("rebind.test") == 1 and connects == [PUBLIC],
                "rebinding: exactly one lookup, connection pinned to the checked address",
                f"lookups={resolver['calls'].count('rebind.test')} connected_to={connects}", 0))

# circuit breaker: 5 x 403 in a row opens it; then the host is refused without any connection
relay.save_state({})
for _ in range(5):
    case("target answers 403 (counts towards the breaker)", V + "403", expect_status=403)
connects.clear()
case("breaker is OPEN: refused, no connection made", V + "ok", expect_reason="circuit_open")
results.append((connects == [], "breaker: no connection while open", f"connects={connects}", 0))
ev = open(os.path.join(TMP, "events.jsonl")).read() if os.path.exists(os.path.join(TMP, "events.jsonl")) else ""
results.append(('"kind": "breaker_open"' in ev, "breaker: alert event written", ev.strip().splitlines()[-1][:100] if ev else "none", 0))
relay.save_state({})
case("challenge page served as JSON counts as blocked", V + "json-html", expect_status=200)

# private-address event written for the watcher
results.append(('"kind": "private_address"' in ev + open(os.path.join(TMP, "events.jsonl")).read(), "private-address attempts write an alert event", "", 0))

fails = [r for r in results if not r[0]]
w = max(len(r[1]) for r in results)
for ok, name, detail, secs in results:
    print(("PASS " if ok else "FAIL ") + name.ljust(w) + "  " + detail + (f"  ({secs}s)" if secs else ""))
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
