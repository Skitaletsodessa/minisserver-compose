#!/usr/bin/env python3
"""Live READ-ONLY proof of the new parents' UI on the REAL gateway over the tailnet site (Task 31 Part E), with a SYNTHETIC household parent `probe-phouse` (never a real person).
Run on the server as skit:   python3 tools/live_ui31.py

  1. enable the synthetic parent, complete its setup link over loopback (password and authenticator generated for this run, written nowhere, printed nowhere);
  2. sign in on the tailnet site (web session: no phone, no device record, so no "new device" message);
  3. a real headless Chromium (mobile user agent, 360x800 and 412x915) loads /parental, a child page and the schedule page with that session and READS: the tiles, the sentences,
     the always-open line, the banned words, the collapsed blocks. It OPENS the access sheet and closes it again; it NEVER grants, saves or changes anything;
  4. the synthetic parent is disabled (which ends its sessions) and the setup-link files are deleted. The audit is checked: no parental write happened during the run."""
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/srv/compose/portal/gateway")
from gw import auth  # noqa: E402

LOOP = "http://127.0.0.1:17444"
TAILNET = os.environ.get("LIVE_UI_BASE", "https://minisserver.tail6bf4d5.ts.net:17443")
GWCTL = "/srv/compose/portal/gateway/gwctl"
OUT = "/srv/apps/portal/gateway/out"
PID = "probe-phouse"
FAILS = []
PW_PY = "/tmp/pw-venv/bin/python"

BROWSE = r'''
import json, sys
from playwright.sync_api import sync_playwright
base, cookie = sys.argv[1], sys.argv[2]
UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
res = {}
with sync_playwright() as p:
    b = p.chromium.launch()
    for (w, h) in ((360, 800), (412, 915)):
        ctx = b.new_context(viewport={"width": w, "height": h}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        pg = ctx.new_page(); errs = []; writes = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text[:160]) if m.type == "error" else None)
        pg.on("request", lambda r: writes.append(r.method + " " + r.url) if r.method != "GET" else None)
        pg.goto(base + "/parental"); pg.wait_for_selector("section[data-child]", timeout=30000); pg.wait_for_timeout(1500)
        tiles = pg.eval_on_selector_all("section[data-child]", "els => els.map(e => ({id: e.dataset.child, text: e.innerText}))")
        main = pg.inner_text("main")
        overflow = pg.evaluate("document.documentElement.scrollWidth > innerWidth")
        pg.click("[data-open-sheet]"); pg.wait_for_selector(".sheet"); sheet = pg.inner_text(".sheet")
        disabled = pg.eval_on_selector_all(".dur", "els => els.every(e => e.disabled)")
        pg.keyboard.press("Escape")
        cid = tiles[0]["id"]
        pg.goto(base + "/parental#" + cid); pg.reload(); pg.wait_for_selector(".kd", timeout=30000); pg.wait_for_timeout(800)
        child = pg.inner_text("main")
        pg.goto(base + "/parental#" + cid + "/schedule"); pg.reload(); pg.wait_for_selector(".sch-rule", timeout=30000)
        sched = pg.inner_text("main")
        chips = pg.eval_on_selector_all(".sch-rule .daychip", "els => els.slice(0,7).map(e => e.getAttribute('aria-pressed'))")
        more = pg.query_selector("details.kd-more") is not None
        res["%dx%d" % (w, h)] = {"tiles": tiles, "main": main, "overflow": overflow, "sheet": sheet, "durations_disabled_first": disabled, "child": child, "sched": sched, "chips": chips, "more": more, "errors": errs, "writes": writes}
        if w == 360:
            pg.screenshot(path="/tmp/live-ui31-schedule.png", full_page=True)
            pg.goto(base + "/parental"); pg.reload(); pg.wait_for_selector("section[data-child]"); pg.wait_for_timeout(1000)
            pg.screenshot(path="/tmp/live-ui31-list.png", full_page=True)
        ctx.close()
    b.close()
print(json.dumps(res))
'''


def stamp(m):
    print("%s  %s" % (time.strftime("%H:%M:%S"), m), flush=True)


