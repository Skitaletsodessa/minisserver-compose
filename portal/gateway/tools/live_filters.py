#!/usr/bin/env python3
"""Live proof of the AdGuard filter-list safety net on the REAL house (Task 29, Phase G/H), run on the server. Synthetic admin+parent person `probe-padmin`
(token in ~/.config/gw-probe, never printed).

What it does, in order, and how it stays safe:
  1. a scratch list server on the TAILNET address only (the allow-list of listener-watch has a matching `until=` entry for this port) serving two tiny lists:
       bad.txt   blocks ONLY example.org (+ one sub-name): the harmless canary that is part of the gateway's must-resolve set, so a deliberate failure hurts nobody
       good.txt  blocks only names under .invalid (they do not exist)
  2. the SSRF guard: without an operator allow entry the gateway refuses a private/tailnet address (and 127.0.0.1, 10.x ...)
  3. the BAD list is added through the API -> the probe must fail on example.org -> automatic revert -> probe again; the lists end exactly as they were
  4. the GOOD list is added -> every step passes, including the replica (a witness name looked up on the Pi) and the parity checker -> then REMOVED the same way
  5. throughout, a monitor looks up ordinary names against BOTH resolvers (origin 192.168.31.2 and replica 192.168.31.5) every 0.4 s and counts failures; example.org
     is watched separately to measure how long the canary was blocked
The only change that reaches the house is this: for a few seconds the canary example.org is blocked (nothing uses it), and two scratch lists come and go.
  live_filters.py run
"""
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway")
from gw import agh as A  # noqa: E402

BASE = "http://127.0.0.1:17444"
TAILNET = "100.76.61.8"
PORT = 18777
ORIGIN, REPLICA = "192.168.31.2", "192.168.31.5"
ALLOW = "/srv/apps/portal/gateway/filter-fetch-allow.txt"
CONT = ["www.google.com", "controlplane.tailscale.com", "api.telegram.org", "deb.debian.org", "github.com", "connectivitycheck.gstatic.com"]
LISTS = {"/bad.txt": "! scratch proof: blocks only the canary\n||example.org^\n||scratch-canary-sub.example.org^\n",
         "/good.txt": "! scratch proof: names that do not exist\n||scratch-proof-1.invalid^\n||scratch-proof-2.invalid^\n||scratch-proof-3.invalid^\n"}


