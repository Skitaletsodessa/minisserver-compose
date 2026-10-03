#!/usr/bin/env python3
"""Fetch relay (Task 23). NOT a general proxy: it fetches an allow-listed set of https pages for one caller (a
Cloudflare Worker) through the house's IP, and refuses everything else.

Read this top to bottom; it is meant to be read in one sitting. The order of the checks in handle_fetch() is the
security model:

  1. kill switch / daily cap                      (cheap, before any work)
  2. HMAC signature + timestamp + replay          (who is asking)
  3. URL rules: https only, allow-listed host,    (what may be asked)
     no userinfo, no odd port, sane characters
  4. politeness: per-host token bucket, per-host concurrency, circuit breaker
  5. resolve the name; EVERY answer must be a public IPv4 address; connect to that exact IP (pinned) with the
     original name as SNI/Host           (defeats DNS rebinding: there is no second lookup to poison)
  6. redirects are followed by hand, at most 3, each hop goes through steps 3-5 again
  7. response shaping: size cap, deadline, content-type filter, gzip bomb guard, header filter

Everything that is refused is counted and logged with a reason. No secret is ever logged.
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- configuration (environment, see .env.example)
SECRET = os.environ.get("RELAY_SECRET", "").encode()
ALLOWLIST_FILE = os.environ.get("RELAY_ALLOWLIST", "/app/allowlist.txt")
STATE_DIR = os.environ.get("RELAY_STATE_DIR", "/state")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "8080"))
TS_WINDOW = int(os.environ.get("RELAY_TS_WINDOW", "60"))            # seconds, +/-
MAX_BODY = int(os.environ.get("RELAY_MAX_BODY", str(5 * 1024 * 1024)))
TOTAL_TIMEOUT = float(os.environ.get("RELAY_TOTAL_TIMEOUT", "20"))   # seconds for the whole fetch incl. redirects
MAX_REDIRECTS = 3
RATE_PER_SEC = float(os.environ.get("RELAY_RATE_PER_SEC", "1"))      # per target host
BURST = float(os.environ.get("RELAY_BURST", "3"))
HOST_CONCURRENCY = int(os.environ.get("RELAY_HOST_CONCURRENCY", "2"))
GLOBAL_CONCURRENCY = int(os.environ.get("RELAY_GLOBAL_CONCURRENCY", "8"))
DAILY_CAP = int(os.environ.get("RELAY_DAILY_CAP", "2000"))
BREAKER_THRESHOLD = int(os.environ.get("RELAY_BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN = int(os.environ.get("RELAY_BREAKER_COOLDOWN", "1800"))
AUTH_FAIL_PER_MIN = int(os.environ.get("RELAY_AUTH_FAIL_PER_MIN", "10"))  # per source, then 429
TRUSTED_PROXIES = {p for p in os.environ.get("RELAY_TRUSTED_PROXIES", "").split(",") if p}
HEADER_READ_TIMEOUT = 10        # slow-loris: the caller has this long to send its request
MAX_URL_LEN = 2048
TZ = ZoneInfo("Europe/Prague")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/129.0.0.0 Safari/537.36")
FORWARD_HEADERS = {"user-agent": "User-Agent", "accept": "Accept", "accept-language": "Accept-Language",
                   "referer": "Referer", "cookie": "Cookie"}      # caller sends them as X-Fwd-<name>
TEXT_TYPES = ("text/", "application/json", "application/xhtml+xml", "application/xml", "application/ld+json")
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
CHALLENGE_MARKERS = (b"just a moment", b"cf-chl", b"captcha", b"access denied", b"attention required")

# http.client / http.server limits tightened: a "huge header" must be refused cheaply
http.client._MAXLINE = 16384
http.client._MAXHEADERS = 60
http.server._MAXLINE = 8192


# ---------------------------------------------------------------- state, counters, log
_lock = threading.Lock()
counters = {}                      # reason -> count (since start)
events = []                        # alert events picked up by the host-side watcher (state/events.jsonl)


def log(**kw):
    kw["ts"] = datetime.now(TZ).isoformat(timespec="seconds")
    sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def count(reason):
    with _lock:
        counters[reason] = counters.get(reason, 0) + 1


def event(kind, **kw):
    """Appended to state/events.jsonl; the watcher on the host turns these into Telegram messages."""
    rec = {"t": int(time.time()), "kind": kind, **kw}
    try:
        with open(os.path.join(STATE_DIR, "events.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def state_path():
    return os.path.join(STATE_DIR, "relay.json")


def load_state():
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    tmp = state_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, state_path())


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- allow-list
def load_allowlist():
    """allowlist.txt: one rule per line:  host_or_*.suffix  [images] [cookies] [rate=N] [burst=N] [concurrency=N]
    (# comments). rate/burst/concurrency override the politeness defaults for that host only, within HARD_MAX."""
    rules = []
    try:
        for line in open(ALLOWLIST_FILE):
            line = line.split("#", 1)[0].strip().lower()
            if not line:
                continue
            host, *flags = line.split()
            rules.append((host, set(flags)))
    except OSError:
        pass
    return rules


HARD_MAX = {"rate": 5.0, "burst": 10.0, "concurrency": 6}     # a typo in allowlist.txt must not open a flood


def host_limits(flags):
    """Per-host politeness: defaults from the environment, overridden by rate=/burst=/concurrency= flags, clamped."""
    lim = {"rate": RATE_PER_SEC, "burst": BURST, "concurrency": HOST_CONCURRENCY}
    for f in flags:
        if "=" in f:
            k, _, v = f.partition("=")
            if k in lim:
                try:
                    lim[k] = max(0.1, min(float(v), HARD_MAX[k]))
                except ValueError:
                    pass
    lim["concurrency"] = max(1, int(lim["concurrency"]))
    return (lim["rate"], lim["burst"], lim["concurrency"])


def match_allowlist(host):
    for pat, flags in load_allowlist():
        if pat.startswith("*."):
            if host.endswith(pat[1:]) and host != pat[2:]:
                return pat, flags
        elif host == pat:
            return pat, flags
    return None


# ---------------------------------------------------------------- URL rules
class Refuse(Exception):
    def __init__(self, status, reason, detail=""):
        self.status, self.reason, self.detail = status, reason, detail


_BAD_CHARS = re.compile(r"[\x00-\x20\x7f\\]")


def validate_url(raw):
    """Returns (host_ascii, port, path_and_query, flags). Raises Refuse. Applied to the first URL and to every
    redirect target."""
    if len(raw) > MAX_URL_LEN:
        raise Refuse(400, "url_too_long")
    if _BAD_CHARS.search(raw):
        raise Refuse(400, "url_bad_chars")
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:
        raise Refuse(400, "url_unparsable")
    if u.scheme != "https":
        raise Refuse(403, "scheme_not_https")
    if "@" in u.netloc:
        raise Refuse(403, "userinfo_in_url")
    try:
        port = u.port
    except ValueError:
        raise Refuse(400, "url_bad_port")
    if port not in (None, 443):
        raise Refuse(403, "port_not_allowed")
    host = (u.hostname or "").rstrip(".").lower()
    if not host:
        raise Refuse(400, "url_no_host")
    try:
        host = host.encode("idna").decode("ascii")          # normalise IDN before comparing
    except UnicodeError:
        raise Refuse(400, "url_bad_idn")
    try:
        ipaddress.ip_address(host)
        raise Refuse(403, "ip_literal_host")                  # names only; an IP literal is never allow-listed
    except ValueError:
        pass
    m = match_allowlist(host)
    if not m:
        raise Refuse(403, "host_not_allowlisted", host)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return host, 443, path, m[1]


# ---------------------------------------------------------------- address rules (application layer)
def is_public_v4(addr):
    """True only for a globally routable IPv4 address. Private, loopback, link-local, CGNAT/tailnet (100.64/10),
    multicast, reserved, unspecified and the documentation ranges are all not 'global' for Python's ipaddress."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return ip.is_global and not ip.is_multicast


def resolve_public(host, deadline):
    """Resolve and require every IPv4 answer to be public. Returns the first answer (the pinned address)."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        raise Refuse(502, "dns_failed", host)
    addrs = [i[4][0] for i in infos]
    if not addrs:
        raise Refuse(502, "dns_no_ipv4", host)
    bad = [a for a in addrs if not is_public_v4(a)]
    if bad:
        event("private_address", host=host, addr=bad[0])
        raise Refuse(403, "private_address", f"{host} -> {bad[0]}")
    return addrs[0]


# ---------------------------------------------------------------- politeness: token bucket, concurrency, breaker
class HostGate:
    def __init__(self, limits):
        self.limits = limits
        self.rate, self.burst, conc = limits
        self.tokens, self.t = self.burst, time.monotonic()
        self.sem = threading.BoundedSemaphore(conc)
        self.lk = threading.Lock()

    def take(self, wait_max=3.0):
        end = time.monotonic() + wait_max
        while True:
            with self.lk:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.1)


