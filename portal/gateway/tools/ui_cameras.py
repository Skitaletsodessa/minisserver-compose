#!/usr/bin/env python3
"""The cameras page in a REAL headless Chromium at phone size (Task 34b round 2), against tools/ui_cameras_scratch.py (fake go2rtc, synthetic archive).

  PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras.py OUTDIR [--screens DIR]      # OUTDIR = the scratch's directory (port.txt, session-household.txt)

Needs a throw-away venv with `pip install playwright` and `playwright install chromium-headless-shell` (NOT a dependency of the gateway). Checks Ivan's complaints:
all three live lenses start TOGETHER (none plays while another still connects); the archive shows ONE list where clips a second or two apart are one event; the player is at
the top and an event choice brings it into view; the lenses of an event start together, the later one with its own delay; previous/next event; no console errors."""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(info)) if info else ""), flush=True)


def main(outdir, screens=None):
    port = open(os.path.join(outdir, "port.txt")).read().strip()
    cookie = open(os.path.join(outdir, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 360, "height": 800}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        t0 = time.time()
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        tabs = page.locator("button.cam-tab")
        check("two tabs: Сейчас and Записи", tabs.count() == 2 and tabs.nth(0).inner_text() == "Сейчас" and tabs.nth(1).inner_text() == "Записи")
        # ── live: nobody plays until all three are ready ──
        state = lambda: page.evaluate("""() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).map(v => ({paused: v.paused, t: v.currentTime, rs: v.readyState, src: !!v.getAttribute('src')}))""")  # noqa: E731
        early_playing = False
        first_ready_while_others_wait = False
        deadline = time.time() + 40
        while time.time() < deadline:
            s = state()
            if any(not x["paused"] for x in s) and not all(not x["paused"] for x in s):
                early_playing = True
            if any(x["rs"] >= 3 for x in s) and all(x["paused"] for x in s) and not all(x["rs"] >= 3 for x in s):
                first_ready_while_others_wait = True
            if all(not x["paused"] and x["t"] > 0 for x in s):
                break
            time.sleep(0.2)
        s = state()
        check("a lens that is ready waits for the others (nothing plays alone)", first_ready_while_others_wait and not early_playing, "early=%s waited=%s" % (early_playing, first_ready_while_others_wait))
        check("then all three lenses play (%.1f s after opening)" % (time.time() - t0), len(s) == 3 and all(not x["paused"] and x["t"] > 0 for x in s), json.dumps(s))
        check("the wait note is gone once they play", page.locator("#cam-root .cam-pane p.note:visible").count() == 0)
        if screens:
            page.screenshot(path=os.path.join(screens, "live.png"))
        # ── the main stream does not come: the tile falls back to the light stream by itself and says so ──
        page.locator(".cam-tile[data-camera=cam0] video").click()
        page.locator(".cam-tile[data-camera=cam0] .cam-hdbtn").click()
        page.wait_for_function("() => document.querySelector('.cam-tile[data-camera=cam0] .cam-status').textContent.includes('недоступна')", timeout=40000)
        page.wait_for_function("() => { const v = document.querySelector('.cam-tile[data-camera=cam0] video'); return !v.paused && v.readyState >= 3; }", timeout=30000)
        check("«Чётче» that the camera cannot serve falls back to the light stream, which plays, with a plain note", True)
        page.locator(".cam-tile[data-camera=cam0] .cam-expand").click()
        # ── archive ──
        tabs.nth(1).click()
        page.wait_for_selector("#cam-clips li button")
        live_after = page.evaluate("""() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).filter(v => v.getAttribute('src')).length""")
        check("leaving the live tab closes the live streams (no camera session held)", live_after == 0, live_after)
        rows = [x.strip() for x in page.locator("#cam-clips li button").all_inner_texts()]
        print("events:", rows)
        check("one merged list: 3 events for 12.11 (not one row per clip)", len(rows) == 3, rows)
        ev = [r for r in rows if r.startswith("13:15:26")]
        check("13:15:26 + 13:15:27 + 13:15:29 are ONE event with the three lenses named", len(ev) == 1 and "Вход" in ev[0] and "Левая" in ev[0] and "Правая" in ev[0], ev)
        check("no separate rows for 13:15:27 / 13:15:29", not any(r.startswith(("13:15:27", "13:15:29")) for r in rows))
        check("newest first", rows[0].startswith("13:30:00"), rows[:1])
        check("before an event is chosen there is no empty player: the list is right under the chips", not page.locator("#cam-sync").is_visible() and page.evaluate("document.getElementById('cam-clips').getBoundingClientRect().top < 560"))
        if screens:
            page.screenshot(path=os.path.join(screens, "archive.png"))
        # scroll the page down (as a long list would), then choose the event: the player must come into view without any manual scrolling
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.locator("#cam-clips li button", has_text="13:15:26").click()
        page.wait_for_function("() => { const r = document.getElementById('cam-sync').getBoundingClientRect(); return r.top >= 100 && r.top < 260; }", timeout=8000)
        check("choosing an event brings the player into view by itself", True)
        check("the player is above the list", page.evaluate("document.getElementById('cam-sync').getBoundingClientRect().top < document.getElementById('cam-clips').getBoundingClientRect().top"))
        if screens:
            page.screenshot(path=os.path.join(screens, "archive-event.png"))
        # ── the lenses of the event start together; the one that began 1 s later waits 1 s ──
        t_start = None
        deadline = time.time() + 30
        while time.time() < deadline:
            s = page.evaluate("""() => Array.from(document.querySelectorAll('#cam-syncgrid video')).map(v => ({paused: v.paused, t: v.currentTime, rs: v.readyState}))""")
            if len(s) == 3 and any(not x["paused"] and x["t"] > 0 for x in s):
                t_start = time.time()
                break
            time.sleep(0.05)
        check("the three lenses are in the player", page.locator("#cam-syncgrid video").count() == 3)
        check("playback started only after all three were ready", t_start is not None and all(x["rs"] >= 2 for x in s), json.dumps(s))
        time.sleep(2.2)
        s = page.evaluate("""() => Array.from(document.querySelectorAll('#cam-syncgrid video')).map(v => ({paused: v.paused, t: v.currentTime}))""")
        check("all three play after ~2 s (the later lenses started 1 s and 3 s later: Левая ~1.2 s, Правая ~0 s behind the first)", len(s) == 3 and s[0]["t"] > 1.0 and not s[0]["paused"] and 0.0 < s[1]["t"] < s[0]["t"], json.dumps(s))
        check("the title names the event", "13:15:26" in page.locator(".cam-event-title").inner_text(), page.locator(".cam-event-title").inner_text())
        # ── pause / next / previous ──
        page.locator(".cam-playpause").click()
        time.sleep(0.3)
        s1 = page.evaluate("Array.from(document.querySelectorAll('#cam-syncgrid video')).map(v => v.paused)")
        check("pause stops all three", all(s1), s1)
        page.locator(".cam-next").click()
        page.wait_for_function("() => document.querySelector('.cam-event-title').textContent.includes('13:30:00')", timeout=8000)
        check("«Позже» goes to the next event (13:30:00)", True)
        page.locator(".cam-next").click()
        page.wait_for_selector(".cam-stepnote:visible", timeout=8000)
        check("no later event: a plain-language note", "Позже" in page.locator(".cam-stepnote").inner_text(), page.locator(".cam-stepnote").inner_text())
        page.locator(".cam-prev").click()
        page.wait_for_function("() => document.querySelector('.cam-event-title').textContent.includes('13:15:26')", timeout=8000)
        check("«Раньше» goes back (13:15:26)", True)
        page.locator("#cam-clips li button", has_text="09:00:00").click()
        page.wait_for_function("() => document.querySelector('.cam-event-title').textContent.includes('09:00:00')", timeout=8000)
        page.locator(".cam-prev").click()
        page.wait_for_function("() => document.querySelector('.cam-event-title').textContent.includes('22:00:00')", timeout=10000)
        check("«Раньше» at the first event of a day crosses to the day before (11.11 22:00:00)", "11.11" in page.locator(".cam-event-title").inner_text(), page.locator(".cam-event-title").inner_text())
        # ── no horizontal overflow, tap targets ──
        ov = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        check("no horizontal overflow at 360 px", ov <= 0, ov)
        small = page.evaluate("""() => Array.from(document.querySelectorAll('#cam-root button:not([hidden])')).filter(b => b.offsetParent && b.getBoundingClientRect().height < 44).map(b => b.textContent)""")
        check("every visible button is at least 44 px high", not small, small)
        errors = [x for x in errors if "503" not in x]                                     # the deliberate main-stream refusal above
        check("no console errors (but the deliberate 503)", not errors, errors[:3])
        b.close()
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    scr = None
    if "--screens" in sys.argv:
        scr = sys.argv[sys.argv.index("--screens") + 1]
        os.makedirs(scr, exist_ok=True)
    sys.exit(main(sys.argv[1], scr))
