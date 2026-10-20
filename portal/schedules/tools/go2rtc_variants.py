#!/usr/bin/env python3
"""Task 27a item 1: which smallest change stops go2rtc listening on *:18555? Each variant runs in a throw-away HA (same image, --network none,
default_config as in production, NO devices) and reports the listening sockets (from /proc/net/tcp) and the go2rtc / setup log lines."""
import os, subprocess, sys, time, shutil

S = "/srv/apps/portal/scratch-go2rtc"
NAME = "portal-go2rtc-probe"
IMG = subprocess.run(["docker", "inspect", "homeassistant", "--format", "{{.Config.Image}}"], capture_output=True, text=True).stdout.strip()
BASE = "homeassistant:\n  name: probe\n  time_zone: Europe/Prague\nhttp:\n  server_host: 127.0.0.1\nlogger:\n  default: warning\n  logs:\n    homeassistant.components.go2rtc: debug\n"
EXPLICIT = ("assist_pipeline bluetooth cloud conversation dhcp energy file history homeassistant_alerts logbook media_source mobile_app my ssdp stream "
            "sun usage_prediction usb webhook zeroconf").split()
VARIANTS = {
    "V0 baseline (default_config)": "default_config:\n",
    "V1 default_config + go2rtc url of an external server": "default_config:\ngo2rtc:\n  url: \"http://127.0.0.1:1984\"\n",
    "V2 explicit list = default_config minus go2rtc": "".join("%s:\n" % d for d in EXPLICIT),
}


def sh(*a):
    return subprocess.run(list(a), capture_output=True, text=True)


def listeners():
    out = sh("docker", "exec", NAME, "sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null").stdout
    res = set()
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[3] == "0A" and ":" in f[1]:
            host, port = f[1].rsplit(":", 1)
            res.add((host, int(port, 16)))
    return sorted(res)


for title, cfg in VARIANTS.items():
    sh("sudo", "rm", "-rf", S)
    os.makedirs(S, mode=0o700)
    open(S + "/configuration.yaml", "w").write(BASE + cfg)
    sh("docker", "rm", "-f", "-v", NAME)
    r = sh("docker", "run", "-d", "--name", NAME, "--network", "none", "--memory", "1024m", "-e", "TZ=Europe/Prague", "-v", S + ":/config", IMG)
    assert r.returncode == 0, r.stderr
    time.sleep(100)
    ls = listeners()
    procs = sh("docker", "exec", NAME, "sh", "-c", "ps -eo args | grep -c '[g]o2rtc -c'").stdout.strip()
    logs = sh("docker", "logs", NAME).stdout + sh("docker", "logs", NAME).stderr
    import re
    logs = re.sub(r"\x1b\[[0-9;]*m", "", logs)
    keep = [l[:200] for l in logs.splitlines() if "go2rtc" in l.lower() or "default_config" in l.lower() or "Setup failed" in l]
    print("=== %s" % title)
    print("  listening tcp ports (any address except 127.*):", [p for h, p in ls if not h.startswith("0100007F")] or "none")
    print("  18555 listening:", any(p == 18555 for h, p in ls), "| go2rtc processes:", procs)
    for l in keep[:8]:
        print("   log:", l)
    sh("docker", "rm", "-f", "-v", NAME)
sh("sudo", "rm", "-rf", S)