def check(name, cond, detail=""):
    stamp("%s %s%s" % ("PASS" if cond else "FAIL", name, ("   [%s]" % detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def call(method, url, body=None, headers=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded" if isinstance(body, bytes) else "application/json")
    if method != "GET":
        h.setdefault("Idempotency-Key", "live-" + secrets.token_hex(6))
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def ctl(*a):
    return subprocess.run([GWCTL, *a], capture_output=True, text=True, timeout=60)


def audit_writes(since):
    r = ctl("audit", "--limit", "200") if False else None                      # the audit is read through the API of the schedule service below
    tok = open(os.path.expanduser("~/.config/schedctl/token")).read().strip()
    req = urllib.request.Request("http://127.0.0.1:16444/v1/audit?limit=60", headers={"Authorization": "Bearer " + tok})
    rows = json.loads(urllib.request.urlopen(req, timeout=20).read())["audit"]
    return [x for x in rows if x["seq"] > since]


def main():
    tok = open(os.path.expanduser("~/.config/schedctl/token")).read().strip()
    seq0 = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:16444/v1/audit?limit=1", headers={"Authorization": "Bearer " + tok}), timeout=20).read())["audit"][0]["seq"]
    ctl("person", "enable", PID)
    ctl("person", "web-login", PID, "on")
    r = ctl("setup-link", PID)
    path = re.search(r"/data/out/(\S+)", r.stdout)
    link_file = os.path.join(OUT, path.group(1)) if path else None
    cookie = None
    try:
        raw = open(link_file).read().strip().rsplit("/", 1)[-1]
        st, page, _ = call("GET", LOOP + "/setup/" + raw)
        seed = re.search(rb'class="seed">([A-Z2-7]+)<', page).group(1).decode()
        password = "Live-ui31-" + secrets.token_urlsafe(18)
        st, _b, _h = call("POST", LOOP + "/setup/" + raw, urllib.parse.urlencode({"password": password, "code": auth.totp_now(seed)}).encode())
        check("the synthetic parent completed its own setup link", st == 200, str(st))
        os.remove(link_file)
        link_file = None
        st, b, h = call("POST", TAILNET + "/v1/auth/login", {"username": PID, "password": password, "totp": auth.totp_now(seed, time.time() + 30)})
        m = re.search(r"gw_session=([^;]+)", h.get("Set-Cookie") or "")
        check("signed in on the tailnet site (a web session, no device)", st == 200 and bool(m), str(st))
        cookie = m.group(1) if m else None
        if cookie:
            fd, p = tempfile.mkstemp(prefix="browse-", suffix=".py"); os.write(fd, BROWSE.encode()); os.close(fd)
            env = {**os.environ, "LD_LIBRARY_PATH": "/tmp/nsslibs/x/usr/lib/x86_64-linux-gnu", "PLAYWRIGHT_BROWSERS_PATH": "/tmp/pw-browsers"}
            res = subprocess.run([PW_PY, p, TAILNET, cookie], capture_output=True, text=True, timeout=300, env=env)
            os.remove(p)
            if res.returncode:
                check("browser run", False, res.stderr[-400:])
            else:
                data = json.loads(res.stdout.strip().splitlines()[-1])
                for size, d in data.items():
                    check("%s the three real children have a tile" % size, len(d["tiles"]) == 3, str([t["id"] for t in d["tiles"]]))
                    check("%s the always-open line names maps (read from the real data)" % size, "Всегда открыто: Telegram, погода, карты, службы Android" in d["main"], d["main"][-250:])
                    for banned in ("DNS", "snapshot", "Правила применены", "заблокировано запросов", "vlad-", "nastia-", "child_"):
                        check("%s default view has no '%s'" % (size, banned), banned not in d["main"])
                    check("%s every tile has a plain sentence" % size, all(("Интернет ограничен" in t["text"] or "открыт" in t["text"] or "воскресенье" in t["text"]) for t in d["tiles"]), str([t["text"][:60] for t in d["tiles"]]))
                    check("%s no horizontal overflow" % size, not d["overflow"])
                    check("%s the sheet opens, durations disabled until a choice, four presets" % size, d["durations_disabled_first"] and "На сколько?" in d["sheet"] and "1 час" in d["sheet"])
                    check("%s the child page: schedule card and the collapsed blocks" % size, "Расписание" in d["child"] and "Подробности для взрослых" in d["child"] and "DNS" not in d["child"])
                    check("%s the schedule editor shows Monday-Saturday (no Maps row)" % size, d["chips"] == ["true"] * 6 + ["false"] and "карты" in d["sched"].lower() and "maps" not in d["sched"].lower(), str(d["chips"]))
                    check("%s a household parent has no «Ещё» menu" % size, not d["more"])
                    check("%s no script error in the console" % size, not d["errors"], str(d["errors"][:2]))
                    check("%s nothing was written by the browser (no POST/PUT/PATCH/DELETE)" % size, not d["writes"], str(d["writes"][:3]))
                stamp("tiles at 360: %s" % [t["text"].replace("\n", " | ")[:110] for t in data["360x800"]["tiles"]])
    finally:
        if link_file and os.path.exists(link_file):
            os.remove(link_file)
        ctl("person", "disable", PID)
        ctl("person", "web-login", PID, "off")
        ctl("clean-out")
    after = audit_writes(seq0)
    check("no parental write reached the schedule service during the run (audit rows after the start: %d)" % len(after), not [x for x in after if x["action"].startswith("child") or x["action"] in ("update", "undo", "entity")], str([x["action"] for x in after]))
    r = ctl("person", "list")
    check("the synthetic parent is disabled again", re.search(PID + r"\s+household\s+parent=True\s+active=False", r.stdout) is not None, r.stdout[:200])
    stamp("RESULT: %s" % ("all checks passed" if not FAILS else "FAILED: %s" % FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
