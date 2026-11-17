#!/usr/bin/env python3
"""2026-11-17 failover in a REAL headless Chromium against tools/ui_cameras_scratch.py: when the lenses cannot be opened as video (the fake go2rtc answers 503 while OUTDIR/live-down
exists) every tile switches to frames (snapshot refreshed every few seconds) instead of «нет связи», the video comes back by itself once the camera answers again, and
«Продолжить» brings it back at once.

  PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras_failover.py OUTDIR"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(info)) if info else ""), flush=True)


JS_PLAY = "() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).map(v => !v.hidden && !v.paused && v.currentTime > 0)"
JS_SNAP = "() => Array.from(document.querySelectorAll('#cam-root .cam-pane img.cam-snap')).map(i => !i.hidden && i.naturalWidth > 0)"


def wait(page, js, n, secs, want=True):
    end = time.time() + secs
    got = []
    while time.time() < end:
        got = page.evaluate(js)
        if len(got) == n and all(bool(x) == want for x in got):
            return True, got
        time.sleep(0.4)
    return False, got


def main(outdir):
    port = open(os.path.join(outdir, "port.txt")).read().strip()
    cookie = open(os.path.join(outdir, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port
    down = os.path.join(outdir, "live-down")
    if os.path.exists(down):
        os.remove(down)
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 412, "height": 915}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        ok, got = wait(page, JS_PLAY, 3, 40)
        check("normal: three lenses play video", ok, got)
        # ── the camera stops delivering video ──
        open(down, "w").close()
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        ok, got = wait(page, JS_SNAP, 3, 40)
        check("video cannot be opened: all three lenses show frames within 40 s", ok, got)
        texts = page.locator(".cam-tile[data-camera] .cam-status").all_inner_texts()
        check("the tiles say what is happening, not «нет связи»", all("кадры" in t for t in texts) and not any("Нет связи" in t for t in texts), texts)
        s1 = page.evaluate("() => document.querySelector('img.cam-snap').src")
        time.sleep(6)
        s2 = page.evaluate("() => document.querySelector('img.cam-snap').src")
        check("the frame is refreshed by itself", s1 != s2, (s1[-24:], s2[-24:]))
        check("the video elements are hidden while frames show", page.evaluate("() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).every(v => v.hidden)"))
        # ── the camera is back: the video returns on its own (first retry after 20 s) ──
        os.remove(down)
        ok, got = wait(page, JS_PLAY, 3, 70)
        check("the camera answers again: video returns by itself on all three", ok, got)
        check("frames are hidden again", page.evaluate("() => Array.from(document.querySelectorAll('img.cam-snap')).every(i => i.hidden)"))
        # ── «Продолжить» brings the video back at once ──
        open(down, "w").close()
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        ok, got = wait(page, JS_SNAP, 3, 40)
        check("again down: frames", ok, got)
        os.remove(down)
        page.locator(".cam-tile[data-camera] .cam-continue:visible").first.click()
        end = time.time() + 15
        first = False
        while time.time() < end:
            first = page.evaluate(JS_PLAY)[0]
            if first:
                break
            time.sleep(0.3)
        check("«Продолжить» on one lens brings its video back within 15 s (before the timer)", first)
        check("no script errors", not errors, errors[:3])
        b.close()
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
