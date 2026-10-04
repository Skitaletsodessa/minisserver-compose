#!/usr/bin/env python3
"""Immich release watch (Task 24 follow-up). Diun cannot list ghcr.io/immich-app/immich-server reliably (~31 000 tags, intermittent
timeouts, 'skipped=1'), so a new Immich release could pass without a notice. This asks GitHub for the latest (non-pre) release once
a day and compares it with the version pinned in /srv/compose/immich/compose.yaml.

New version -> ONE Telegram message (with a hint whether the notes use security wording) and ONE reminder 7 days later if still not
installed (the policy is: security releases within a week). It never updates anything (rule 5).
A failing check (GitHub unreachable, odd answer) alerts through watchnotify only after 2 consecutive days.
Options: --dry-run (print, send nothing; state untouched). WATCH_TEST=1 uses the watchnotify test mode."""
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, "/srv/compose/_system/lib")
import watchnotify  # noqa: E402

COMPOSE = os.environ.get("IRW_COMPOSE", "/srv/compose/immich/compose.yaml")
STATE = os.environ.get("IRW_STATE", "/var/lib/immich-release-watch/state.json")
URL = os.environ.get("IRW_URL", "https://api.github.com/repos/immich-app/immich/releases/latest")
DRY = "--dry-run" in sys.argv
SECURITY = re.compile(r"security|vulnerab|CVE-\d|GHSA-", re.I)


def ver(tag):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def load():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save(st):
    if DRY:
        return
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE + ".tmp", "w"))
    os.replace(STATE + ".tmp", STATE)


def fail(st, why):
    st["fails"] = st.get("fails", 0) + 1
    print("check failed:", why)
    if st["fails"] >= 2 and not DRY:
        watchnotify.alert("immich-release-watch", f"Immich release check has failed {st['fails']} days in a row: {why}. "
                          "Diun may also be skipping the Immich tag list, so a release could be missed - check by hand.")
    save(st)
    sys.exit(0)


def main():
    st = load()
    running = re.search(r"immich-server:(v\d+\.\d+\.\d+)", open(COMPOSE).read())
    if not running:
        fail(st, "cannot read the pinned version from compose.yaml")
    running = running.group(1)
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "minisserver-immich-release-watch", "Accept": "application/vnd.github+json"})
        rel = json.load(urllib.request.urlopen(req, timeout=20))
        latest, body, link = rel["tag_name"], rel.get("body") or "", rel.get("html_url", "")
    except Exception as e:  # network, rate limit, bad JSON: all the same to us
        fail(st, f"{type(e).__name__}")
    if ver(latest) is None or ver(running) is None:
        fail(st, f"unrecognised version format ({latest!r} vs {running!r})")
    if st.get("fails", 0) >= 2 and not DRY:
        watchnotify.clear("immich-release-watch", "Immich release check works again.")
    st["fails"] = 0
    st["last_ok"] = int(time.time())
    print(f"running {running}, latest {latest}")
    if ver(latest) <= ver(running):
        st.pop("first_seen", None)
        save(st)
        return
    sec = "security wording found in the release notes" if SECURITY.search(body) else "no security wording in the release notes"
    seen = st.get("first_seen", {})
    if seen.get("tag") != latest:
        text = (f"Immich {latest} is out (running {running}); {sec}. {link}\n"
                "Policy: a public service takes security releases within a week. Nothing was changed - tell me to update.")
        print("NOTIFY:", text)
        if DRY or watchnotify.send(text):
            st["first_seen"] = {"tag": latest, "at": int(time.time()), "reminded": False}
    elif not seen.get("reminded") and time.time() - seen["at"] > 7 * 86400:
        text = f"Reminder: Immich {latest} has been out for 7+ days and {running} is still running ({sec})."
        print("NOTIFY:", text)
        if DRY or watchnotify.send(text):
            seen["reminded"] = True
    save(st)


main()
