#!/usr/bin/env python3
"""Live proofs of adding/removing a child device from the portal (Task 29b), on a SCRATCH device with a synthetic IP nobody uses (192.168.31.198) and a made-up MAC.
Run on the server. The real children are only READ (their rule lines are compared byte for byte before and after). Calls go through the REAL gateway as the synthetic
admin parent `probe-padmin` (token in ~/.config/gw-probe, never printed) and, for the role check, the synthetic non-admin parent `probe-phouse`."""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway/tools")
import live_proof as L  # noqa: E402

IP, MAC, NAME, LABEL = "192.168.31.198", "02:aa:bb:cc:dd:98", "Proofchild", "scratch"
CID = "child_proofchild_scratch"
REAL = ["192.168.31.60", "192.168.31.61", "192.168.31.30"]
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


def client_entry(fn=agh):
    return [c for c in fn("/clients").get("clients") or [] if IP in (c.get("ids") or [])]


def wait_for(cond, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:                                  # noqa: BLE001
            pass
        time.sleep(step)
    return False


def call(who, method, path, body=None, headers=None):
    return L.call(method, path, L.tok(who), body, headers)


def sched(*a):
    r = subprocess.run([SCHEDCTL, *a], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def systemctl(*a):
    return subprocess.run(["sudo", "systemctl", *a], capture_output=True, text=True, timeout=60)


def names(js):
    return [(s["name"], s["ok"]) for s in (js or {}).get("steps", [])]


def main():
    stamp("=== start (DNS monitor runs separately) ===")
    base_real = real_lines()
    base_rules = agh("/filtering/status").get("user_rules")
    base_clients = sorted(c["name"] for c in agh("/clients").get("clients") or [])
    stamp("real children lines: %d; AdGuard clients: %s" % (len(base_real), base_clients))
    check("the scratch IP is a plain client to begin with (nothing blocks it)", verdict("never-allowed.example.net") == "allowed" and not client_entry())

    # ── read-only: candidates and the role matrix ──
    st, c = call("probe-padmin", "GET", "/v1/parental/candidates")
    check("candidates answer 200 for the admin parent and list only unknown LAN devices (no child, no infrastructure)",
          st == 200 and all(x["ip"] not in REAL + ["192.168.31.1", "192.168.31.2", "192.168.31.5"] for x in c["candidates"]), str((st, c))[:200])
    stamp("candidates (real LAN, read-only): %d, e.g. %s" % (len(c["candidates"]), [(x["ip"], x.get("vendor"), x.get("randomised_mac")) for x in c["candidates"][:5]]))
    check("candidates changed nothing (rules and clients identical)", agh("/filtering/status").get("user_rules") == base_rules and sorted(x["name"] for x in agh("/clients")["clients"]) == base_clients)
    st, js = call("probe-phouse", "GET", "/v1/parental/candidates")
    check("a non-admin parent is refused the candidates (403)", st == 403, str((st, js))[:120])
    st, js = call("probe-phouse", "POST", "/v1/parental/children", {"name": NAME, "ip": IP, "template": "closed"})
    check("a non-admin parent cannot add a device (403) and nothing was created", st == 403 and not client_entry() and verdict("never-allowed.example.net") == "allowed", str((st, js))[:120])
    st, js = L.call("POST", "/v1/parental/children", None, {"name": NAME, "ip": IP, "template": "closed"})
    check("no token: 401", st == 401, str(st))

    # ── refusals change nothing ──
    for label, body, codes in (("a real child's address", {"name": "X1", "ip": "192.168.31.60", "template": "closed"}, ("duplicate",)),
                               ("the server's own address", {"name": "X2", "ip": "192.168.31.2", "template": "closed"}, ("infrastructure_ip",)),
                               ("the Pi's address", {"name": "X3", "ip": "192.168.31.5", "template": "closed"}, ("infrastructure_ip",)),
                               ("the router's address", {"name": "X4", "ip": "192.168.31.1", "template": "closed"}, ("infrastructure_ip",)),
                               ("an address outside the LAN", {"name": "X5", "ip": "8.8.8.8", "template": "closed"}, ("not_lan",)),
                               ("a garbage address", {"name": "X6", "ip": "999.1.1.1", "template": "closed"}, ("bad_ip",))):
        st, js = call("probe-padmin", "POST", "/v1/parental/children", body)
        code = (js or {}).get("error", {}).get("code") if isinstance(js, dict) else None
        check("refused: %s (%s)" % (label, code), st in (400, 409, 422) and code in codes, str((st, js))[:160])
    check("the refusals changed nothing", agh("/filtering/status").get("user_rules") == base_rules and sorted(x["name"] for x in agh("/clients")["clients"]) == base_clients)

    # ── the add ──
    t0 = time.time()
    st, js = call("probe-padmin", "POST", "/v1/parental/children", {"name": NAME, "device_label": LABEL, "ip": IP, "mac": MAC, "icon": "tablet", "template": "closed"})
    stamp("add took %.0f s -> %s" % (time.time() - t0, (st, (js or {}).get("result"), names(js))))
    check("the add succeeded: created, every verification step ok", st in (200, 201) and js.get("result") == "created" and all(ok for _n, ok in names(js)) and
          [n for n, _ in names(js)] == ["create", "job", "verify", "rules", "parity"], str(js)[:300])
    cid = (js or {}).get("child_id")
    check("the child id is the transliterated name", cid == CID, str(cid))
    check("an ordinary name is blocked for the new IP, always-on host allowed, youtube blocked (closed, no windows)",
          verdict("never-allowed.example.net") == "blocked" and verdict("api.telegram.org") == "allowed" and verdict("youtube.com") == "blocked")
    cl = client_entry()
    check("AdGuard has a client entry with the child flags (own settings, Safe Search, YouTube restriction)",
          bool(cl) and cl[0].get("use_global_settings") is False and cl[0].get("safe_search", {}).get("enabled") is True and cl[0].get("safe_search", {}).get("youtube") is True, str(cl)[:200])
    check("the three real children's rule lines are byte-identical", real_lines() == base_real)
    check("the replica holds the scratch lines and the client entry", wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")) and bool(client_entry(replica)), 90))
    st, lst = call("probe-padmin", "GET", "/v1/parental/children")
    mine = next((x for x in (lst or {}).get("children", []) if x["id"] == CID), None)
    check("the list shows it, closed, with the chosen icon, policy ok", st == 200 and mine is not None and mine["status"]["state"] in ("closed", "restricted") and mine["status"]["policy_ok"] and mine.get("icon") == "tablet", str(mine)[:240])
    st, ck = call("probe-padmin", "GET", "/v1/parental/children/%s/setup-checklist" % CID)
    ids = [i["id"] for i in (ck or {}).get("items", [])]
    check("the setup checklist has the five items; DHCP-reservation is 'unknown' (the portal cannot see the router), static DNS 'todo' (no queries yet)",
          st == 200 and ids == ["dhcp_reservation", "mac_randomisation", "static_dns", "private_dns_vpn", "bedtime"] and
          {i["id"]: i["status"] for i in ck["items"]}["static_dns"] == "todo", str(ck)[:300])
    st, js2 = call("probe-padmin", "POST", "/v1/parental/children", {"name": NAME, "device_label": LABEL, "ip": IP, "template": "closed"})
    check("adding the same device again is refused (duplicate), nothing doubled", st in (400, 409, 422) and len([c for c in agh("/clients")["clients"] if IP in (c.get("ids") or [])]) == 1, str((st, js2))[:160])

    # ── the job enforces without the portal ──
    subprocess.run(["docker", "stop", "portal-gateway", "portal-schedules"], capture_output=True, timeout=120)
    stamp("portal-gateway and portal-schedules STOPPED; waiting 75 s across at least one job run")
    time.sleep(75)
    check("with the portal stopped the new device is still filtered (catch-all) on the origin and the replica", verdict("never-allowed.example.net") == "blocked" and
          scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")))
    subprocess.run(["docker", "start", "portal-schedules", "portal-gateway"], capture_output=True, timeout=120)
    ok = wait_for(lambda: L.call("GET", "/v1/health")[0] == 200 and urllib.request.urlopen("http://127.0.0.1:16444/v1/health", timeout=3).status == 200, 90)
    r = subprocess.run(["sh", "/srv/compose/portal/gateway/check-bind.sh"], capture_output=True, text=True)
    check("both back, bind check OK", ok and r.returncode == 0, r.stdout[:200])
    time.sleep(3)

    # ── removal ──
    st, js = call("probe-padmin", "DELETE", "/v1/parental/children/" + CID, {"confirm": "wrong"})
    check("removal with a wrong confirmation is refused (422/400, confirm_mismatch) and removes nothing", st in (400, 422) and bool(client_entry()) and verdict("never-allowed.example.net") == "blocked", str((st, js))[:160])
    t0 = time.time()
    st, js = call("probe-padmin", "DELETE", "/v1/parental/children/" + CID, {"confirm": NAME + " " + LABEL})
    stamp("removal took %.0f s -> %s" % (time.time() - t0, (st, (js or {}).get("result"), names(js))))
    check("removal: removed, steps remove/job/client/others/parity all ok", st == 200 and js.get("result") == "removed" and [n for n, _ in names(js)] == ["remove", "job", "client", "others", "parity"] and all(ok for _n, ok in names(js)), str(js)[:300])
    check("the IP is an ordinary client again", verdict("never-allowed.example.net") == "allowed" and verdict("youtube.com") == "allowed")
    check("the AdGuard client entry is gone on the origin ...", not client_entry())
    check("... and on the replica", wait_for(lambda: not client_entry(replica), 120))
    check("no scratch line left on the origin or the replica", not scratch_lines(agh("/filtering/status").get("user_rules")) and wait_for(lambda: not scratch_lines(replica("/filtering/status").get("user_rules") or []), 90))
    check("the real children's lines are byte-identical", real_lines() == base_real)
    st, rm = call("probe-padmin", "GET", "/v1/parental/removed-children")
    rec = next((x for x in (rm or {}).get("removed", []) if x["ip"] == IP), None)
    check("it is listed among the recently removed, restorable for 24 h", st == 200 and rec is not None, str(rm)[:200])

    # ── undo ──
    if rec:
        t0 = time.time()
        st, js = call("probe-padmin", "POST", "/v1/parental/removed-children/%s/restore" % rec["removal_id"])
        stamp("restore took %.0f s -> %s" % (time.time() - t0, (st, (js or {}).get("result"), names(js))))
        check("the undo re-adds and verifies the device (created)", st in (200, 201) and js.get("result") == "created", str(js)[:300])
        check("filtered again: catch-all + client entry (origin) and replica follows", verdict("never-allowed.example.net") == "blocked" and bool(client_entry()) and
              wait_for(lambda: scratch_lines(replica("/filtering/status").get("user_rules") or []) == scratch_lines(agh("/filtering/status").get("user_rules")) and bool(client_entry(replica)), 90))
        check("the real children's lines are still byte-identical", real_lines() == base_real)

    # ── failure path: the host job cannot apply -> the add rolls itself back ──
    # first clean the restored device completely (forget), then stop the job timer and the drift watcher around ONE failing add
    rc, _o, err = sched("child", "remove", CID, "--forget")
    check("scratch device removed and its undo record forgotten (service)", rc == 0, err[:200])
    check("the IP is released by the job", wait_for(lambda: verdict("never-allowed.example.net") == "allowed", 100))
    if client_entry():
        agh("/clients/delete", {"name": NAME + " " + LABEL})
    check("client entry gone on the origin and the replica", wait_for(lambda: not client_entry() and not client_entry(replica), 120))
    stamp("stopping adguard-policy.timer and policy-drift-watch.timer for the failure-path proof (restored in `finally`)")
    systemctl("stop", "adguard-policy.timer")
    systemctl("stop", "policy-drift-watch.timer")
    try:
        t0 = time.time()
        st, js = call("probe-padmin", "POST", "/v1/parental/children", {"name": NAME, "device_label": LABEL, "ip": IP, "mac": MAC, "icon": "tablet", "template": "closed"})
        dt_ = time.time() - t0
        stamp("failing add took %.0f s -> %s" % (dt_, (st, (js or {}).get("result"), (js or {}).get("reason"), names(js))))
        check("with the job stopped the add FAILS at the job step and reports it as reverted", st in (200, 409, 422, 504) and (js or {}).get("result") == "reverted" and ("job", False) in names(js), str(js)[:300])
        check("the failure was reported within the apply timeout (< 200 s)", dt_ < 200)
        rc, out, _e = sched("child", "list")
        check("the entity is gone from the schedule service (rolled back, not left half-added)", CID not in out and "Proofchild" not in out, out[:200])
        check("no AdGuard client entry was left on the origin or the replica", not client_entry() and not client_entry(replica))
        st, rm = call("probe-padmin", "GET", "/v1/parental/removed-children")
        check("a failed add leaves no 'recently removed' record", not [x for x in (rm or {}).get("removed", []) if x["ip"] == IP], str(rm)[:200])
    finally:
        systemctl("start", "adguard-policy.timer")
        systemctl("start", "policy-drift-watch.timer")
        stamp("timers restarted")
    check("after the job is back the IP stays an ordinary client", wait_for(lambda: verdict("never-allowed.example.net") == "allowed", 100))
    check("the real children's lines are byte-identical to before the whole run", real_lines() == base_real)
    final_rules = agh("/filtering/status").get("user_rules")
    check("the origin's rules equal the starting rules", final_rules == base_rules)
    check("AdGuard clients equal the starting set", sorted(c["name"] for c in agh("/clients")["clients"]) == base_clients)
    check("the replica agrees with the origin (rules)", wait_for(lambda: replica("/filtering/status").get("user_rules") == agh("/filtering/status").get("user_rules"), 120))
    st, rm = call("probe-padmin", "GET", "/v1/parental/removed-children")
    check("nothing of this proof is left among the recently removed", st == 200 and not [x for x in rm.get("removed", []) if x["ip"] == IP], str(rm)[:200])
    stamp("=== %d check(s) failed: %s ===" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
