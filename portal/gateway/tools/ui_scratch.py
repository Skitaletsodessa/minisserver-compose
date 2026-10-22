#!/usr/bin/env python3
"""A SCRATCH stack for looking at the parental-control pages in a browser (Task 29): the real gateway and the real schedule service in-process, fake HA, fake AdGuard
(real HTTP, lists with content), synthetic people and data. Nothing here touches the house: no real AdGuard, no real HA, no real credential.

  python tools/ui_scratch.py OUTDIR            # prints the port; writes OUTDIR/session-admin.txt and session-household.txt (mode 600) and keeps running

No password is typed anywhere: the script creates a web session for each synthetic account the way a login would and writes the cookie value; a tester injects it
into the browser of the SCRATCH origin only (localhost through a tunnel). It runs until killed. Run it from portal/gateway with the test venv.
"""
import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from helpers import Env, PASSWORD  # noqa: E402
from gw import auth  # noqa: E402


def now_utc(minutes_ago=0):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    e = Env()
    from fakeagh import FakeJob                                       # Task 29b: the miniature host job, so adding a device can be verified in the scratch stack
    e.job = FakeJob(e).start()
    e.arp = os.path.join(outdir, "arp")
    with open(e.arp, "w") as f:
        f.write("IP address HW type Flags HW address Mask Device" + chr(10))
        for ip, mac in (("192.168.31.1", "9c:9d:7e:74:f6:7d"), ("192.168.31.60", "20:36:d0:14:8d:5a"), ("192.168.31.61", "4a:11:22:33:44:55"), ("192.168.31.70", "4a:11:22:33:44:99"), ("192.168.31.71", "20:36:d0:55:66:77")):
            f.write("%s 0x1 0x2 %s * eno1" % (ip, mac) + chr(10))
    e.o["devices"].arp_file = e.arp
    e.o["devices"].apply_timeout_s = 20
    a = {"person_id": "scratch", "phone_id": None, "ip": "scratch"}
    seeds = {}
    for name, role in (("Webadmin", "admin"), ("Marta", "household")):
        e.people.create(a, name, role, True, web_login=True)
        pid = name.lower()
        raw, _ = e.people.create_setup_link(a, pid)
        _p, seed = e.people.setup_begin(raw)
        e.people.setup_complete(raw, PASSWORD, auth.totp_now(seed), "1.1.1.1")
        seeds[pid] = seed
    for pid in seeds:
        e.db.x("UPDATE credentials SET totp_last_step=0 WHERE person_id=?", (pid,))
        cookie, _sess = e.people.login(pid, PASSWORD, auth.totp_now(seeds[pid]), "127.0.0.1")
        path = os.path.join(outdir, "session-%s.txt" % ("admin" if pid == "webadmin" else "household"))
        with open(path, "w") as f:
            f.write(cookie)
        os.chmod(path, 0o600)
    # an APP session of the admin (a phone token traded for a web session, as the app does): the admin-only entries must be absent in it
    code, _c = e.people.create_enrolment_code(a, "webadmin")
    dev = e.people.enrol(code, {"label": "scratch-phone"}, "1.1.1.1")
    princ = e.people.phone_principal(dev["token"])
    raw_app, _view = e.people.create_app_session(princ, "127.0.0.1")
    with open(os.path.join(outdir, "session-app-admin.txt"), "w") as f:
        f.write(raw_app)
    os.chmod(os.path.join(outdir, "session-app-admin.txt"), 0o600)
    # the scratch AdGuard: the house lists, some activity of the child at .60, a bad list the proof can add by URL
    base = "https://l.example/base.txt"
    e.agh.add_list("AdGuard DNS filter", base, ["doubleclick.net", "ads.example.org", "tracker.example.org"])
    e.agh.add_list("Malicious URL Blocklist (URLHaus)", "https://l.example/urlhaus.txt", ["evil.example.net"])
    e.fetch_contents[base] = "||doubleclick.net^\n||ads.example.org^\n||tracker.example.org^\n"
    bad = "https://l.example/bad.txt"
    e.agh.contents[bad] = ["example.org", "bad1.example.org"]
    e.fetch_contents[bad] = "||example.org^\n||bad1.example.org^\n"
    from gw import filterlists as fl
    good = fl.REGISTRY % 4
    e.agh.contents[good] = ["good%d.example.org" % i for i in range(40)]
    e.fetch_contents[good] = "".join("||good%d.example.org^\n" % i for i in range(40))
    e.agh.stats = {"top_queried_domains": [{"example.com": 9}, {"news.example.cz": 5}], "top_blocked_domains": [{"ads.example.org": 7}]}
    log = []
    for i in range(6):
        log += [{"time": now_utc(5 + i), "client": "192.168.31.60", "question": {"name": "youtube.com.", "type": "A"}, "reason": "NotFilteredNotFound"}]
    log += [{"time": now_utc(20), "client": "192.168.31.60", "question": {"name": "ads.example.org.", "type": "A"}, "reason": "FilteredBlackList"},
            {"time": now_utc(30), "client": "192.168.31.60", "question": {"name": "evil.example.net.", "type": "A"}, "reason": "FilteredBlackList"},
            {"time": now_utc(40), "client": "192.168.31.60", "question": {"name": "school.example.org.", "type": "A"}, "reason": "NotFilteredNotFound"},
            {"time": now_utc(50), "client": "192.168.31.61", "question": {"name": "other.example.org.", "type": "A"}, "reason": "NotFilteredNotFound"}]
    e.agh.querylog = sorted(log, key=lambda x: x["time"], reverse=True)
    e.agh.auto_clients = [{"ip": "192.168.31.60", "name": "a-phone.lan", "source": "ARP"}]
    # the managed block that the service says is expected, so the health line is green
    import httpx
    sd = {"telegram": ["t.me", "telegram.org"], "youtube": ["youtube.com"], "roblox": ["roblox.com"]}
    lines = httpx.post(e.sched.url + "/v1/child-policy/render", headers={"Authorization": "Bearer " + e.sched.admin_secret},
                       json={"service_domains": sd, "at": dt.datetime.now(dt.timezone.utc).isoformat(), "overrides": []}).json()["lines"]
    e.agh.user_rules = lines
    e.parity_runs = True
    e._write_parity(time.time())
    e.o["filters"].replica_wait_s, e.o["filters"].parity_wait_s = 2.0, 2.0
    e.agh.auto_clients = [{"ip": "192.168.31.60", "name": "a-phone.lan", "source": "ARP"}, {"ip": "192.168.31.70", "name": "anna-phone.lan", "source": "ARP"}]
    # a clock that starts at SCRATCH_AT (default 12:00 local) and then runs at normal speed: the service, the fake job and the gateway all read it, so sentences and read-backs agree
    hh, mm = (os.environ.get("SCRATCH_AT") or "12:00").split(":")
    start = dt.datetime.now(dt.timezone.utc).astimezone().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    shift = start.timestamp() - time.time()
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Prague")
    e.now_fn = lambda: dt.datetime.fromtimestamp(time.time() + shift, tz)
    e.sched.store.clock = lambda: e.now_fn().isoformat(timespec="seconds")
    e.o["parental"].clock = lambda: time.time() + shift
    with open(os.path.join(outdir, "port.txt"), "w") as f:
        f.write(str(e.port))
    print("scratch gateway on 127.0.0.1:%d (sessions in %s)" % (e.port, outdir), flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ui-scratch")
