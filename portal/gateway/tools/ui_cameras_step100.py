#!/usr/bin/env python3
"""Task 35c: the archive player in a REAL headless Chromium at phone size, stepping «Раньше» through N events (default 100) against tools/ui_cameras_scratch.py started with
SCRATCH_EVENTS=N+10: every step must show three lenses that load (readyState >= 2, no error note), no 429/5xx on any request, and the page must not hold more than 3 open
video sources (it releases the previous ones).   PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras_step100.py OUTDIR [N]"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"


def main(outdir, n):
    port = open(os.path.join(outdir, "port.txt")).read().strip()
    cookie = open(os.path.join(outdir, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port
    bad, reqs, fails = [], [0], []
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 412, "height": 915}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def on_resp(r):
            reqs[0] += 1
            if r.status == 429 or r.status >= 500:
                bad.append((r.status, r.url[-60:]))
        page.on("response", on_resp)
        page.goto(base + "/cameras")
        page.wait_for_selector("button.cam-tab")
        page.locator("button.cam-tab").nth(1).click()
        page.wait_for_selector("#cam-clips li button")
        page.locator("#cam-clips li button").first.click()
        page.wait_for_selector(".cam-event-title")
        t0 = time.time()
        titles = set()
        for i in range(n):
            ok = False
            end = time.time() + 15
            while time.time() < end:
                st = page.evaluate("""() => { const v = Array.from(document.querySelectorAll('#cam-sync video')); return {n: v.length, ready: v.filter(x => x.readyState >= 2).length, err: document.querySelectorAll('#cam-sync .note.err').length, srcs: document.querySelectorAll('video[src]').length}; }""")
                if st["n"] == 3 and st["ready"] == 3 and st["err"] == 0:
                    ok = True
                    break
                time.sleep(0.15)
            title = page.locator(".cam-event-title").inner_text()
            titles.add(title)
            if not ok or st["srcs"] > 3:
                fails.append((i, title, st))
            page.locator(".cam-prev").click()
            page.wait_for_function("t => document.querySelector('.cam-event-title').textContent !== t", arg=title, timeout=10000)
        print("steps %d in %.0f s, distinct events shown %d, requests %d" % (n, time.time() - t0, len(titles), reqs[0]))
        print("failed steps:", fails[:5], "refusals:", bad[:5], "page errors:", errors[:3])
        b.close()
    good = not fails and not bad and not errors and len(titles) >= n
    print("PASS" if good else "FAIL")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100))
