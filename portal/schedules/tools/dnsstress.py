#!/usr/bin/env python3
"""A small DNS load generator for the live proofs (stdlib only): plain UDP queries to one resolver, a mix of well-known names (cache hits) and unique names under
real domains (cache misses that must be recursed through DoH), counted per resolver. A lookup FAILS when no answer arrives in `timeout` s or the rcode is
SERVFAIL/REFUSED/FORMERR; NOERROR and NXDOMAIN are both a working resolver. AdGuard's blocked answers (0.0.0.0, NXDOMAIN) are answers too.

  dnsstress.py 192.168.31.2 192.168.31.5 --seconds 120 --rate 20      # prints one JSON line per resolver at the end
Used as a library by live_maps.py (Stress.start / Stress.stop)."""
import json
import random
import socket
import struct
import sys
import threading
import time

ORDINARY = ["www.wikipedia.org", "www.bbc.co.uk", "www.seznam.cz", "www.idnes.cz", "github.com", "www.python.org", "debian.org", "deb.debian.org", "www.ikea.com",
            "www.mozilla.org", "cloudflare.com", "www.apple.com", "www.microsoft.com", "www.amazon.de", "www.o2.cz", "api.telegram.org", "www.cnn.com", "www.nasa.gov",
            "www.eff.org", "www.openstreetmap.org", "ubuntu.com", "kernel.org", "www.gnu.org", "www.rust-lang.org", "www.ietf.org", "weather.com", "example.com",
            "www.google.com", "connectivitycheck.gstatic.com", "play.googleapis.com"]
UNIQUE_PARENTS = ["wikipedia.org", "seznam.cz", "debian.org", "github.com", "cloudflare.com", "example.com", "mozilla.org", "kernel.org"]


def build(name, qid, qtype=1):
    hdr = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00" + struct.pack(">HH", qtype, 1)
    return hdr + q


def lookup(server, name, timeout=3.0):
    """Returns (ok, rcode or None, seconds)."""
    qid = random.randrange(65536)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.sendto(build(name, qid), (server, 53))
        while True:
            data, _ = s.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                rcode = data[3] & 0x0F
                return rcode in (0, 3), rcode, time.time() - t0
    except socket.timeout:
        return False, None, time.time() - t0
    except OSError:
        return False, None, time.time() - t0
    finally:
        s.close()


class Stress:
    def __init__(self, servers, rate=20, workers=4):
        self.servers, self.rate, self.workers = list(servers), rate, workers
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.stats = {s: {"lookups": 0, "failed": 0, "max_s": 0.0, "failures": []} for s in self.servers}
        self.threads = []

    def _run(self, server, idx):
        rng = random.Random(idx * 7919 + int(time.time()))
        gap = self.workers / float(self.rate)
        while not self.stop_ev.is_set():
            t = time.time()
            if rng.random() < 0.5:
                name = rng.choice(ORDINARY)
            else:
                name = "x%08x.%s" % (rng.getrandbits(32), rng.choice(UNIQUE_PARENTS))
            ok, rcode, secs = lookup(server, name)
            with self.lock:
                st = self.stats[server]
                st["lookups"] += 1
                st["max_s"] = max(st["max_s"], secs)
                if not ok:
                    st["failed"] += 1
                    if len(st["failures"]) < 20:
                        st["failures"].append({"name": name, "rcode": rcode, "at": time.strftime("%H:%M:%S")})
            self.stop_ev.wait(max(0.0, gap - (time.time() - t)))

    def start(self):
        for s in self.servers:
            for i in range(self.workers):
                th = threading.Thread(target=self._run, args=(s, i), daemon=True)
                th.start()
                self.threads.append(th)
        return self

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.stats))

    def stop(self):
        self.stop_ev.set()
        for th in self.threads:
            th.join(timeout=10)
        return self.snapshot()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    seconds = int(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 60
    rate = int(sys.argv[sys.argv.index("--rate") + 1]) if "--rate" in sys.argv else 20
    servers = [a for a in args if a.count(".") == 3]
    st = Stress(servers, rate=rate).start()
    time.sleep(seconds)
    for s, v in st.stop().items():
        print(json.dumps({"server": s, **v}))


if __name__ == "__main__":
    main()
