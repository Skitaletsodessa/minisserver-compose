#!/usr/bin/env python3
"""Live proofs of the children's DNS policy on the single source (Task 29a item 6.4), on a SCRATCH child device with a synthetic IP nobody uses (192.168.31.199).
Run on the server. The three real children are only READ (their rules lines are compared byte for byte before and after). Parental calls go through the gateway as the
synthetic parents (tokens in ~/.config/gw-probe, never printed); the scratch device is created and removed through the schedule service (admin CLI)."""
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway/tools")
import live_proof as L  # noqa: E402

IP, NAME, CID = "192.168.31.199", "scratch-proof", "child_scratch_proof"
REAL = ["192.168.31.60", "192.168.31.61", "192.168.31.30"]
BASE = "/v1/parental/children/" + CID
SCHEDCTL = "/srv/compose/portal/schedules/schedctl"
FAILS = []


def stamp(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def check(name, cond, detail=""):
    stamp("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def agh(path, body=None):
    pw = open("/home/skit/adguard-admin-password.txt").read().strip()
    req = urllib.request.Request("http://127.0.0.1:3000/control" + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Basic " + base64.b64encode(("admin:" + pw).encode()).decode(), "Content-Type": "application/json"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode()
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw


def replica(path):
    env = dict(l.strip().split("=", 1) for l in open("/srv/compose/adguard-sync/.env") if "=" in l and not l.startswith("#"))
    pw = env["REPLICA1_PASSWORD"].strip("\"'")
    req = urllib.request.Request("https://piserver.tail6bf4d5.ts.net/control" + path, headers={"Authorization": "Basic " + base64.b64encode(("admin:" + pw).encode()).decode()})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def verdict(name, ip=IP):
    r = agh("/filtering/check_host?name=%s&client=%s" % (urllib.parse.quote(name), ip))
    return "blocked" if str(r.get("reason", "")).startswith("Filtered") else "allowed"


def lines_for(ips, rules):
    return [x for x in rules if any(x.endswith("$client=" + ip) for ip in ips)]


def real_lines():
    return lines_for(REAL, agh("/filtering/status").get("user_rules") or [])


def scratch_lines(rules):
    return lines_for([IP], rules)


def wait_for(cond, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:                                  # noqa: BLE001 - a service being restarted answers nothing for a moment
            pass
        time.sleep(step)
    return False


def tok(who):
    return L.tok(who)


def call(who, method, path, body=None, headers=None):
    return L.call(method, path, tok(who), body, headers)


def sched(*a, stdin=None):
    r = subprocess.run([SCHEDCTL, *a], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def etag_and_rules():
    r = L.call("GET", BASE + "/windows", tok("probe-padmin"))
    return r


def put_windows(who, rules, expect=200):
    import urllib.request as u
    req = u.Request(L.BASE + BASE + "/windows", method="GET", headers={"Authorization": "Bearer " + tok(who)})
    with u.urlopen(req, timeout=30) as r:
        etag = r.headers["ETag"]
    return call(who, "PUT", BASE + "/windows", {"rules": rules}, {"If-Match": etag})


def main():
    stamp("=== start: real children's lines recorded; DNS monitor is running separately ===")
    base_real = real_lines()
    stamp("real children lines: %d" % len(base_real))
    # ── add the scratch device (closed: catch-all + always-on only is NOT the default; it starts with the standard windows) ──
    rc, out, err = sched("child", "add", NAME, IP)
    check("scratch device added through the schedule service", rc == 0, err[:200])
    g = {"allowed_from": "08:00", "allowed_to": "23:59", "max_open_minutes_per_day": 240}
    json.dump(g, open("/tmp/scratch-g.json", "w"))
    rc, _o, err = sched("guardrails", CID, "/tmp/scratch-g.json")
    check("scratch guardrails widened (scratch only: the real children keep 08:00-21:00)", rc == 0, err[:200])
    os.remove("/tmp/scratch-g.json")
    t0 = time.time()
    check("the host job applied it: catch-all active for the scratch IP (an ordinary name is blocked)", wait_for(lambda: verdict("never-allowed.example.net") == "blocked", 100))
    stamp("applied after %.0f s" % (time.time() - t0))
    check("always-on host stays allowed (api.telegram.org)", verdict("api.telegram.org") == "allowed")
    check("youtube blocked outside its window", verdict("youtube.com") == "blocked")
    cl = [c for c in agh("/clients").get("clients") or [] if c.get("name") == NAME]
    check("an AdGuard client entry exists for it with the child flags (own settings, Safe Search, YouTube restriction)",
          bool(cl) and cl[0].get("use_global_settings") is False and cl[0].get("safe_search", {}).get("enabled") is True and cl[0].get("safe_search", {}).get("youtube") is True, str(cl)[:200])
    check("the three real children's rules are byte-identical after the add", real_lines() == base_real)
    check("the replica follows: its rules hold the scratch lines", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")), 90), "")
    st, ch = call("probe-padmin", "GET", "/v1/parental/children")
    mine = next((c for c in (ch or {}).get("children", []) if c["id"] == CID), None)
    check("the gateway lists it with status closed and policy_ok", st == 200 and mine is not None and mine["status"]["state"] in ("closed", "restricted") and mine["status"]["policy_ok"], str(mine)[:200])

    # ── rejected edits ──
    st, js = put_windows("probe-padmin", [{"id": "yt", "days": [1, 2, 3, 4, 5, 6, 7], "start": "06:00", "end": "07:30", "opens": ["youtube"]}])
    check("a window before 08:00 is REJECTED by the guardrail (422, reasons)", st == 422 and js["error"]["code"] == "validation_failed", str((st, js))[:200])
    st, js = put_windows("probe-phouse", [{"id": "yt", "days": [1], "start": "10:00", "end": "11:00", "opens": ["telegram"]}])
    check("an invariant target (telegram) cannot be opened as a window (400)", st == 400, str((st, js))[:160])
    check("nothing of the rejected edits reached AdGuard", verdict("youtube.com") == "blocked")

    # ── window edge on the real clock, with the service and the gateway STOPPED across it ──
    now = dt.datetime.now().astimezone()
    start = (now + dt.timedelta(minutes=5)).replace(second=0, microsecond=0)
    end = start + dt.timedelta(minutes=4)
    rules = [{"id": "yt", "days": [1, 2, 3, 4, 5, 6, 7], "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "opens": ["youtube", "roblox"]}]
    st, pv = call("probe-phouse", "POST", BASE + "/windows/preview", {"rules": rules})
    check("preview: valid, shows what opens next", st == 200 and pv["valid"] and pv["open_next_7_days"], str(pv)[:160])
    st, js = put_windows("probe-phouse", rules)
    check("the window is saved by the household parent (200)", st == 200, str(js)[:160])
    stamp("window %s - %s (edge at %s)" % (start.strftime("%H:%M"), end.strftime("%H:%M"), start.strftime("%H:%M:%S")))
    check("the edit reached AdGuard within the minute: nothing open yet, rules block for the scratch IP unchanged in effect", verdict("youtube.com") == "blocked")
    check("... and the replica holds the same block", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")), 90))
    # stop the service and the gateway 90 s before the edge
    wait_for(lambda: time.time() >= start.timestamp() - 90, 600, 2)
    subprocess.run(["docker", "stop", "portal-gateway", "portal-schedules"], capture_output=True, timeout=120)
    stamp("portal-gateway and portal-schedules STOPPED (ports answer nothing: %s)" % (subprocess.run("ss -tln | grep -cE ':(16444|17444) '", shell=True, capture_output=True, text=True).stdout.strip(),))
    opened = None
    end_ts = start.timestamp() + 80
    while time.time() < end_ts:
        if opened is None and verdict("youtube.com") == "allowed":
            opened = time.time()
            stamp("youtube.com ALLOWED for the scratch IP at %s (edge %s): +%.0f s after the edge" % (time.strftime("%H:%M:%S"), start.strftime("%H:%M:%S"), opened - start.timestamp()))
        time.sleep(2)
    check("the window OPENED on time with the service and the gateway stopped (within 70 s of the edge: the job runs once a minute)", opened is not None and opened - start.timestamp() < 70)
    check("roblox.com allowed too while open; telegram still allowed", verdict("roblox.com") == "allowed" and verdict("api.telegram.org") == "allowed")
    check("the replica opened it as well (rules equal)", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")), 90))
    check("the real children's block did not move", real_lines() == base_real)
    closed = None
    wait_end = end.timestamp() + 80
    while time.time() < wait_end:
        if time.time() > end.timestamp() - 5 and closed is None and verdict("youtube.com") == "blocked":
            closed = time.time()
            stamp("youtube.com BLOCKED again at %s (end %s): +%.0f s after the end" % (time.strftime("%H:%M:%S"), end.strftime("%H:%M:%S"), closed - end.timestamp()))
        time.sleep(2)
    check("the window CLOSED on time, still with the service and the gateway stopped", closed is not None and closed - end.timestamp() < 70)
    subprocess.run(["docker", "start", "portal-schedules", "portal-gateway"], capture_output=True, timeout=120)
    ok = wait_for(lambda: L.call("GET", "/v1/health")[0] == 200 and urllib.request.urlopen("http://127.0.0.1:16444/v1/health", timeout=3).status == 200, 90)
    r = subprocess.run(["sh", "/srv/compose/portal/gateway/check-bind.sh"], capture_output=True, text=True)
    check("both services back, bind check OK", ok and r.returncode == 0, r.stdout[:200])

    # ── exceptions ──
    time.sleep(5)
    st, x1 = call("probe-padmin", "POST", BASE + "/exceptions", {"target": "youtube", "minutes": 2})
    check("exception granted (201): youtube 2 min", st == 201, str(x1)[:160])
    st, d = call("probe-phouse", "GET", BASE)
    check("shown in the status (open_services has youtube, one exception)", st == 200 and "youtube" in d["status"]["open_services"] and len(d["status"]["exceptions"]) == 1, str(d.get("status"))[:200])
    g0 = time.time()
    check("it reaches AdGuard (youtube allowed for the scratch IP within a minute)", wait_for(lambda: verdict("youtube.com") == "allowed", 75, 2))
    stamp("granted -> effective after %.0f s" % (time.time() - g0))
    check("... and the replica follows", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")), 90))
    exp = dt.datetime.fromisoformat(x1["expires_at"]).timestamp()
    wait_for(lambda: verdict("youtube.com") == "blocked", max(5, exp - time.time() + 90), 3)
    stamp("youtube blocked again at +%.0f s after the expiry (by itself)" % (time.time() - exp))
    check("the exception EXPIRED BY ITSELF (blocked again within ~70 s of its end)", verdict("youtube.com") == "blocked" and time.time() - exp < 100)
    st, d = call("probe-padmin", "GET", BASE)
    check("status shows no exception any more", st == 200 and not d["status"]["exceptions"])
    st, x2 = call("probe-phouse", "POST", BASE + "/exceptions", {"target": "roblox", "minutes": 30})
    check("a second exception (roblox 30 min) granted by the household parent", st == 201)
    check("it takes effect", wait_for(lambda: verdict("roblox.com") == "allowed", 75, 2))
    st, _x = call("probe-padmin", "DELETE", BASE + "/exceptions/" + x2["id"])
    check("cancelled early (204)", st == 204)
    c0 = time.time()
    check("blocked again within a minute of the cancel", wait_for(lambda: verdict("roblox.com") == "blocked", 75, 2))
    stamp("cancel -> blocked after %.0f s" % (time.time() - c0))
    st, x3 = call("probe-padmin", "POST", BASE + "/exceptions", {"target": "all", "until_end_of_day": True})
    check("'everything' until the end of day is accepted (today's behaviour: 23:59)", st == 201 and x3["expires_at"][11:16] == "23:59", str(x3)[:160])
    check("everything open: a random name is allowed", wait_for(lambda: verdict("never-allowed.example.net") == "allowed", 75, 2))
    call("probe-padmin", "DELETE", BASE + "/exceptions/" + x3["id"])
    check("cancelled: the catch-all is back", wait_for(lambda: verdict("never-allowed.example.net") == "blocked", 75, 2))

    # ── per-child domains with undo ──
    st, l1 = call("probe-phouse", "POST", BASE + "/domains", {"kind": "allow", "domain": "proof-allow.example.org"})
    check("allow-always added", st == 200 and l1["allow"] == ["proof-allow.example.org"])
    check("allowed for the scratch IP only", wait_for(lambda: verdict("proof-allow.example.org") == "allowed", 75, 2) and verdict("proof-allow.example.org", REAL[0]) == "blocked")
    st, l2 = call("probe-phouse", "POST", BASE + "/domains", {"kind": "deny", "domain": "proof-allow.example.org"})
    check("block-forever for the same name MOVES it (deny beats allow)", st == 200 and l2 == {"allow": [], "deny": ["proof-allow.example.org"]})
    check("blocked now", wait_for(lambda: verdict("proof-allow.example.org") == "blocked", 75, 2))
    st, l3 = call("probe-phouse", "POST", BASE + "/domains/undo")
    check("UNDO restores the allow", st == 200 and l3 == {"allow": ["proof-allow.example.org"], "deny": []})
    check("allowed again", wait_for(lambda: verdict("proof-allow.example.org") == "allowed", 75, 2))
    st, l4 = call("probe-phouse", "POST", BASE + "/domains/undo")
    check("a second UNDO removes it again", st == 200 and l4 == {"allow": [], "deny": []})
    check("blocked again", wait_for(lambda: verdict("proof-allow.example.org") == "blocked", 75, 2))
    st, js = call("probe-padmin", "POST", BASE + "/domains", {"kind": "deny", "domain": "api.telegram.org"})
    check("denying an always-on host is REJECTED (invariant, 422)", st == 422, str(js)[:160])
    check("the replica rules equal the origin's for the scratch IP at the end of the changes", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")), 90))

    # ── health, then remove the scratch device and leave nothing behind ──
    st, h = call("probe-padmin", "GET", "/v1/parental/health")
    check("gateway health: policy in sync", st == 200 and h["policy"]["in_sync"], str(h.get("policy")))
    rc, _o, err = sched("child", "remove", CID)
    check("scratch device removed through the schedule service", rc == 0, err[:200])
    check("the IP is an ordinary client again (youtube.com allowed, no catch-all)", wait_for(lambda: verdict("youtube.com") == "allowed" and verdict("never-allowed.example.net") == "allowed", 100))
    check("the real children's rules are byte-identical to before the whole run", real_lines() == base_real)
    left = [c for c in agh("/clients").get("clients") or [] if c.get("name") == NAME]
    stamp("AdGuard client entry left behind by the removal (known gap, task 29b cleans it): %d" % len(left))
    if left:
        agh("/clients/delete", {"name": NAME})
        check("client entry deleted on the origin", not [c for c in agh("/clients").get("clients") or [] if c.get("name") == NAME])
        check("... and the replica follows (client gone there too)", wait_for(lambda: not [c for c in replica("/clients").get("clients") or [] if c.get("name") == NAME], 120))
    check("no scratch line left in the rules of origin and replica", not scratch_lines(agh("/filtering/status").get("user_rules")) and wait_for(lambda: not scratch_lines(replica("/filtering/status").get("user_rules") or []), 90))
    stamp("=== %d check(s) failed: %s ===" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