_gates = {}
_global_sem = threading.BoundedSemaphore(GLOBAL_CONCURRENCY)


def gate(host, flags=()):
    limits = host_limits(flags)
    with _lock:
        g = _gates.get(host)
        if g is None or g.limits != limits:          # first use, or the allow-list changed the limits
            g = _gates[host] = HostGate(limits)
        return g


def breaker_check(host):
    st = load_state()
    b = (st.get("breaker") or {}).get(host)
    if b and b.get("open_until", 0) > time.time():
        raise Refuse(503, "circuit_open", f"{host} until {int(b['open_until'])}")


def breaker_record(host, bad):
    with _lock:
        st = load_state()
        br = st.setdefault("breaker", {})
        b = br.setdefault(host, {"streak": 0, "open_until": 0})
        if bad:
            b["streak"] += 1
            if b["streak"] >= BREAKER_THRESHOLD:
                b["open_until"] = time.time() + BREAKER_COOLDOWN
                b["streak"] = 0
                event("breaker_open", host=host, cooldown=BREAKER_COOLDOWN)
                log(level="warn", msg="circuit breaker opened", host=host)
        else:
            b["streak"] = 0
        save_state(st)


def daily_use(increment=False):
    with _lock:
        st = load_state()
        d = st.setdefault("daily", {})
        if d.get("day") != today():
            d.clear()
            d.update({"day": today(), "n": 0, "alerted80": False})
        if increment:
            d["n"] += 1
            if d["n"] >= 0.8 * DAILY_CAP and not d.get("alerted80"):
                d["alerted80"] = True
                event("cap80", used=d["n"], cap=DAILY_CAP)
        save_state(st)
        return d["n"]


