#!/usr/bin/env python3
"""Phone smoke test (Task 30 phase 2): loads every family page of the portal in a real headless Chromium at 360x800 and 412x915 (also with the text at 130 %) and
FAILS on horizontal overflow, on interactive elements smaller than 48x48 px (checkboxes are measured through their label) and on a missing bottom navigation.

  python tools/ui_smoke.py BASE_URL SESSION_COOKIE_VALUE
Run it against the scratch stack of tools/ui_scratch.py (synthetic data, a synthetic session cookie injected for that origin), never against the real portal with a
real session. Needs `pip install playwright` + `playwright install chromium-headless-shell` (a throw-away venv; it is NOT a dependency of the gateway)."""
import sys

from playwright.sync_api import sync_playwright

PAGES = ["/home", "/services", "/parental", "/settings/plant_rack1", "/people", "/audit", "/parental/filters"]
VIEWPORTS = [(360, 800), (412, 915)]
MEASURE = """() => {
  const out = {sw: document.documentElement.scrollWidth, w: innerWidth, small: [], bottomnav: !!document.querySelector('.bottomnav a')};
  document.querySelectorAll('a, button, summary, select, input:not([type=checkbox]):not([type=hidden]), .days label').forEach(e => {
    const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    if (!r.width || !r.height || cs.visibility === 'hidden' || e.closest('[hidden]') || e.disabled && false) return;
    if (r.height < 47.5 || r.width < 47.5) out.small.push(e.tagName + ':' + (e.textContent || '').trim().slice(0, 20) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
  });
  return out;
}"""


def main(base, cookie):
    fails = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for (w, h) in VIEWPORTS:
            for zoom in (100, 130):
                ctx = b.new_context(viewport={"width": w, "height": h}, user_agent="Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36", is_mobile=True, has_touch=True)
                ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
                for path in PAGES:
                    page = ctx.new_page()
                    page.goto(base + path)
                    page.wait_for_timeout(600)
                    if zoom != 100:
                        page.evaluate("z => document.documentElement.style.fontSize = z + '%'", zoom)
                        page.wait_for_timeout(200)
                    m = page.evaluate(MEASURE)
                    tag = "%dx%d font %d%% %s" % (w, h, zoom, path)
                    problems = []
                    if m["sw"] > m["w"]:
                        problems.append("horizontal overflow: scrollWidth %d > %d" % (m["sw"], m["w"]))
                    if m["small"] and zoom == 100:
                        problems.append("targets under 48 px: %s" % ", ".join(m["small"][:6]))
                    if path in ("/home", "/parental", "/services") and not m["bottomnav"]:
                        problems.append("no bottom navigation")
                    print(("FAIL " if problems else "PASS ") + tag + ("  -> " + "; ".join(problems) if problems else ""))
                    fails += [tag] if problems else []
                    page.close()
                ctx.close()
        b.close()
    print("\n%d page(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1].rstrip("/"), sys.argv[2]))
