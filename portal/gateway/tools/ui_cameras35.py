#!/usr/bin/env python3
"""Task 35 B in a REAL headless Chromium against tools/ui_cameras_scratch.py: «Чётче» on every lens (with the door's "no main stream" answer faked), real full screen
that folds back, and the layout in portrait and landscape at phone sizes (live grid, big view, archive player).

  PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras35.py OUTDIR [--screens DIR]"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
SIZES = [("portrait 360x800", 360, 800, 2), ("portrait 412x915", 412, 915, 2), ("landscape 800x360", 800, 360, 3), ("landscape 915x412", 915, 412, 3)]
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(info)) if info else ""), flush=True)


def run(p, base, cookie, label, w, h, cols, door, screens):
    b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    ctx = b.new_context(viewport={"width": w, "height": h}, user_agent=UA, is_mobile=True, has_touch=True)
    ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    if door:                                                                    # the public door answers hd_available=false
        def patch(route):
            r = route.fetch()
            js = r.json()
            js["hd_available"] = False
            route.fulfill(response=r, body=json.dumps(js))
        page.route("**/v1/cameras", patch)
    page.goto(base + "/cameras")
    page.wait_for_selector(".cam-tile[data-camera]")
    deadline = time.time() + 40
    while time.time() < deadline:
        s = page.evaluate("() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).map(v => !v.paused && v.currentTime > 0)")
        if len(s) == 3 and all(s):
            break
        time.sleep(0.3)
    tag = "[%s%s] " % (label, ", door" if door else "")
    # ── «Чётче» is on every lens, not only in the big view ──
    vis = page.locator(".cam-tile[data-camera] .cam-hdbtn:visible").count()
    check(tag + "«Чётче» is visible on all 3 lenses", vis == 3, vis)
    if door:
        page.locator(".cam-tile[data-camera] .cam-hdbtn").first.click()
        note = page.locator(".cam-tile[data-camera] .cam-status").first
        page.wait_for_function("() => /недоступна/.test(document.querySelector('.cam-tile[data-camera] .cam-status').textContent)", timeout=4000)
        check(tag + "without the main stream the button explains why (and the picture keeps playing)",
              "домашней сети" in note.inner_text() and page.evaluate("() => !document.querySelector('.cam-tile[data-camera] video').paused"), note.inner_text()[:60])
    # ── layout of the live grid ──
    tracks = page.evaluate("() => getComputedStyle(document.querySelector('.cam-grid')).gridTemplateColumns.split(' ').length")
    check(tag + "the live grid has %d columns" % cols, tracks == cols, tracks)
    ov = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    check(tag + "no horizontal overflow", ov <= 0, ov)
    if screens:
        page.screenshot(path=os.path.join(screens, "live-" + label.replace(" ", "_") + ("-door" if door else "") + ".png"))
    # ── big view and real full screen ──
    page.locator(".cam-tile[data-camera] .cam-expand").first.click()
    page.wait_for_timeout(500)
    big = page.evaluate("() => { const f = document.querySelector('.cam-tile.cam-big'); if (!f) return null; const v = f.querySelector('video').getBoundingClientRect(); return {w: v.width, h: v.height, fs: !!(document.fullscreenElement || document.webkitFullscreenElement), vw: innerWidth, vh: innerHeight}; }")
    check(tag + "the big view opens", big is not None, big)
    if big:
        check(tag + "the big video fits the screen and keeps 16:9", big["w"] <= big["vw"] + 1 and big["h"] <= big["vh"] + 1 and abs(big["w"] / big["h"] - 16 / 9) < 0.2, big)
        check(tag + "the Fullscreen API is used (real full screen)", big["fs"], big)
        if screens:
            page.screenshot(path=os.path.join(screens, "big-" + label.replace(" ", "_") + ("-door" if door else "") + ".png"))
        page.evaluate("() => document.exitFullscreen && document.exitFullscreen()")           # what the system back gesture does
        page.wait_for_timeout(500)
        folded = page.evaluate("() => !document.querySelector('.cam-tile.cam-big') && !document.body.classList.contains('cam-has-big')")
        check(tag + "leaving full screen folds the big view back", folded)
    # ── archive player ──
    if not door:
        page.locator("button.cam-tab", has_text="Записи").click()
        page.wait_for_selector("#cam-clips li button")
        page.locator("#cam-clips li button", has_text="13:15:26").click()
        page.wait_for_selector(".cam-syncgrid .cam-lens video", timeout=10000)
        page.wait_for_timeout(1500)
        g = page.evaluate("() => { const g = document.querySelector('.cam-syncgrid'); const l = Array.from(g.querySelectorAll('.cam-lens')).map(n => n.getBoundingClientRect()); return {cols: getComputedStyle(g).gridTemplateColumns.split(' ').length, tops: l.map(r => Math.round(r.top)), right: Math.max(...l.map(r => r.right)), vw: innerWidth}; }")
        check(tag + "archive lenses in %d columns" % (3 if cols == 3 else 2), g["cols"] == (3 if cols == 3 else 2), g)
        check(tag + "archive lenses do not overflow", g["right"] <= g["vw"] + 1, g)
        ov = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        check(tag + "archive: no horizontal overflow", ov <= 0, ov)
        if screens:
            page.screenshot(path=os.path.join(screens, "archive-" + label.replace(" ", "_") + ".png"))
    errors = [x for x in errors if "503" not in x]
    check(tag + "no console errors", not errors, errors[:3])
    b.close()


def main(outdir, screens=None):
    port = open(os.path.join(outdir, "port.txt")).read().strip()
    cookie = open(os.path.join(outdir, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port
    with sync_playwright() as p:
        for label, w, h, cols in SIZES:
            run(p, base, cookie, label, w, h, cols, False, screens)
        run(p, base, cookie, SIZES[0][0], SIZES[0][1], SIZES[0][2], SIZES[0][3], True, screens)
        run(p, base, cookie, SIZES[2][0], SIZES[2][1], SIZES[2][2], SIZES[2][3], True, screens)
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    scr = None
    if "--screens" in sys.argv:
        scr = sys.argv[sys.argv.index("--screens") + 1]
        os.makedirs(scr, exist_ok=True)
    sys.exit(main(sys.argv[1], scr))
