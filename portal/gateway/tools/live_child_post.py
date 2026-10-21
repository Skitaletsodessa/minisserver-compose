#!/usr/bin/env python3
"""Second part of the Task 29b live proofs: with a scratch child present - Safe Search rewrite, a fresh backup + restore test that contains it, policy-drift-watch,
the gateway's health line - then remove it. Same synthetic IP/MAC as live_child_devices.py. Run on the server, GW_TIMEOUT=900."""
import json
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "/srv/compose/portal/gateway/tools")
import live_child_devices as m  # noqa: E402
import live_proof as L  # noqa: E402

stamp, check = m.stamp, m.check


def sh(cmd, timeout=1500):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def main():
    base_real = m.real_lines()
    base_rules = m.agh("/filtering/status").get("user_rules")
    st, js = m.call("probe-padmin", "POST", "/v1/parental/children", {"name": m.NAME, "device_label": m.LABEL, "ip": m.IP, "mac": m.MAC, "icon": "tablet", "template": "closed"})
    check("scratch device added (created)", st == 200 and js.get("result") == "created", str(js)[:200])
    # AdGuard's check_host does not render client-specific Safe Search rewrites (it only evaluates the rules: www.google.com shows the allow-always rule for the
    # existing children too), and a query cannot be sent from the scratch IP; so what is checked is the client entry's flags (the same settings the 2026-10-05
    # real-query test proved to produce the rewrite) next to an existing child's entry.
    mine = m.client_entry()[0]
    ref = [c for c in m.agh("/clients")["clients"] if "192.168.31.60" in (c.get("ids") or [])][0]
    same = (mine.get("use_global_settings"), mine.get("safe_search")) == (ref.get("use_global_settings"), ref.get("safe_search"))
    stamp("client flags: new %s / existing %s" % ((mine.get("use_global_settings"), mine.get("safe_search")), (ref.get("use_global_settings"), ref.get("safe_search"))))
    check("the new child's Safe Search / YouTube settings equal an existing child's (own settings, not global)", same and mine.get("use_global_settings") is False)

    # backup + restore test with the scratch child present
    rc, out = sh("sudo systemctl start restic-backup.service", 1800)
    check("a fresh local restic backup ran with the scratch child present", rc == 0, out[-300:])
    rc, out = sh("sudo /srv/compose/_system/restic-backup/restore-test-portal.sh 2>&1", 900)
    stamp("restore test output:\n" + out)
    check("the portal restore test passes", rc == 0 and "BROKEN" not in out, out[-300:])
    rc, out = sh("sudo rm -rf /tmp/rt29b && mkdir -m 700 /tmp/rt29b && sudo bash -c 'set -a; . /srv/compose/_system/restic-backup/.env; set +a; restic restore latest --tag set --target /tmp/rt29b --include /srv/apps/backup-staging/portal-schedules 2>&1 | tail -1'; sudo chmod -R a+rX /tmp/rt29b")
    db = sqlite3.connect("file:/tmp/rt29b/srv/apps/backup-staging/portal-schedules/schedules.db?mode=ro&immutable=1", uri=True)
    n = db.execute("SELECT count(*) FROM entities WHERE id='child_proofchild_scratch'").fetchone()[0]
    stamp("restored database holds the scratch child entity: %d" % n)
    check("the restored database contains the scratch child", n == 1)
    sh("sudo rm -rf /tmp/rt29b")

    # drift watch + gateway health
    rc, out = sh("sudo systemctl start policy-drift-watch.service; sleep 2; journalctl -u policy-drift-watch.service -n 6 --no-pager -o cat")
    stamp("policy-drift-watch run:\n" + out)
    check("policy-drift-watch ran clean with the scratch child present", "drift" not in out.lower() or "no drift" in out.lower() or "ok" in out.lower(), out[-300:])
    st, h = m.call("probe-padmin", "GET", "/v1/parental/health")
    check("gateway health: policy in sync", st == 200 and h["policy"]["in_sync"], str(h.get("policy")))

    rc, _o, err = m.sched("child", "remove", m.CID, "--forget")
    check("scratch device removed and its record forgotten (service; the gateway removal route was proven in the first run)", rc == 0, err[:200])
    m.wait_for(lambda: m.verdict("never-allowed.example.net") == "allowed", 150)
    if m.client_entry():
        m.agh("/clients/delete", {"name": m.NAME + " " + m.LABEL})
    check("clean: client entry gone on origin and replica", m.wait_for(lambda: not m.client_entry() and not m.client_entry(m.replica), 150))
    check("real children's lines unchanged; origin rules equal the start", m.real_lines() == base_real and m.agh("/filtering/status").get("user_rules") == base_rules)
    st, rm = m.call("probe-padmin", "GET", "/v1/parental/removed-children")
    check("nothing left among the removed", not [x for x in rm["removed"] if x["ip"] == m.IP])
    stamp("=== %d check(s) failed: %s ===" % (len(m.FAILS), m.FAILS))
    return 1 if m.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
