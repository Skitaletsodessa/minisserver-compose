#!/usr/bin/env python3
"""The saved-route button in a REAL headless Chromium against tools/ui_cameras_scratch.py (OUTDIR/ptz is the request/status directory; this script plays the host job):
the button is on the live tab, a press writes request.json, the page shows «Идёт маршрут…» while status.json says running and «Маршрут пройден.» when done; a failed run shows the reason.
  PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras_ptz.py OUTDIR"""
import json, os, sys, time, threading
from playwright.sync_api import sync_playwright
UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
results = []
def check(name, ok, info=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + ((" | " + str(info)) if info else ""), flush=True)
def main(out):
    port = open(os.path.join(out, "port.txt")).read().strip(); cookie = open(os.path.join(out, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port; ptz = os.path.join(out, "ptz")
    for f in ("request.json", "status.json"):
        if os.path.exists(os.path.join(ptz, f)): os.remove(os.path.join(ptz, f))
    def host_job(final, msg=""):                      # what camera_ptz.py does: running, then done/failed
        end = time.time() + 20
        while time.time() < end and not os.path.exists(os.path.join(ptz, "request.json")): time.sleep(0.2)
        time.sleep(1)
        json.dump({"state": "running", "tour": 1, "ends_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 40)), "seconds": 40}, open(os.path.join(ptz, "status.json"), "w"))
        time.sleep(5)
        json.dump({"state": final, "tour": 1, "message": msg}, open(os.path.join(ptz, "status.json"), "w"))
    with sync_playwright() as p:
        b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 412, "height": 915}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        page = ctx.new_page(); errors = []; page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/cameras"); page.wait_for_selector(".cam-tile[data-camera]")
        btn = page.locator(".cam-route-btn")
        check("the route button is on the live tab", btn.count() == 1 and "маршрут 1" in btn.inner_text().lower(), btn.inner_text() if btn.count() else "")
        t = threading.Thread(target=host_job, args=("done",)); t.start()
        btn.click()
        page.wait_for_function("() => /Идёт маршрут/.test(document.querySelector('.cam-route-status').textContent)", timeout=15000)
        check("while it runs the page says so with the seconds left", True, page.locator(".cam-route-status").inner_text())
        check("the button is disabled while it runs", btn.is_disabled())
        req = json.load(open(os.path.join(ptz, "request.json")))
        check("the press wrote a request for tour 1", req.get("tour") == 1 and req.get("id"), req)
        page.wait_for_function("() => /Маршрут пройден/.test(document.querySelector('.cam-route-status').textContent)", timeout=20000)
        check("when done the page says «Маршрут пройден.»", True)
        check("and the button is free again", not btn.is_disabled())
        t.join()
        os.remove(os.path.join(ptz, "request.json"))
        t = threading.Thread(target=host_job, args=("failed", "the camera is busy with another job")); t.start()
        btn.click()
        page.wait_for_function("() => /не запустился/.test(document.querySelector('.cam-route-status').textContent)", timeout=20000)
        check("a failed run shows the reason", "busy" in page.locator(".cam-route-status").inner_text(), page.locator(".cam-route-status").inner_text())
        t.join()
        check("no script errors", not errors, errors[:3]); b.close()
    print("%d of %d checks passed" % (sum(results), len(results))); return 0 if all(results) else 1
if __name__ == "__main__": sys.exit(main(sys.argv[1]))
