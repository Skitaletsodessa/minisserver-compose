#!/usr/bin/env python3
"""Tests for listener-watch with synthetic and captured `ss -tulnpH` output. A recording stub replaces watchnotify: nothing is sent."""
import datetime
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lw", os.path.join(HERE, "listener-watch.py"))
lw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lw)
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("   [%s]" % extra if not cond and extra else ""))
    if not cond:
        FAILS.append(name)


class Rec:
    def __init__(self):
        self.calls = []

    def alert(self, key, text, tier="A", **kw):
        self.calls.append(("alert", key, tier))
        self.texts = getattr(self, "texts", {})
        self.texts[key] = text

    def clear(self, key, text, **kw):
        self.calls.append(("clear", key))

    def alerts(self, tier=None):
        return sorted(k for a, k, *t in self.calls if a == "alert" and (tier is None or t[0] == tier))

    def socks(self):
        """the sockets named in the (single) tier-A group message, one per line"""
        return sorted(l.strip() for k, t in getattr(self, "texts", {}).items() if k.startswith("listener-new-") for l in t.splitlines() if l.startswith("  "))

    def clears(self):
        return sorted(k for a, k, *t in self.calls if a == "clear" and k.startswith("listener"))


ENTRIES = lw.load_allowlist(os.path.join(HERE, "allowlist.conf"), today=datetime.date(2026, 10, 20))


def line(netid, local, proc=None):
    return "%s UNCONN 0 0 %s 0.0.0.0:* %s" % (netid, local, ('users:(("%s",pid=1,fd=3))' % proc) if proc else "")


def run(text, state=None, entries=ENTRIES):
    n = Rec()
    st = lw.evaluate(n, lw.parse_ss(text), entries, state or {})
    return n, st


# ── parsing ──
p = lw.parse_ss("\n".join([line("tcp", "0.0.0.0:22", "sshd"), line("tcp", "[::]:22", "sshd"), line("udp", "*:5353", "python3"),
                           line("udp", "[fe80::5a47:caff:fe74:e255]%eno1:5353", "python3"), line("tcp", "127.0.0.1:8123", "python3"), line("tcp", "[::1]:8123"),
                           "garbage line", ""]))
check("parse: wildcard forms become '*'", [(x["addr"], x["port"]) for x in p[:3]] == [("*", 22), ("*", 22), ("*", 5353)])
check("parse: link-local zone is removed", p[3]["addr"] == "fe80::5a47:caff:fe74:e255" and p[3]["proc"] == "python3")
check("parse: a socket without process info keeps '?'", p[5]["proc"] == "?")
check("normalise drops loopback and duplicates", len(lw.normalise(p)) == 3, str(lw.normalise(p)))

# ── the real machine, captured 2026-10-20 ──
real = open(os.path.join(HERE, "ss-sample.txt")).read()            # captured 2026-10-21 07:46, outside an HA Tuya scan
n, st = run(real)
check("the real ss output of today: NO alert at all", n.alerts("A") == [], str(n.alerts("A")))
check("the real ss output of today: nothing expected is missing", n.alerts("C") == [], str(n.alerts("C")))
check("the real ss output (after the go2rtc removal): more than 40 non-loopback sockets were judged", st["checked"] > 40, str(st["checked"]))
before = open(os.path.join(HERE, "ss-before-go2rtc-removal.txt")).read()
n, _ = run(before)
check("the capture from BEFORE the removal: the watcher would have raised tier A for go2rtc on *:18555", len(n.alerts("A")) == 1 and n.socks() == ["tcp *:18555 (go2rtc)"], str(n.socks()))
# ── a new listener ──
new = real + line("tcp", "0.0.0.0:8888", "python3") + "\n"
n, st = run(new)
check("a NEW wildcard listener -> tier A, one alert for it", len(n.alerts("A")) == 1 and n.socks() == ["tcp *:8888 (python3)"], str(n.socks()))
n, st2 = run(new, st)
check("the same listener on the next run: alert is renewed (watchnotify dedups: same key), state keeps it", len(st2["alerted"]) == 1 and st2["alerted"] == st["alerted"] and n.alerts("A") == st["alerted"])
n, st3 = run(real, st2)
check("listener gone: cleared (recovery)", n.clears() == st2["alerted"] and st3["alerted"] == [])

new = real + line("tcp", "192.168.31.2:9999", "node") + "\n"
check("a new LAN-address listener -> tier A", run(new)[0].socks() == ["tcp 192.168.31.2:9999 (node)"])
new = real + line("udp", "100.76.61.8:7777", "python3") + "\n"
check("a new tailnet-address UDP listener -> tier A", run(new)[0].socks() == ["udp 100.76.61.8:7777 (python3)"])

# ── wrong owner on a known port ──
swapped = real.replace('("smbd",pid=1182,fd=29)', '("evil",pid=1,fd=29)')
n, _ = run(swapped)
check("a known port taken by a DIFFERENT process -> tier A", "tcp 192.168.31.2:445 (evil)" in n.socks(), str(n.socks()))
check("... and the expected smbd listener is reported missing (quiet)", "listener-missing-tcp-192.168.31.2-445" in n.alerts("C"))

