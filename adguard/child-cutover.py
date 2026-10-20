#!/usr/bin/env python3
"""The cutover of the children's DNS policy from the old text files to the single source (Task 29, Phase H; ADR 0008).

  child-cutover.py status                 mode, shadow counters, snapshot, last job run, where the rollback bundle would go
  child-cutover.py check                  every pre-flight condition, changes NOTHING (exit 1 if any fails)
  child-cutover.py cutover --go-from-ivan  THE switch. Only after Ivan said go IN CHAT. Refuses inside 15 min of a window edge (16:00, 19:00, 00:00).
  child-cutover.py rollback [BUNDLE]      back to the files: the latest pre-cutover bundle unless one is named

`cutover`: pre-flight -> rollback bundle (the old files + a git tag) -> final shadow pass -> mark the service `child_cutover` -> source.conf = snapshot -> one job run ->
VERIFY: the rules block in AdGuard is byte for byte the one of before the switch, and the checks of each child device (check_host) give the same answers -> stop
child-ui -> enable the list generator. ANY failure after the point of no return runs the rollback by itself and says so.
Nothing here ever lets DNS fail: it only changes WHICH source the (unchanged, once-a-minute) job renders from, and proves the output is identical before it stays.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import childpolicy  # noqa: E402
import childsnapshot  # noqa: E402
import overrides  # noqa: E402

POLICY = HERE / "child-policy"
BUNDLES = Path(os.environ.get("CUTOVER_BUNDLES", "/var/lib/child-override"))
SERVICE = os.environ.get("SCHEDCTL_URL", "http://127.0.0.1:16444")
TOKEN_FILE = os.environ.get("SCHEDCTL_TOKEN_FILE", os.path.expanduser("~/.config/schedctl/token"))
PARITY_STATE = Path(os.environ.get("PARITY_STATE", "/var/lib/adguard-parity/state.json"))
REPO = os.environ.get("COMPOSE_REPO", "/srv/compose")
EDGES = [(16, 0), (19, 0), (0, 0)]
EDGE_GUARD_MIN = 15
FILES = ("allow-always.txt", "allow-weekdays.txt", "allow-extra.txt", "deny-extra.txt", "clients.conf", "source.conf")
UNITS_OFF = ("child-ui.service",)
UNITS_ON = ("adguard-child-lists.path",)
CHECK_NAMES = ("youtube.com", "www.youtube.com", "roblox.com", "maps.google.com", "api.telegram.org", "t.me", "never-allowed.example.net", "www.google.com")


# ── plumbing the tests replace ────────────────────────────────────────────────────────────────────────────────────
def svc(method, path, body=None):
    try:
        tok = Path(TOKEN_FILE).read_text().strip()
    except OSError:
        return None, {"error": {"code": "no_token", "message": "cannot read the schedule-service token"}}
    req = urllib.request.Request(SERVICE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError):
        return None, {"error": {"code": "unreachable", "message": "the schedule service did not answer"}}


def sysctl(*args):
    """systemctl through sudo; returns the exit code (0 also for a unit that does not exist, which is not ours to judge)."""
    return subprocess.run(["sudo", "-n", "systemctl", *args], capture_output=True, text=True, timeout=60).returncode


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=60)


def generate_lists():
    return subprocess.run([sys.executable, str(HERE / "generate-child-lists.py")], capture_output=True, text=True, timeout=120).returncode


def say(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ── pre-flight ───────────────────────────────────────────────────────────────────────────────────────────────────
def minutes_to_nearest_edge(now):
    best = None
    for h, m in EDGES:
        for d in (-1, 0, 1):
            edge = now.replace(hour=h, minute=m, second=0, microsecond=0) + datetime.timedelta(days=d)
            gap = abs((now - edge).total_seconds()) / 60.0
            best = gap if best is None else min(best, gap)
    return best


def shadow_state():
    try:
        return json.loads(childsnapshot.SHADOW.read_text())
    except (OSError, ValueError):
        return None


def preflight(now, min_hours=20.0, min_checks=600, api=None):
    """Every condition that must hold before the switch. Returns a list of problems (empty = go)."""
    probs = []
    if childsnapshot.mode() != "files":
        probs.append("source.conf is not `files` (already cut over, or unreadable)")
    sh = shadow_state()
    if not sh:
        probs.append("no shadow state: the job has not compared the two sources yet")
    else:
        if sh["mismatches"] != 0:
            probs.append("the shadow comparison recorded %d mismatch(es); last: %s" % (sh["mismatches"], json.dumps(sh.get("last_mismatch"))[:300]))
        if sh["checked"] < min_checks:
            probs.append("only %d shadow comparisons so far (need %d)" % (sh["checked"], min_checks))
        try:
            span = (datetime.datetime.fromisoformat(sh["last_checked"]) - datetime.datetime.fromisoformat(sh["first_checked"])).total_seconds() / 3600
            if span < min_hours:
                probs.append("the shadow period is %.1f h (need %.0f h, which includes real window edges)" % (span, min_hours))
            if (now - datetime.datetime.fromisoformat(sh["last_checked"])).total_seconds() > 180:
                probs.append("the last shadow comparison is older than 3 min: the job is not running")
        except (KeyError, ValueError):
            probs.append("the shadow state is unreadable")
    loaded = childsnapshot.load()
    if loaded["snap"] is None or loaded["source"] != "live":
        probs.append("no usable LIVE snapshot (%s)" % loaded["live_problem"])
    try:
        st = json.loads(Path(childsnapshot.STATUS).read_text())
        age = (now - datetime.datetime.fromisoformat(st["at"])).total_seconds()
        if not st.get("ok") or st.get("source") != "files" or age > 180:
            probs.append("the job's last run is not a healthy `files` run (ok=%s source=%s age=%ds)" % (st.get("ok"), st.get("source"), age))
    except (OSError, ValueError, KeyError):
        probs.append("the job has written no apply status")
    try:
        (api or childpolicy.Api()).call("GET", "/status")
    except Exception as e:                                                      # noqa: BLE001
        probs.append("AdGuard does not answer (%s)" % e.__class__.__name__)
    s, h = svc("GET", "/v1/health")
    if s != 200:
        probs.append("the schedule service does not answer")
    else:
        if not h.get("in_sync"):
            probs.append("the schedule service reports HA not in sync")
        if (h.get("child_policy") or {}).get("cutover"):
            probs.append("the service already has child_cutover set")
    try:
        ps = json.loads(PARITY_STATE.read_text())
        if ps.get("streak"):
            probs.append("the parity checker reports a mismatch: %s" % sorted(ps["streak"]))
        if time.time() - PARITY_STATE.stat().st_mtime > 900:
            probs.append("the parity checker has not run for more than 15 min")
    except (OSError, ValueError):
        probs.append("the parity state is unreadable")
    gap = minutes_to_nearest_edge(now)
    if gap < EDGE_GUARD_MIN:
        probs.append("%.0f min from a window edge (16:00, 19:00, 00:00); the switch needs at least %d" % (gap, EDGE_GUARD_MIN))
    if loaded["snap"] is not None:
        snap_ex = {(d["ip"], x["target"]) for d in loaded["snap"]["devices"] for x in d.get("exceptions") or []}
        for e in overrides.active(now):
            if (e["client"], e["target"]) not in snap_ex:
                probs.append("an exception of the OLD file is not in the model: %s %s until %s (close it, or wait)" % (e["client"], e["target"], e["expires"]))
    return probs


# ── the pieces of the switch ───────────────────────────────────────────────────────────────────────────────────
def applied_hash():
    try:
        return json.loads(Path(childsnapshot.STATUS).read_text()).get("rules_hash")
    except (OSError, ValueError):
        return None


def child_answers(api):
    """check_host for every child device and a fixed set of names: the policy as AdGuard itself evaluates it."""
    out = {}
    for ip, _name, _mac in childpolicy.load_clients() if childsnapshot.mode() == "files" else childsnapshot.clients_from(childsnapshot.load()["snap"]):
        for n in CHECK_NAMES:
            r = api.call("GET", "/filtering/check_host?name=%s&client=%s" % (n, ip))
            out["%s %s" % (ip, n)] = (r.get("reason"), r.get("rule"))
    return out


def make_bundle(now):
    BUNDLES.mkdir(parents=True, exist_ok=True)
    d = BUNDLES / ("pre-cutover-" + now.strftime("%Y%m%d-%H%M%S"))
    d.mkdir()
    for f in FILES:
        if (POLICY / f).exists():
            shutil.copy2(POLICY / f, d / f)
    if overrides.STATE.exists():
        shutil.copy2(overrides.STATE, d / "overrides.json")
    for src, name in ((Path(childsnapshot.STATUS), "apply-status.json"), (childsnapshot.SHADOW, "shadow.json")):
        if src.exists():
            shutil.copy2(src, d / name)
    head = git("rev-parse", "HEAD").stdout.strip()
    (d / "README.txt").write_text("Pre-cutover bundle %s\nrepo HEAD %s\nrollback: child-cutover.py rollback %s\n" % (now.isoformat(timespec="seconds"), head, d))
    git("tag", "-f", "pre-task29-cutover")
    return d


def run_job(now=None):
    return childpolicy.run(now)


def cutover(now, go, min_hours=20.0, min_checks=600):
    if not go:
        say("refused: --go-from-ivan is required (Ivan's explicit go in chat)")
        return 2
    probs = preflight(now, min_hours, min_checks)
    if probs:
        say("PRE-FLIGHT FAILED - nothing was changed:")
        for p in probs:
            say("  - " + p)
        return 1
    api = childpolicy.Api()
    bundle = make_bundle(now)
    say("rollback bundle: %s (and git tag pre-task29-cutover)" % bundle)
    run_job(now)                                                                  # a fresh files-mode pass: its block and its shadow comparison are the reference
    before_hash, before = applied_hash(), child_answers(api)
    sh = shadow_state()
    if sh["mismatches"] != 0:
        say("the final shadow pass found a mismatch: %s - nothing was changed" % json.dumps(sh["last_mismatch"])[:300])
        return 1
    say("reference: block hash %s, %d child checks, shadow %d checks 0 mismatches" % (before_hash, len(before), sh["checked"]))
    # ── point of no return: from here a failure rolls back by itself ──
    try:
        s, b = svc("PUT", "/v1/child/cutover", {"done": True, "note": "Task 29 cutover %s" % now.isoformat(timespec="seconds")})
        if s != 200:
            raise RuntimeError("the service refused the cutover flag: %s %s" % (s, b))
        childsnapshot.MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(childsnapshot.MODE_FILE) + ".tmp")
        tmp.write_text("# set by child-cutover.py on %s; `files` = the old text files + overrides.json, `snapshot` = the schedule service (Task 29)\nsnapshot\n" % now.isoformat(timespec="seconds"))
        os.replace(tmp, childsnapshot.MODE_FILE)
        run_job(now)
        st = json.loads(Path(childsnapshot.STATUS).read_text())
        if not st.get("ok") or st.get("source") != "snapshot":
            raise RuntimeError("the first snapshot run was not healthy: %s" % st)
        if st["rules_hash"] != before_hash:
            raise RuntimeError("the rules block CHANGED at the switch: %s -> %s" % (before_hash, st["rules_hash"]))
        after = child_answers(api)
        if after != before:
            diff = [k for k in before if before[k] != after.get(k)]
            raise RuntimeError("check_host answers changed for: %s" % diff[:5])
        say("verified: the block is byte for byte the same (%s) and all %d child checks give the same answers" % (st["rules_hash"], len(after)))
    except Exception as e:                                                         # noqa: BLE001 - whatever it was, the files come back
        say("CUTOVER FAILED AFTER THE SWITCH: %s -> rolling back" % e)
        rc = rollback(bundle, now)
        say("rollback %s" % ("done" if rc == 0 else "FAILED: look at it by hand, the bundle is %s" % bundle))
        return 1
    for u in UNITS_OFF:
        sysctl("disable", "--now", u)
    for u in UNITS_ON:
        sysctl("enable", "--now", u)
    generate_lists()
    say("child-ui stopped and disabled (files kept), the list generator is on")
    say("CUTOVER DONE. rollback: child-cutover.py rollback %s" % bundle)
    return 0


def rollback(bundle=None, now=None):
    now = now or datetime.datetime.now(childpolicy.TZ)
    if bundle is None:
        found = sorted(BUNDLES.glob("pre-cutover-*"))
        if not found:
            say("no pre-cutover bundle found")
            return 1
        bundle = found[-1]
    bundle = Path(bundle)
    for f in FILES:
        if (bundle / f).exists():
            shutil.copy2(bundle / f, POLICY / f)
    if (bundle / "overrides.json").exists():
        shutil.copy2(bundle / "overrides.json", overrides.STATE)
    s, b = svc("PUT", "/v1/child/cutover", {"done": False, "note": "rollback %s" % now.isoformat(timespec="seconds")})
    if s != 200:
        say("WARNING: the service did not clear child_cutover (%s): clear it with `schedctl` or the API" % s)
    for u in UNITS_ON:
        sysctl("disable", "--now", u)
    for u in UNITS_OFF:
        sysctl("enable", "--now", u)
    try:
        run_job(now)
        st = json.loads(Path(childsnapshot.STATUS).read_text())
    except Exception as e:                                                         # noqa: BLE001
        say("the job failed after the rollback: %s" % e)
        return 1
    ok = st.get("ok") and st.get("source") == "files"
    say("after the rollback: source=%s ok=%s hash=%s" % (st.get("source"), st.get("ok"), st.get("rules_hash")))
    return 0 if ok else 1


def status():
    sh = shadow_state()
    print("mode:", childsnapshot.mode())
    print("shadow:", json.dumps(sh) if sh else "none")
    try:
        print("last job run:", Path(childsnapshot.STATUS).read_text().replace("\n", " "))
    except OSError:
        print("last job run: none")
    found = sorted(BUNDLES.glob("pre-cutover-*"))
    print("bundles:", [str(f) for f in found] or "none")
    print("minutes to the nearest edge: %.0f" % minutes_to_nearest_edge(datetime.datetime.now(childpolicy.TZ)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("check")
    c.add_argument("--min-hours", type=float, default=20.0)
    c.add_argument("--min-checks", type=int, default=600)
    g = sub.add_parser("cutover")
    g.add_argument("--go-from-ivan", action="store_true")
    g.add_argument("--min-hours", type=float, default=20.0)
    g.add_argument("--min-checks", type=int, default=600)
    r = sub.add_parser("rollback")
    r.add_argument("bundle", nargs="?")
    a = ap.parse_args(argv)
    now = datetime.datetime.now(childpolicy.TZ)
    if a.cmd == "status":
        return status()
    if a.cmd == "check":
        probs = preflight(now, a.min_hours, a.min_checks)
        for p in probs:
            say("  - " + p)
        say("pre-flight: %s" % ("ALL GOOD" if not probs else "%d problem(s)" % len(probs)))
        return 1 if probs else 0
    if a.cmd == "cutover":
        return cutover(now, a.go_from_ivan, a.min_hours, a.min_checks)
    return rollback(a.bundle, now)


if __name__ == "__main__":
    sys.exit(main())
