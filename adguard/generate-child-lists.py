#!/usr/bin/env python3
"""Task 29 (ADR 0008 (d)): after the cutover the domain lists are DATA of the schedule service; the text files under child-policy/ are GENERATED from its snapshot
and committed (like the HA config), so they stay readable and in git history, and nothing edits them by hand any more.

Runs from child-lists.path when the snapshot changes (and once at the cutover). Only acts while child-policy/source.conf says `snapshot`; in `files` mode the
files are the source and this does nothing (so a rollback to `files` plus a checkout of the pre-cutover files is a complete rollback).
Writes: allow-always.txt, allow-weekdays.txt (house-wide), allow-extra.txt / deny-extra.txt (informational UNION over the children), and generated/<device>.txt
(the per-child allow and deny lists). Commits only the child-policy directory and pushes.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import childsnapshot  # noqa: E402

POLICY = HERE / "child-policy"
HEADER = ["# GENERATED from the schedule service's child-policy snapshot (Task 29) - do not edit; change it in the portal.",
          "# snapshot checksum: %s"]


def write_if_changed(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def main():
    if childsnapshot.mode() != "snapshot":
        print("source is `files`: nothing generated")
        return 0
    loaded = childsnapshot.load()
    snap = loaded["snap"]
    if snap is None:
        print("no usable snapshot:", loaded["live_problem"], file=sys.stderr)
        return 1
    head = "\n".join(HEADER) % snap["checksum"][:16] + "\n"
    lists = snap["lists"]
    changed = []
    for name, key in (("allow-always.txt", "allow_always"), ("allow-weekdays.txt", "allow_weekdays")):
        if write_if_changed(POLICY / name, head + "\n".join(sorted(lists.get(key) or [])) + "\n"):
            changed.append(name)
    allow = sorted({d for dev in snap["devices"] for d in (dev.get("allow_extra") or [])})
    deny = sorted({d for dev in snap["devices"] for d in (dev.get("deny_extra") or [])})
    for name, vals, note in (("allow-extra.txt", allow, "union over the children (informational)"), ("deny-extra.txt", deny, "union over the children (informational)")):
        if write_if_changed(POLICY / name, head + "# " + note + "\n" + "\n".join(vals) + "\n"):
            changed.append(name)
    for dev in snap["devices"]:
        text = head + "# %s (%s)\n# allow:\n%s\n# deny:\n%s\n" % (dev["name"], dev["ip"], "\n".join(sorted(dev.get("allow_extra") or [])), "\n".join(sorted(dev.get("deny_extra") or [])))
        if write_if_changed(POLICY / "generated" / (dev["id"] + ".txt"), text):
            changed.append("generated/%s.txt" % dev["id"])
    keep = {dev["id"] + ".txt" for dev in snap["devices"]}              # a device that was removed leaves no file behind (found by the live proof with a scratch device)
    for stale in sorted((POLICY / "generated").glob("*.txt")) if (POLICY / "generated").is_dir() else []:
        if stale.name not in keep:
            stale.unlink()
            changed.append("generated/%s (removed)" % stale.name)
    if not changed:
        print("up to date")
        return 0
    repo = "/srv/compose"
    run = lambda *a: subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True, timeout=90)
    run("add", "-A", "--", "adguard/child-policy")
    if run("diff", "--cached", "--quiet", "--", "adguard/child-policy").returncode == 0:
        print("changed files are ignored by git?", changed)
        return 0
    r = run("commit", "-q", "-m", "child-policy: lists regenerated from the snapshot %s (%s)\n\nCo-Authored-By: portal-apply <noreply@minisserver>" % (snap["checksum"][:12], ", ".join(changed)),
            "--", "adguard/child-policy")
    if r.returncode != 0:
        print("commit failed:", r.stderr.strip()[:200], file=sys.stderr)
        return 1
    p = run("push", "-q")
    print("regenerated %s; %s" % (", ".join(changed), "pushed" if p.returncode == 0 else "push failed: " + p.stderr.strip()[:160]))
    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