# ── an expected listener disappears ──
gone = "\n".join(l for l in real.splitlines() if "192.168.31.2:445" not in l)
n, st = run(gone)
check("expected listener missing -> tier C only, no tier A", n.alerts("A") == [] and n.alerts("C") == ["listener-missing-tcp-192.168.31.2-445"], str(n.alerts()))
n, _ = run(real, st)
check("... and it clears when it is back", "listener-missing-tcp-192.168.31.2-445" in n.clears())

# ── ephemeral ports ──
shifted = real.replace(":34242", ":51234").replace(":41121", ":52000")
check("ephemeral ports may change between starts", run(shifted)[0].alerts() == [])
low = real + line("udp", "*:5000", "python3") + "\n"
check("a LOW-numbered port is not ephemeral -> tier A", run(low)[0].socks() == ["udp *:5000 (python3)"])
# ── until= (a time-limited exception) with a synthetic allow-list ──
import tempfile as _tf
_f = _tf.NamedTemporaryFile("w", suffix=".conf", delete=False)
_f.write("tcp * 18555 go2rtc until=2026-10-21 temporary known fault, to be removed\n")
_f.close()
inside = lw.load_allowlist(_f.name, today=datetime.date(2026, 10, 20))
n, _ = run("tcp LISTEN 0 4096 *:18555 *:* " + 'users:(("go2rtc",pid=1,fd=3))', entries=inside)
check("until= in the future: the listener is allowed", n.alerts() == [], str(n.alerts()))
n, _ = run("", entries=inside)
check("until= entry whose listener is absent is NOT reported as missing (an exception is not an expectation)", n.alerts() == [], str(n.alerts()))
after = lw.load_allowlist(_f.name, today=datetime.date(2026, 10, 22))
n, _ = run("tcp LISTEN 0 4096 *:18555 *:* " + 'users:(("go2rtc",pid=1,fd=3))', entries=after)
check("after the until= date the same listener is NOT allowed any more -> tier A", n.socks() == ["tcp *:18555 (go2rtc)"], str(n.socks()))
os.unlink(_f.name)


# ── Task 29a: one message per check, and the HA tuya_local LAN scan ──
scan = open(os.path.join(HERE, "ss-tuya-scan.txt")).read()           # captured 2026-10-21 07:51:21, DURING a scan: 6666/6667/7000 + a random-port sender per interface address
n, st = run(scan)
check("a capture taken DURING the HA Tuya scan: NO alert (the sockets are allow-listed)", n.alerts("A") == [] and n.alerts("C") == [], str(n.socks()) + str(n.alerts()))
check("... and the scan capture really contains the scan sockets (6666, 6667, 7000 and the per-bridge senders)", all(p in scan for p in (":6666", ":6667", ":7000", "172.17.0.1:", "172.30.0.1:")))
burst = real + "".join(line("udp", "172.%d.0.1:%d" % (17 + i, 40000 + i), "python3") + "\n" for i in range(9)) + "".join(line("udp", "*:%d" % p, "python3") + "\n" for p in (4001, 4002, 4003, 4004))
no_allow = lw.load_allowlist(os.path.join(HERE, "allowlist.conf"), today=datetime.date(2026, 10, 20))
n, st = run(burst, entries=[e for e in no_allow if not (e["proc"] == "python3" and e["proto"] == "udp" and e["addr"].startswith("172."))])
check("a burst of 13 unlisted sockets -> ONE tier-A message naming all 13", len(n.alerts("A")) == 1 and len(n.socks()) == 13, str((len(n.alerts("A")), len(n.socks()))))
n, st2 = run(burst, st, entries=[e for e in no_allow if not (e["proc"] == "python3" and e["proto"] == "udp" and e["addr"].startswith("172."))])
check("the same burst on the next run: the same single alert (watchnotify dedups it)", n.alerts("A") == st["alerted"] and len(st2["alerted"]) == 1)
n, st3 = run(real, st2)
check("the burst is over: ONE recovery, not 13", len(n.clears()) == 1 and st3["alerted"] == [], str(n.clears()))
n, st4 = run(burst + line("udp", "*:4005", "python3") + "\n", st, entries=[e for e in no_allow if not (e["proc"] == "python3" and e["proto"] == "udp" and e["addr"].startswith("172."))])
check("a 14th socket joins: a new alert for the new set and the old one is cleared (still one message each)", len(n.alerts("A")) == 1 and len(n.socks()) == 14 and len(n.clears()) == 1)
long_burst = real + "".join(line("udp", "*:%d" % p, "python3") + "\n" for p in range(4100, 4140))
n, _ = run(long_burst)
check("a very long burst: still one message, the list is capped at 25 lines with a count", len(n.alerts("A")) == 1 and "и ещё 15" in list(n.texts.values())[0], "")
# ── allow-list file errors ──
import tempfile
bad = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
bad.write("tcp 1.2.3.4 80\n")
bad.close()
try:
    lw.load_allowlist(bad.name)
    check("a malformed allow-list line is an error", False)
except ValueError:
    check("a malformed allow-list line is an error", True)
os.unlink(bad.name)

print("RESULT:", "all passed" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