def stamp(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def token():
    p = os.path.expanduser("~/.config/gw-probe/probe-padmin")
    if os.stat(p).st_mode & 0o077:
        sys.exit("token file must be mode 600")
    return open(p).read().strip()


def call(method, path, body=None, timeout=700):
    h = {"Authorization": "Bearer " + token(), "Idempotency-Key": "live-" + os.urandom(6).hex(), "Content-Type": "application/json"}
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = LISTS.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Monitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_flag = False
        self.sent = {ORIGIN: 0, REPLICA: 0}
        self.failed = {ORIGIN: [], REPLICA: []}
        self.canary = []                      # (time, "blocked"|"open") transitions of example.org on the origin

    def run(self):
        last = None
        i = 0
        while not self.stop_flag:
            for srv in (ORIGIN, REPLICA):
                name = CONT[i % len(CONT)]
                self.sent[srv] += 1
                try:
                    a = A.dns_query(srv, name, timeout=2.0)
                    if a["rcode"] != 0 or not a["addrs"] or A.dns_blocked(a):
                        self.failed[srv].append((time.strftime("%H:%M:%S"), name, "rcode %s %s" % (a["rcode"], a["addrs"][:1])))
                except OSError as e:
                    self.failed[srv].append((time.strftime("%H:%M:%S"), name, "no answer: %s" % e))
            try:
                a = A.dns_query(ORIGIN, "example.org", timeout=2.0)
                state = "blocked" if A.dns_blocked(a) else "open"
                if state != last:
                    self.canary.append((time.strftime("%H:%M:%S"), state))
                    last = state
            except OSError:
                pass
            i += 1
            time.sleep(0.4)


def summary(res):
    ch = res["change"]
    return "result=%s  action=%s  steps=%s  reason=%s" % (res["result"], ch["action"], [(s["name"], "ok" if s["ok"] else "FAIL") for s in ch["steps"]], ch["reason"])


def lists():
    st, js = call("GET", "/v1/parental/filter-lists")
    assert st == 200, (st, js)
    return [(l["name"], l["enabled"], l["rules_count"]) for l in js["lists"]], js


def main():
    mon = Monitor()
    srv = http.server.ThreadingHTTPServer((TAILNET, PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stamp("scratch list server on %s:%d (tailnet address only)" % (TAILNET, PORT))
    before, _ = lists()
    stamp("lists before: %s" % before)
    mon.start()
    time.sleep(2)
    try:
        # 2. the guard
        for url in ("http://127.0.0.1:%d/bad.txt" % PORT, "http://%s:%d/bad.txt" % (TAILNET, PORT), "https://10.0.0.1/x.txt", "https://192.168.31.2/x.txt"):
            st, js = call("POST", "/v1/parental/filter-lists", {"url": url, "name": "must be refused"})
            stamp("guard: %-40s -> %s %s" % (url, st, (js or {}).get("error", {}).get("code")))
        open(ALLOW, "w").write("%s:%d   # live proof, removed at the end of the script\n" % (TAILNET, PORT))
        # 3. the bad list
        t0 = time.time()
        st, js = call("POST", "/v1/parental/filter-lists", {"url": "http://%s:%d/bad.txt" % (TAILNET, PORT), "name": "SCRATCH bad list (proof)"})
        stamp("BAD list: HTTP %s in %.1f s -> %s" % (st, time.time() - t0, summary(js) if st == 200 else js))
        bad_change = js["change"]["id"] if st == 200 else None
        after_bad, _ = lists()
        stamp("lists after the bad list: %s  (unchanged: %s)" % (after_bad, after_bad == before))
        # 4. the good list
        t0 = time.time()
        st, js = call("POST", "/v1/parental/filter-lists", {"url": "http://%s:%d/good.txt" % (TAILNET, PORT), "name": "SCRATCH good list (proof)"})
        stamp("GOOD list: HTTP %s in %.1f s -> %s" % (st, time.time() - t0, summary(js) if st == 200 else js))
        good_id = js["list"]["id"] if st == 200 and js.get("list") else None
        for step in (js["change"]["steps"] if st == 200 else []):
            stamp("    step %-22s %s  %s" % (step["name"], "ok" if step["ok"] else "FAIL", step.get("detail", "")))
        if good_id:
            try:
                r = A.dns_query(REPLICA, "scratch-proof-1.invalid", timeout=3)
                o = A.dns_query(ORIGIN, "scratch-proof-1.invalid", timeout=3)
                stamp("witness scratch-proof-1.invalid: origin blocked=%s replica blocked=%s" % (A.dns_blocked(o), A.dns_blocked(r)))
            except OSError as e:
                stamp("witness lookup failed: %s" % e)
            t0 = time.time()
            st, js = call("DELETE", "/v1/parental/filter-lists/%d" % good_id)
            stamp("REMOVE good list: HTTP %s in %.1f s -> %s" % (st, time.time() - t0, summary(js) if st == 200 else js))
            for step in (js["change"]["steps"] if st == 200 else []):
                stamp("    step %-22s %s  %s" % (step["name"], "ok" if step["ok"] else "FAIL", step.get("detail", "")))
            time.sleep(1)
            r = A.dns_query(REPLICA, "scratch-proof-1.invalid", timeout=3)
            stamp("after the removal the replica blocks the witness: %s" % A.dns_blocked(r))
        st, h = call("GET", "/v1/health")
        stamp("gateway /v1/health filter_reverts: %s (bad change was %s)" % ([(x["id"], x["list_name"], (x["reason"] or "")[:80]) for x in h.get("filter_reverts", [])], bad_change))
    finally:
        mon.stop_flag = True
        mon.join(5)
        try:
            os.remove(ALLOW)
        except OSError:
            pass
        srv.shutdown()
        after, js = lists()
        stamp("lists at the end: %s  (same as before: %s)" % (after, after == before))
        stamp("monitor: origin %d lookups, %d failed %s; replica %d lookups, %d failed %s" % (mon.sent[ORIGIN], len(mon.failed[ORIGIN]), mon.failed[ORIGIN][:5],
                                                                                         mon.sent[REPLICA], len(mon.failed[REPLICA]), mon.failed[REPLICA][:5]))
        stamp("canary example.org on the origin: %s" % mon.canary)
        stamp("history:")
        for h in js["history"][:4]:
            stamp("    %s %-8s %-28s %-9s %s" % (h["at"], h["action"], h["list_name"], h["result"], (h["reason"] or "")[:90]))


if __name__ == "__main__":
    main()
