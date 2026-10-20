"""AdGuard Home client of the gateway (Task 29, Phase G): the ORIGIN only (127.0.0.1:3000), basic auth with the admin password read from a file that is
mounted read-only into the container (never copied, never logged, never in an error text). Used for: the query log (activity, read on demand, nothing stored),
check_host, clients, stats and the filter-list calls of the safety net. The replica (Pi) is never contacted with credentials: it is observed by real DNS
queries (`dns_query`) and by the parity checker's state file.
"""
import asyncio
import base64
import json
import os
import random
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request


class AghError(Exception):
    """status None = unreachable. The message never contains the password or the Authorization header."""
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Agh:
    def __init__(self, base, password_file, user="admin", timeout=20.0):
        self.base = base.rstrip("/")
        self.password_file = password_file
        self.user = user
        self.timeout = timeout

    def available(self):
        return bool(self.password_file) and os.path.exists(self.password_file)

    def _headers(self):
        with open(self.password_file, encoding="utf-8") as f:
            pw = f.read().strip()
        return {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (self.user, pw)).encode()).decode(), "Content-Type": "application/json"}

    def _sync(self, method, path, body=None, timeout=None):
        try:
            headers = self._headers()
        except OSError:
            raise AghError(None, "the AdGuard credentials are not available")
        req = urllib.request.Request(self.base + path, data=(json.dumps(body).encode() if body is not None else None), method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise AghError(e.code, (e.read() or b"")[:300].decode("utf-8", "replace").strip() or "HTTP %d" % e.code)
        except (urllib.error.URLError, OSError, ValueError):
            raise AghError(None, "AdGuard did not answer")
        s = raw.strip()
        if s.startswith((b"{", b"[")):
            try:
                return json.loads(s)
            except ValueError:
                raise AghError(502, "AdGuard sent a broken answer")
        return s.decode("utf-8", "replace")

    async def call(self, method, path, body=None, timeout=None):
        return await asyncio.to_thread(self._sync, method, path, body, timeout)

    # ── helpers on top ──
    async def check_host(self, name, client=None):
        q = "/filtering/check_host?name=" + urllib.parse.quote(name)
        if client:
            q += "&client=" + urllib.parse.quote(client)
        return await self.call("GET", q)


def filtered(check):
    """A check_host answer says the name is blocked by a rule (custom rule, list or service)."""
    return str((check or {}).get("reason", "")).startswith("Filtered")


def blocking_list_ids(check):
    return {r.get("filter_list_id") for r in (check or {}).get("rules") or [] if r.get("filter_list_id") is not None}


# ── a minimal DNS client (UDP/53) for the probes: no library, A records only ────────────────────────────────
def _qname(name):
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("idna")
        if not 0 < len(b) < 64:
            raise ValueError("bad label")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _skip_name(buf, i):
    while True:
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def dns_query(server, name, port=53, timeout=3.0):
    """Returns {"rcode": int, "addrs": [ipv4 strings]}; raises OSError on timeout/refusal. TCP is not needed for an A answer of this size."""
    qid = random.randrange(0, 65536)
    pkt = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(pkt, (server, port))
        data, _ = s.recvfrom(4096)
    if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid:
        raise OSError("bad DNS answer")
    _qid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    addrs = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return {"rcode": flags & 0x0F, "addrs": addrs}


def dns_blocked(answer, mode="default"):
    """What AdGuard answers for a blocked name depends on its blocking mode: default / custom_ip -> an address in the sinkhole (0.0.0.0, ::); nxdomain -> rcode 3;
    refused -> rcode 5. In the default mode an NXDOMAIN is a name that does not exist, NOT a block (found by the live proof with .invalid test names)."""
    if mode == "nxdomain":
        return answer["rcode"] == 3
    if mode == "refused":
        return answer["rcode"] == 5
    return answer["rcode"] == 0 and bool(answer["addrs"]) and all(a in ("0.0.0.0", "::") for a in answer["addrs"])
