#!/usr/bin/env python3
"""Secret-hygiene scan (Task 28 Phase J), run on the server. Reads every secret the gateway uses (HA token, the two schedule-service tokens, any
enrolled probe token, the secrets of the watcher) and searches the places where a secret must never appear: container logs, the audit mirror and the
databases, the whole git history of /srv/compose, the alert state directory, shell histories, /tmp and the portal data tree.
It prints ONLY the secret's NAME, the place and a match count; never a value. Exit 0 = nothing found.
    python3 secret_hygiene.py"""
import glob
import os
import subprocess
import sys

SECRETS = {
    "ha-token": "/srv/apps/portal/gateway-secrets/ha-token",
    "sched-admin": "/srv/apps/portal/gateway-secrets/sched-admin",
    "sched-household": "/srv/apps/portal/gateway-secrets/sched-household",
}
for p in glob.glob("/home/skit/.config/gw-probe/*") + glob.glob("/home/skit/.config/gateway-watch/*"):
    SECRETS["device-token:" + os.path.basename(os.path.dirname(p)) + "/" + os.path.basename(p)] = p

vals = {}
for name, path in SECRETS.items():
    try:
        v = open(path).read().strip()
    except OSError:
        continue
    if len(v) >= 20:
        vals[name] = v.encode()
print("secrets loaded for the scan: %s (values never printed)" % ", ".join(sorted(vals)))

places = []                              # (label, bytes)


def add(label, data):
    places.append((label, data))


def run(*cmd):
    return subprocess.run(list(cmd), capture_output=True).stdout


for c in ("portal-gateway", "portal-schedules", "caddy", "homeassistant"):
    r = subprocess.run(["docker", "logs", c], capture_output=True)
    add("docker logs " + c, r.stdout + r.stderr)
for f in ("/srv/apps/portal/gateway/audit.jsonl", "/srv/apps/portal/gateway/stops.jsonl", "/srv/apps/portal/gateway/gateway.db", "/srv/apps/portal/gateway/gateway.db-wal",
          "/srv/apps/portal/schedules/audit.jsonl", "/srv/apps/portal/schedules/schedules.db", "/srv/apps/portal/schedules/schedules.db-wal"):
    try:
        add(f, open(f, "rb").read())
    except OSError:
        pass
add("git history of /srv/compose (git log -p --all)", run("git", "-C", "/srv/compose", "log", "-p", "--all", "--no-color"))
for f in glob.glob("/var/lib/watch-alerts/*"):
    try:
        add(f, open(f, "rb").read())
    except OSError:
        pass
for f in ("/home/skit/.bash_history", "/home/skit/.python_history"):
    try:
        add(f, open(f, "rb").read())
    except OSError:
        pass
for f in glob.glob("/tmp/*") + glob.glob("/tmp/gwdev/**/*", recursive=True):
    if os.path.isfile(f) and os.path.getsize(f) < 20_000_000:
        try:
            add(f, open(f, "rb").read())
        except OSError:
            pass
for root, dirs, files in os.walk("/srv/apps/portal"):
    dirs[:] = [d for d in dirs if d not in ("gateway-secrets", "venv-gw", "venv-test", "__pycache__")]
    for f in files:
        p = os.path.join(root, f)
        if p.endswith((".db", ".db-wal", ".db-shm")) or p.startswith("/srv/apps/portal/gateway/") and f in ("audit.jsonl", "stops.jsonl"):
            continue                                                    # already scanned above
        try:
            if os.path.getsize(p) < 20_000_000:
                add(p, open(p, "rb").read())
        except OSError:
            pass
journal = subprocess.run(["sudo", "-n", "journalctl", "--no-pager", "--since", "-1 day"], capture_output=True).stdout
add("journal, last 24 h", journal)

found = 0
for label, data in places:
    for name, v in vals.items():
        n = data.count(v)
        if n:
            found += n
            print("FOUND  %-30s in %s: %d time(s)" % (name, label, n))
print("scanned %d places, %.1f MB; matches: %d" % (len(places), sum(len(d) for _l, d in places) / 1e6, found))
sys.exit(1 if found else 0)
