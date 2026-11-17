#!/usr/bin/env python3
"""Task 35b in a REAL headless Chromium against tools/ui_cameras_scratch.py (scratch gateway, fake camera, synthetic person):
 - «Чётче» now works through the public door (hd_available true): the lens switches to the HD source and the button says «Обычное качество»;
 - when the HD stream ends after the full 30 minutes the lens goes back to the light stream and says why; an earlier end says «недоступна, возможно её уже смотрит другой»;
 - a clip marked damaged in the index shows «Запись повреждена на карте камеры» instead of a player (one damaged lens, and all lenses damaged).

  PLAYWRIGHT_BROWSERS_PATH=... python tools/ui_cameras35b.py OUTDIR"""
import os
import sqlite3
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
results = []


def check(name, ok, info=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(info)) if info else ""), flush=True)


def playing(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('#cam-root .cam-pane video')).map(v => !v.paused && v.currentTime > 0)")


def main(outdir):
    port = open(os.path.join(outdir, "port.txt")).read().strip()
    cookie = open(os.path.join(outdir, "session-household.txt")).read().strip()
    base = "http://127.0.0.1:" + port
    index = os.path.join(outdir, "camera", "index.sqlite")
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 412, "height": 915}, user_agent=UA, is_mobile=True, has_touch=True)
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        requests = []
        page.on("request", lambda r: requests.append(r.url) if "/live" in r.url else None)
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        end = time.time() + 40
        while time.time() < end and not (len(playing(page)) == 3 and all(playing(page))):
            time.sleep(0.3)
        # ── HD on lens 1 ──
        btn = page.locator(".cam-tile[data-camera] .cam-hdbtn").first
        btn.click()
        page.wait_for_function("() => /quality=hd/.test(document.querySelector('.cam-tile[data-camera] video').src)", timeout=5000)
        page.wait_for_timeout(500)
        check("«Чётче» switches the lens to the HD source", any("quality=hd" in u for u in requests), [u for u in requests if "hd" in u][:1])
        check("the button now offers the normal quality", "Обычное" in btn.inner_text(), btn.inner_text())
        # ── the stream ends after the full 30 minutes (the clock is moved 31 minutes ahead, the 'ended' event is what the server's cut-off produces); done at once, before the short scratch media ends by itself ──
        check("(the HD mode is on when the end is simulated)", "Обычное" in btn.inner_text())
        page.evaluate("() => { window.__r = Date.now; Date.now = () => window.__r() + 31 * 60000; document.querySelector('.cam-tile[data-camera] video').dispatchEvent(new Event('ended')); }")
        page.wait_for_function("() => !/quality=hd/.test(document.querySelector('.cam-tile[data-camera] video').src)", timeout=5000)
        end = time.time() + 15
        st = ""
        while time.time() < end:
            st = page.locator(".cam-tile[data-camera] .cam-status").first.inner_text()
            if "30 минут" in st:
                break
            time.sleep(0.3)
        check("after 30 minutes the lens is back on the light stream and says why", "30 минут" in st and "Показываю обычную" in st, st[:90])
        check("the button offers HD again", "Чётче" in btn.inner_text(), btn.inner_text())
        # ── an HD stream that ends EARLY (another person took it, the camera did not deliver): the other message ──
        page.evaluate("() => { Date.now = window.__r; }")                                                                          # the real clock again
        btn.click()
        page.wait_for_function("() => /quality=hd/.test(document.querySelector('.cam-tile[data-camera] video').src)", timeout=5000)
        page.evaluate("() => { document.querySelector('.cam-tile[data-camera] video').dispatchEvent(new Event('ended')); }")
        end = time.time() + 15
        st = ""
        while time.time() < end:
            st = page.locator(".cam-tile[data-camera] .cam-status").first.inner_text()
            if "другой человек" in st:
                break
            time.sleep(0.3)
        check("an HD stream that ends early says it is not available (maybe somebody else has it)", "другой человек" in st and "30 минут" not in st, st[:100])
        errors = [x for x in errors if "503" not in x and "409" not in x]
        check("no console errors on the live tab", not errors, errors[:3])
        # ── damaged clip in the archive ──
        db = sqlite3.connect(index)
        if "corrupt" not in [r[1] for r in db.execute("PRAGMA table_info(clips)")]:
            db.execute("ALTER TABLE clips ADD COLUMN corrupt TEXT")
        db.execute("UPDATE clips SET corrupt=NULL")
        db.execute("UPDATE clips SET corrupt='CABAC_MAX_BIN' WHERE ch=0 AND begin='2026-11-12 13:15:26'")
        db.commit()
        page.goto(base + "/cameras")
        page.wait_for_selector(".cam-tile[data-camera]")
        page.locator("button.cam-tab", has_text="Записи").click()
        page.wait_for_selector("#cam-clips li button")
        page.locator("#cam-clips li button", has_text="13:15:26").click()
        page.wait_for_selector(".cam-syncgrid .cam-lens", timeout=10000)
        page.wait_for_timeout(1500)
        notes = page.locator(".cam-syncgrid .cam-lens .note.err").all_inner_texts()
        check("a damaged clip shows the note instead of a player", notes == ["Запись повреждена на карте камеры."], notes)
        vids = page.locator(".cam-syncgrid .cam-lens video").count()
        check("the two other lenses still have their players", vids == 2, vids)
        db.execute("UPDATE clips SET corrupt='x' WHERE begin LIKE '2026-11-12 13:15:%'")
        db.commit()
        page.locator("#cam-clips li button", has_text="13:30:00").click()
        page.locator("#cam-clips li button", has_text="13:15:26").click()
        page.wait_for_timeout(2500)
        sync = page.evaluate("() => (document.querySelector('.cam-sync-note, .cam-syncnote, #cam-syncnote') || {}).textContent || document.querySelector('#cam-root').innerText")
        check("all three lenses damaged: the note says so, no player", "повреждена" in sync and page.locator(".cam-syncgrid .cam-lens video").count() == 0, sync[-120:])
        db.close()
        b.close()
    print("%d of %d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
