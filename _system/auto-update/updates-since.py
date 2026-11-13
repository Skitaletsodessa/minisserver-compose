#!/usr/bin/env python3
"""Task 35 C3 helper: what was updated since the last post-update run. Prints ONE line: `apt list | containers list | kernel` (fields separated by ` | `).
usage: updates-since.py SINCE "YYYY-MM-DD HH:MM:SS" APT_HISTORY CONTAINER_LOG PENDING_REBOOT_JSON"""
import json
import os
import re
import sys


def apt_updates(since, hist):
    out = []
    try:
        text = open(hist, errors="replace").read()
    except OSError:
        return out
    for block in text.split("\n\n"):
        m = re.search(r"^Start-Date: (\d{4}-\d\d-\d\d)\s+(\d\d:\d\d:\d\d)", block, re.M)
        if not m or (m.group(1) + " " + m.group(2)) <= since:
            continue
        for line in block.splitlines():
            if line.startswith(("Upgrade: ", "Install: ")):
                for name, old, new in re.findall(r"([\w.+-]+):\w+ \(([^,)]*)(?:, ([^)]*))?\)", line):
                    out.append("%s %s" % (name, (old + ">" + new) if new else old))
    return out


def container_updates(since, path):
    out = []
    try:
        for line in open(path):
            d = json.loads(line)
            if d.get("at", "") > since and d.get("result") in ("updated", "rolled-back", "failed"):
                what = {"updated": (d.get("to") or "")[:19], "rolled-back": "ROLLED BACK", "failed": "FAILED"}[d["result"]]
                out.append("%s %s" % (d.get("service"), what))
    except (OSError, ValueError):
        pass
    return out


def kernel_change(pend):
    try:
        return "%s>%s" % (json.load(open(pend))["kernel_before"], os.uname().release)
    except (OSError, ValueError, KeyError):
        return "-"


if __name__ == "__main__":
    since, hist, ctr, pend = sys.argv[1:5]
    a = apt_updates(since, hist)
    c = container_updates(since, ctr)
    print((", ".join(a[:12]) + (" +%d more" % (len(a) - 12) if len(a) > 12 else "")) or "-", "|", ", ".join(c) or "-", "|", kernel_change(pend))
