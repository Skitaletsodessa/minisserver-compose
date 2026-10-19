#!/usr/bin/env python3
"""Tests for the watchnotify severity tiers (Task 26). Nothing is sent: the Telegram sender is replaced.
  python3 /srv/compose/_system/lib/test_watchnotify_tiers.py [OLD_GITREV]
OLD_GITREV (default 8f34b2a~1) is the watchnotify from before the tiers; its output must equal the new module's default (tier A)."""
import importlib.util, os, shutil, subprocess, sys, tempfile, time

OLD_REV = sys.argv[1] if len(sys.argv) > 1 else "8f34b2a~1"
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

def harness(m):
    state = tempfile.mkdtemp(prefix="wn-test-")
    clock = [1_000_000.0]
    sent = []
    class FT:
        def __getattr__(self, n): return getattr(time, n)
        def time(self): return clock[0]
    m.time, m.STATE_DIR, m.OUTBOX, m.TAG = FT(), state, os.path.join(state, "outbox.jsonl"), ""
    m.REMIND = 600
    m._send_raw = lambda msg, attempts=3: (sent.append(msg) or True)
    return state, clock, sent

# old module from git
oldsrc = subprocess.run(["git", "-C", "/srv/compose", "show", f"{OLD_REV}:_system/lib/watchnotify.py"], capture_output=True, text=True, check=True).stdout
oldpath = os.path.join(tempfile.mkdtemp(), "wn_old.py"); open(oldpath, "w").write(oldsrc)

def scenario_default(m):
    state, clock, sent = harness(m)
    steps = [("alert", 0), ("alert", 100), ("alert", 650), ("alert", 700), ("alert", 1300), ("clear", 1400), ("clear", 1500), ("alert", 1600), ("clear", 1700)]
    for op, t in steps:
        clock[0] = 1_000_000.0 + t
        if op == "alert": m.alert("k-default", "disk is full")
        else: m.clear("k-default", "disk ok again")
    shutil.rmtree(state, ignore_errors=True)
    return sent

old, new = load(oldpath, "wn_old"), load(os.path.join(HERE, "watchnotify.py"), "wn_new")
a, b = scenario_default(old), scenario_default(new)
check("default (tier A) message stream is identical to the pre-tier module", a == b, f"old={a} new={b}")
check("default stream has the expected shape: first, 2 reminders, recovery, first, recovery", len(b) == 6, str(len(b)))

# the way an existing watcher calls it: alert(key, text) / clear(key, text) / send(text) / active()
state, clock, sent = harness(new)
new.alert("adguard-watch", "AGH down"); clock[0] += 700; new.alert("adguard-watch", "AGH down")
check("existing watcher call style: reminder after the interval", len(sent) == 2 and "REMINDER" in sent[1], str(sent))
clock[0] += 10; new.clear("adguard-watch", "AGH up")
check("existing watcher call style: recovery message with duration", len(sent) == 3 and "outage lasted" in sent[2], str(sent))
check("active() keeps its 3-tuple shape", new.active() == [], str(new.active()))
new.alert("x", "x"); check("active() tuple shape (key, since, delivered)", len(new.active()[0]) == 3)
shutil.rmtree(state, ignore_errors=True)

# tier B
state, clock, sent = harness(new)
new.alert("b-key", "stuck", tier="B")
check("tier B: one message at the first call", len(sent) == 1 and sent[0] == "stuck", str(sent))
clock[0] += 5000; new.alert("b-key", "stuck", tier="B"); clock[0] += 5000; new.alert("b-key", "stuck", tier="B")
check("tier B: no reminder, ever", len(sent) == 1, str(sent))
d = [x for x in new.active_detail() if x["key"] == "b-key"]
check("tier B: stays in the active list, not quiet", len(d) == 1 and not d[0]["quiet"] and d[0]["tier"] == "B", str(d))
new.clear("b-key", "recovered")
check("tier B: clear sends nothing and removes the alert", len(sent) == 1 and not [x for x in new.active_detail() if x["key"] == "b-key"], str(sent))
shutil.rmtree(state, ignore_errors=True)

# tier C
state, clock, sent = harness(new)
new.alert("c-key", "foreign change at 06:02", tier="C"); clock[0] += 5000; new.alert("c-key", "foreign change at 06:02", tier="C")
check("tier C: nothing is sent", sent == [], str(sent))
d = [x for x in new.active_detail() if x["key"] == "c-key"]
check("tier C: listed as quiet with its text", len(d) == 1 and d[0]["quiet"] and d[0]["text"] == "foreign change at 06:02", str(d))
new.clear("c-key", "gone")
check("tier C: clears silently", sent == [] and not new.active_detail(), str(sent))
shutil.rmtree(state, ignore_errors=True)

# overrides and the CLI flag
state, clock, sent = harness(new)
new.alert("o-key", "x", repeat=100, recovery=False)
clock[0] += 150; new.alert("o-key", "x", repeat=100, recovery=False); new.clear("o-key", "y")
check("explicit repeat/recovery overrides work", len(sent) == 2 and "REMINDER" in sent[1], str(sent))
shutil.rmtree(state, ignore_errors=True)

print("\nRESULT:", "all passed" if not FAILS else "FAILED: " + ", ".join(FAILS))
sys.exit(1 if FAILS else 0)
