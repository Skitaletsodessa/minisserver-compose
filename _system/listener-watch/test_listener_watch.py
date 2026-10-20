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

    def clear(self, key, text, **kw):
        self.calls.append(("clear", key))

    def alerts(self, tier=None):
        return sorted(k for a, k, *t in self.calls if a == "alert" and (tier is None or t[0] == tier))

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
real = open(os.path.join(HERE, "ss-sample.txt")).read()
n, st = run(real)
check("the real ss output of today: NO alert at all", n.alerts("A") == [], str(n.alerts("A")))
check("the real ss output of today: nothing expected is missing", n.alerts("C") == [], str(n.alerts("C")))
check("the real ss output: go2rtc on 18555 is allowed only because of the until= date",
      any(e["port"] == "18555" and e["until"] for e in ENTRIES) and st["checked"] > 40, str(st["checked"]))

# ── a new listener ──
new = real + line("tcp", "0.0.0.0:8888", "python3") + "\n"
n, st = run(new)
check("a NEW wildcard listener -> tier A, one alert for it", n.alerts("A") == ["listener-new-tcp-*-8888"], str(n.alerts()))
n, st2 = run(new, st)
check("the same listener on the next run: alert is renewed (watchnotify dedups), state keeps it", st2["alerted"] == ["listener-new-tcp-*-8888"])
n, st3 = run(real, st2)
check("listener gone: cleared (recovery)", "listener-new-tcp-*-8888" in n.clears() and st3["alerted"] == [])

new = real + line("tcp", "192.168.31.2:9999", "node") + "\n"
check("a new LAN-address listener -> tier A", run(new)[0].alerts("A") == ["listener-new-tcp-192.168.31.2-9999"])
new = real + line("udp", "100.76.61.8:7777", "python3") + "\n"
check("a new tailnet-address UDP listener -> tier A", run(new)[0].alerts("A") == ["listener-new-udp-100.76.61.8-7777"])

# ── wrong owner on a known port ──
swapped = real.replace('("smbd",pid=1182,fd=29)', '("evil",pid=1,fd=29)')
n, _ = run(swapped)
check("a known port taken by a DIFFERENT process -> tier A", "listener-new-tcp-192.168.31.2-445" in n.alerts("A"), str(n.alerts()))
check("... and the expected smbd listener is reported missing (quiet)", "listener-missing-tcp-192.168.31.2-445" in n.alerts("C"))

# ── an expected listener disappears ──
gone = "\n".join(l for l in real.splitlines() if "192.168.31.2:445" not in l)
n, st = run(gone)
check("expected listener missing -> tier C only, no tier A", n.alerts("A") == [] and n.alerts("C") == ["listener-missing-tcp-192.168.31.2-445"], str(n.alerts()))
n, _ = run(real, st)
check("... and it clears when it is back", "listener-missing-tcp-192.168.31.2-445" in n.clears())

# ── ephemeral ports ──
shifted = real.replace(":59885", ":51234").replace(":40687", ":52000")
check("ephemeral ports may change between starts", run(shifted)[0].alerts() == [])
low = real + line("udp", "*:5000", "python3") + "\n"
check("a LOW-numbered port is not 'ephemeral' -> tier A", run(low)[0].alerts("A") == ["listener-new-udp-*-5000"])

# ── until= ──
later = lw.load_allowlist(os.path.join(HERE, "allowlist.conf"), today=datetime.date(2026, 10, 22))
n, _ = run(real, entries=later)
check("after the until= date go2rtc is NOT allowed any more -> tier A", n.alerts("A") == ["listener-new-tcp-*-18555"], str(n.alerts()))
check("the expired entry is not reported as 'missing' either", n.alerts("C") == [])
no_go2rtc = "\n".join(l for l in real.splitlines() if ":18555" not in l)
check("go2rtc removed: before the date nothing is reported missing", run(no_go2rtc)[0].alerts() == [])

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