# ---------------------------------------------------------------- one fetch hop
def _close_quietly(x):
    try:
        x.close()
    except OSError:
        pass


def fetch_hop(host, path, flags, caller_headers, deadline):
    ip = resolve_public(host, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refuse(504, "timeout")
    ctx = ssl.create_default_context()                       # certificates ARE verified, against the original name
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        raw = socket.create_connection((ip, 443), timeout=min(8, remaining))
        s = ctx.wrap_socket(raw, server_hostname=host)
    except (OSError, ssl.SSLError) as e:
        raise Refuse(502, "connect_failed", type(e).__name__)
    conn = http.client.HTTPSConnection(host, 443, timeout=min(10, remaining), context=ctx)
    # Hard wall-clock deadline: a watchdog closes the socket when TOTAL_TIMEOUT is up. Per-recv timeouts alone are
    # not enough - a peer that sends one byte every few seconds never trips them.
    watchdog = threading.Timer(max(0.1, remaining), lambda: _close_quietly(s))
    watchdog.daemon = True
    watchdog.start()
    conn.sock = s                                            # pinned: connect() is skipped, Host stays the name
    hdrs = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip", "Connection": "close"}
    for k, v in caller_headers.items():
        if k == "Cookie" and "cookies" not in flags:
            continue
        hdrs[k] = v
    try:
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        status = resp.status
        loc = resp.getheader("Location")
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        enc = (resp.getheader("Content-Encoding") or "").lower()
        set_cookies = resp.msg.get_all("Set-Cookie") or []
        lang = resp.getheader("Content-Language")
        if 300 <= status < 400 and status != 304:
            return {"status": status, "loc": loc, "body": b"", "ctype": ctype, "cookies": set_cookies}
        allowed = TEXT_TYPES + (IMAGE_TYPES if "images" in flags else ())
        if status not in (204, 304) and not any(ctype.startswith(t) for t in allowed):
            raise Refuse(502, "content_type_not_allowed", ctype or "none")
        body = b""
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else None
        if enc not in ("", "gzip", "identity"):
            raise Refuse(502, "content_encoding_unsupported", enc)
        while True:
            if time.monotonic() > deadline:
                raise Refuse(504, "timeout_reading_body")
            if resp.isclosed():                           # a "Connection: close" response closes itself after the last byte
                break
            try:
                s.settimeout(max(0.5, min(5.0, deadline - time.monotonic())))
            except OSError:                               # socket already closed by the library at EOF: nothing left to read
                break
            try:
                chunk = resp.read1(16384)
            except (socket.timeout, TimeoutError):
                raise Refuse(504, "timeout_reading_body")
            if not chunk:
                break
            if decomp:
                chunk = decomp.decompress(chunk, MAX_BODY + 1 - len(body))   # zip-bomb guard: output is capped
                if decomp.unconsumed_tail and len(body) + len(chunk) > MAX_BODY:
                    raise Refuse(502, "body_too_large")
            body += chunk
            if len(body) > MAX_BODY:
                raise Refuse(502, "body_too_large")
        return {"status": status, "loc": None, "body": body, "ctype": ctype, "cookies": set_cookies, "lang": lang}
    except Refuse:
        raise
    except (OSError, http.client.HTTPException, zlib.error) as e:
        if time.monotonic() >= deadline:
            raise Refuse(504, "timeout")
        raise Refuse(502, "upstream_error", type(e).__name__)
    finally:
        watchdog.cancel()
        _close_quietly(conn)


def looks_blocked(status, body, ctype):
    if status in (403, 429):
        return True
    head = body[:2000].lower()
    if "json" in ctype and head.lstrip().startswith(b"<"):
        return True                                     # expected JSON, got markup: a challenge page
    return status == 200 and any(m in head for m in CHALLENGE_MARKERS) and b"<html" in head and len(body) < 20000


def do_fetch(url, caller_headers):
    deadline = time.monotonic() + TOTAL_TIMEOUT
    cur = url
    result = None
    cookies = []
    for hop in range(MAX_REDIRECTS + 1):
        host, _port, path, flags = validate_url(cur)         # steps 3 again for every hop
        breaker_check(host)
        g = gate(host, flags)
        if not g.sem.acquire(timeout=3):
            raise Refuse(429, "host_concurrency")
        try:
            if not g.take():
                raise Refuse(429, "host_rate_limit", host)
            res = fetch_hop(host, path, flags, caller_headers, deadline)
        finally:
            g.sem.release()
        if "cookies" in flags:
            for c in res.get("cookies", []):
                pair = c.split(";", 1)[0]
                cookies = [x for x in cookies if x.split("=", 1)[0] != pair.split("=", 1)[0]] + [pair]   # one per name, last wins
        if res["loc"] is not None:
            if hop == MAX_REDIRECTS:
                raise Refuse(502, "too_many_redirects")
            nxt = urllib.parse.urljoin(f"https://{host}{path}", res["loc"])
            try:
                validate_url(nxt)
            except Refuse as r:
                raise Refuse(502, "redirect_refused:" + r.reason, nxt[:120])
            cur = nxt
            continue
        breaker_record(host, looks_blocked(res["status"], res["body"], res["ctype"]))
        res["host"], res["path"], res["cookie_pairs"] = host, path, cookies
        return res
    raise Refuse(502, "too_many_redirects")


# ---------------------------------------------------------------- HTTP front end
_seen = {}                    # signature -> expiry (replay guard)
_auth_fail = {}               # source -> [timestamps]


def sign(ts, url):
    return hmac.new(SECRET, f"{ts}\n{url}".encode(), hashlib.sha256).hexdigest()


def source_of(handler):
    peer = handler.client_address[0]
    if peer in TRUSTED_PROXIES:
        xf = handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For", "")
        if xf:
            return xf.split(",")[0].strip()[:45]
    return peer


def check_auth(handler, url):
    ts = handler.headers.get("X-Relay-Ts", "")
    sig = handler.headers.get("X-Relay-Sig", "")
    src = source_of(handler)
    now = time.time()
    with _lock:
        fails = [t for t in _auth_fail.get(src, []) if now - t < 60]
        _auth_fail[src] = fails
        if len(fails) >= AUTH_FAIL_PER_MIN:
            raise Refuse(429, "auth_rate_limited")
    ok = False
    try:
        ok = bool(sig) and abs(now - int(ts)) <= TS_WINDOW and hmac.compare_digest(sig, sign(ts, url))
    except ValueError:
        ok = False
    if ok:
        with _lock:
            for k in [k for k, e in _seen.items() if e < now]:
                del _seen[k]
            if sig in _seen:
                ok = False
            else:
                _seen[sig] = now + 2 * TS_WINDOW
        if not ok:
            count("replay")
    if not ok:
        with _lock:
            _auth_fail[src].append(now)
        event("auth_fail", src=src)
        raise Refuse(401, "bad_signature")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = HEADER_READ_TIMEOUT                       # also the slow-loris limit on reading the request
    server_version = "relay"
    sys_version = ""

    def log_message(self, *a):                          # we write our own structured log
        pass

    def send_plain(self, status, reason="", extra=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("X-Relay-Origin", "relay")
        if reason and status != 401:                    # a 401 says nothing about why
            self.send_header("X-Relay-Reason", reason.split(":")[0][:40])
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self):
        t0 = time.monotonic()
        if len(self.path) > MAX_URL_LEN + 256:
            return self.send_plain(414, "request_uri_too_long")
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if u.path != "/fetch":
            return self.send_plain(404, "no_such_path")
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        url = (q.get("url") or [""])[0]
        caller = (self.headers.get("X-Relay-Caller") or "-")[:24]
        status, reason, nbytes, target, detail = 200, "", 0, "-", ""
        got_global = False
        try:
            st = load_state()
            if st.get("killed"):
                raise Refuse(503, "killed")
            if daily_use() >= DAILY_CAP:
                raise Refuse(503, "daily_cap")
            check_auth(self, url)
            if not _global_sem.acquire(timeout=2):
                raise Refuse(503, "global_concurrency")
            got_global = True
            hdrs = {}
            for k, canon in FORWARD_HEADERS.items():
                v = self.headers.get("X-Fwd-" + canon)
                if v:
                    hdrs[canon] = v[:4096]
            daily_use(increment=True)
            res = do_fetch(url, hdrs)
            target = res["host"] + res["path"].split("?", 1)[0]
            body = res["body"]
            nbytes, status = len(body), res["status"]
            self.send_response(res["status"])
            self.send_header("Content-Type", res["ctype"] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Relay-Origin", "target")
            if res.get("lang"):
                self.send_header("Content-Language", res["lang"][:20])
            if res["cookie_pairs"]:
                packed = base64.b64encode(json.dumps(res["cookie_pairs"][:30]).encode()).decode()
                self.send_header("X-Relay-Cookies", packed[:6000])
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Refuse as r:
            status, reason, detail = r.status, r.reason, str(r.detail)[:80]
            count(r.reason.split(":")[0])
            self.send_plain(r.status, r.reason)
            if r.reason.startswith("redirect_refused:private") or r.reason == "private_address":
                log(level="warn", msg="private address attempt", detail=r.detail)
        except Exception as e:                         # never leak internals
            status, reason = 500, "internal:" + type(e).__name__
            count("internal_error")
            try:
                self.send_plain(500, "internal")
            except OSError:
                pass
        finally:
            if got_global:
                _global_sem.release()
            uq = urllib.parse.urlsplit(url)
            log(caller=caller, src=source_of(self), target=target if target != "-" else (uq.hostname or "-") + uq.path[:80],
                query_keys=sorted(urllib.parse.parse_qs(uq.query).keys())[:12], status=status, bytes=nbytes,
                ms=int((time.monotonic() - t0) * 1000), refused=reason or None, detail=detail or None)

    def do_POST(self):
        self.send_plain(405, "method_not_allowed")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_POST


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    request_queue_size = 64


def flush_counters():
    """Counters of refusals by reason, for relay-status and the watcher (every 10 s)."""
    while True:
        time.sleep(10)
        try:
            with _lock:
                snap = dict(counters)
            tmp = os.path.join(STATE_DIR, "counters.json.tmp")
            with open(tmp, "w") as f:
                json.dump({"t": int(time.time()), "since_start": snap}, f)
            os.replace(tmp, os.path.join(STATE_DIR, "counters.json"))
        except OSError:
            pass


def main():
    if len(SECRET) < 24:
        sys.exit("RELAY_SECRET missing or too short (>= 24 characters required)")
    os.makedirs(STATE_DIR, exist_ok=True)
    threading.Thread(target=flush_counters, daemon=True).start()
    log(msg="relay starting", port=LISTEN_PORT, daily_cap=DAILY_CAP, hosts=[r[0] for r in load_allowlist()])
    Server(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
