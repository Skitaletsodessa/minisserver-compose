#!/usr/bin/env python3
"""Phone UI tests in a REAL headless Chromium with a mobile user agent (Task 30 phase 2, extended in Task 31 Part E).

  python tools/ui_smoke.py BASE_URL HOUSEHOLD_COOKIE ADMIN_COOKIE APP_ADMIN_COOKIE [--screens OUTDIR]

Run it against the scratch stack of tools/ui_scratch.py (synthetic data, synthetic session cookies injected for that origin only), never against the real portal with a real
session. The scratch stack starts its clock at 12:00 (SCRATCH_AT) so the sentences are deterministic. Needs a throw-away venv with `pip install playwright` and
`playwright install chromium-headless-shell` (NOT a dependency of the gateway).

Part 1 (every page, 360x800 and 412x915, text at 100 % and 130 %): no horizontal overflow, every interactive element >= 48 px (the sheet open, the schedule editor, the
device editor included), the bottom navigation is there.
Part 2 (flows at both sizes): the tile sentences; the open-access sheet (nothing chosen -> durations disabled, the exact API calls, the undo snackbar, the read-back, undo);
the schedule editor (day chips, time limits, cap stepper, save with If-Match, the refusal text, the read-back); the child page's collapsed blocks; the roles
(household, admin on the web, admin in an app session)."""
import json
import sys
import time

from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36"
VIEWPORTS = [(360, 800), (412, 915)]
PAGES = ["/home", "/services", "/parental", "/parental#child_a", "/parental#child_a/schedule", "/settings/plant_rack1", "/people", "/audit", "/parental/filters"]
MEASURE = """() => {
  const out = {sw: document.documentElement.scrollWidth, w: innerWidth, small: [], bottomnav: !!document.querySelector('.bottomnav a')};
  document.querySelectorAll('a, button, summary, select, input:not([type=checkbox]):not([type=hidden]), .days label').forEach(e => {
    const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    if (!r.width || !r.height || cs.visibility === 'hidden' || e.closest('[hidden]')) return;
    if (r.height < 47.5 || r.width < 47.5) out.small.push(e.tagName + ':' + (e.textContent || '').trim().slice(0, 20) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height));
  });
  return out;
}"""
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("   [%s]" % detail) if (detail and not cond) else ""), flush=True)
    if not cond:
        FAILS.append(name)
    return cond


def new_ctx(b, base, cookie, w, h, scheme="light"):
    ctx = b.new_context(viewport={"width": w, "height": h}, user_agent=UA, is_mobile=True, has_touch=True, color_scheme=scheme)
    if cookie:
        ctx.add_cookies([{"name": "gw_session", "value": cookie, "url": base}])
    return ctx


def font(page, pct):
    if pct != 100:
        page.evaluate("z => document.documentElement.style.fontSize = z + '%'", pct)
        page.wait_for_timeout(250)


def part1(b, base, cookies):
    for (w, h) in VIEWPORTS:
        for pct in (100, 130):
            ctx = new_ctx(b, base, cookies["admin"], w, h)
            for path in PAGES:
                page = ctx.new_page()
                page.goto(base + path)
                page.wait_for_timeout(900)
                font(page, pct)
                m = page.evaluate(MEASURE)
                tag = "%dx%d font %d%% %s" % (w, h, pct, path)
                problems = []
                if m["sw"] > m["w"]:
                    problems.append("horizontal overflow: scrollWidth %d > %d" % (m["sw"], m["w"]))
                if m["small"]:
                    problems.append("targets under 48 px: %s" % ", ".join(m["small"][:6]))
                if path in ("/home", "/services", "/parental") and not m["bottomnav"]:
                    problems.append("no bottom navigation")
                check("layout " + tag, not problems, "; ".join(problems))
                page.close()
            ctx.close()


def sheet_layout(b, base, cookies):
    for (w, h) in VIEWPORTS:
        for pct in (100, 130):
            ctx = new_ctx(b, base, cookies["household"], w, h)
            page = ctx.new_page()
            page.goto(base + "/parental")
            page.wait_for_selector("[data-open-sheet='child_a']")
            font(page, pct)
            page.click("[data-open-sheet='child_a']")
            page.wait_for_timeout(300)
            page.click(".choice[data-what=youtube]")
            m = page.evaluate(MEASURE)
            check("sheet open %dx%d font %d%%: no overflow, targets >= 48 px" % (w, h, pct), m["sw"] <= m["w"] and not m["small"], str(m["small"][:5]))
            vis = page.evaluate("() => { const s = document.querySelector('.sheet'); const r = s.getBoundingClientRect(); return [r.bottom <= innerHeight + 1, s.scrollHeight > s.clientHeight]; }")
            check("sheet %dx%d font %d%% fits the screen (or scrolls inside itself)" % (w, h, pct), vis[0])
            ctx.close()


def wait_text(page, sel, text, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if text in page.inner_text(sel):
                return True
        except Exception:                                                                    # noqa: BLE001
            pass
        page.wait_for_timeout(1000)
    return False


def open_access_flow(b, base, cookies, w, h):
    tag = "%dx%d" % (w, h)
    ctx = new_ctx(b, base, cookies["household"], w, h)
    page = ctx.new_page()
    calls = []
    page.on("request", lambda r: calls.append((r.method, r.url.replace(base, ""), r.post_data)) if r.method != "GET" else None)
    page.goto(base + "/parental")
    page.wait_for_selector("[data-open-sheet='child_a']")
    txt = page.inner_text("section[data-child='child_a']")
    check(tag + " tile: closed sentence with the opening time", "Интернет ограничен. YouTube и Roblox откроются в 16:00" in txt, txt[:200])
    check(tag + " tile: schedule line", "По расписанию: Пн–Сб, 16:00–19:00" in txt)
    main = page.inner_text("main")
    check(tag + " the always-open line is on the page and names maps", "Всегда открыто: Telegram, погода, карты, службы Android" in main, main[-300:])
    for banned in ("DNS", "snapshot", "Правила применены", "заблокировано запросов", "a-phone", "child_a"):
        check(tag + " default view has no '%s'" % banned, banned not in main)
    check(tag + " the page has no attention banner when all is well", page.is_hidden("#par-attn"))
    page.click("[data-open-sheet='child_a']")
    page.wait_for_selector(".sheet")
    durs = page.query_selector_all(".dur")
    check(tag + " sheet: exactly four duration buttons, all disabled before a choice", len(durs) == 4 and all(d.is_disabled() for d in durs))
    check(tag + " sheet: labels 30 минут / 1 час / 2 часа / До конца дня", [d.query_selector(".dur-name").inner_text() for d in durs] == ["30 минут", "1 час", "2 часа", "До конца дня"])
    check(tag + " sheet: the hint is shown while nothing is chosen", "Сначала выберите, что открыть." in page.inner_text(".sheet"))
    check(tag + " sheet: «Всё» carries its plain sentence", "Откроется весь интернет, кроме того, что вы сами запретили, и защитных списков." in page.inner_text(".choice-all"))
    page.click(".choice[data-what=youtube]")
    check(tag + " sheet: choosing enables the durations and hides the hint", all(not d.is_disabled() for d in page.query_selector_all(".dur")) and page.is_hidden(".sheet-hint"))
    page.click(".choice[data-what=all]")
    check(tag + " sheet: «Всё» is exclusive (YouTube is released)", page.get_attribute(".choice[data-what=youtube]", "aria-pressed") == "false" and page.get_attribute(".choice[data-what=all]", "aria-pressed") == "true")
    page.click(".choice[data-what=youtube]")
    check(tag + " sheet: choosing YouTube releases «Всё»", page.get_attribute(".choice[data-what=all]", "aria-pressed") == "false")
    check(tag + " sheet: «До конца дня» shows the real end time", "до 23:59" in page.inner_text(".dur[data-dur=eod]"))
    page.keyboard.press("Escape")
    check(tag + " sheet: Escape closes it", page.query_selector(".sheet") is None)
    # the real flow: YouTube + Roblox for 30 minutes
    page.click("[data-open-sheet='child_a']"); page.click(".choice[data-what=youtube]"); page.click(".choice[data-what=roblox]")
    n0 = len(calls)
    page.click(".dur[data-dur='30']")
    page.wait_for_selector("#snack:not([hidden])", timeout=15000)
    posts = [c for c in calls[n0:] if c[0] == "POST"]
    check(tag + " the exact API calls: one exception per service, 30 minutes", [json.loads(p[2]) for p in posts] == [{"target": "youtube", "minutes": 30}, {"target": "roblox", "minutes": 30}]
          and all(p[1] == "/v1/parental/children/child_a/exceptions" for p in posts), str(posts))
    check(tag + " no confirmation dialog: the sheet is gone at once", page.query_selector(".sheet") is None)
    snack = page.inner_text("#snack")
    check(tag + " undo snackbar says what and until when, with «Отменить»", "Открыто: YouTube и Roblox — до 12:" in snack and "Отменить" in snack, snack)
    page.wait_for_timeout(500)
    tile = page.inner_text("section[data-child='child_a']")
    check(tag + " the tile shows ONE chip for both services with the time left", "YouTube и Roblox · ещё" in tile and "Закрыть" in tile, tile)
    check(tag + " honest feedback: «Применяется…» first", "Применяется…" in tile)
    check(tag + " the read-back: «Готово: открыто до 12:xx» appears only after the device state is confirmed", wait_text(page, "section[data-child='child_a']", "Готово: открыто до 12:", 60))
    check(tag + " the big sentence now says it is open", "YouTube и Roblox открыты ещё" in page.inner_text("section[data-child='child_a']"))
    # close the chip
    n1 = len(calls)
    page.click("section[data-child='child_a'] .chip button")
    page.wait_for_timeout(800)
    dels = [c for c in calls[n1:] if c[0] == "DELETE"]
    check(tag + " «Закрыть» on the chip cancels both exceptions", len(dels) == 2 and all("/exceptions/" in d[1] for d in dels), str(dels))
    check(tag + " ... and the tile says it is closed again once confirmed", wait_text(page, "section[data-child='child_a']", "Готово: доступ закрыт", 60))
    # undo through the snackbar: YouTube for 1 hour
    page.click("[data-open-sheet='child_a']"); page.click(".choice[data-what=youtube]")
    n2 = len(calls)
    page.click(".dur[data-dur='60']")
    page.wait_for_selector("#snack:not([hidden])", timeout=15000)
    page.click("#snack .snack-undo")
    page.wait_for_timeout(1500)
    check(tag + " 60 minutes is one of the four presets (the POST carried minutes 60)", any(c[0] == "POST" and json.loads(c[2]) == {"target": "youtube", "minutes": 60} for c in calls[n2:]))
    check(tag + " «Отменить» in the snackbar deletes what was just created", any(c[0] == "DELETE" for c in calls[n2:]))
    check(tag + " the snackbar shows «Отменено.»", "Отменено." in page.inner_text("#snack"))
    ctx.close()


def failure_flow(b, base, cookies, w, h):
    tag = "%dx%d" % (w, h)
    ctx = new_ctx(b, base, cookies["household"], w, h)
    page = ctx.new_page()
    page.goto(base + "/parental")
    page.wait_for_selector("[data-open-sheet='child_a']")
    page.route("**/exceptions", lambda route: route.fulfill(status=422, content_type="application/json", body=json.dumps({"error": {"code": "midnight", "message": "x"}}))
               if route.request.method == "POST" else route.continue_())
    page.click("[data-open-sheet='child_a']"); page.click(".choice[data-what=roblox]"); page.click(".dur[data-dur='120']")
    page.wait_for_timeout(1200)
    tile = page.inner_text("section[data-child='child_a']")
    check(tag + " a refused grant says so in words with «Повторить» (no code shown)", "Не получилось: Так поздно открыть не получится" in tile and "Повторить" in tile and "midnight" not in tile, tile)
    page.unroute("**/exceptions")
    page.click("button[data-retry='child_a']")
    check(tag + " «Повторить» sends the request again and the read-back follows", wait_text(page, "section[data-child='child_a']", "Готово: открыто до", 60))
    page.click("section[data-child='child_a'] .chip button")                                   # leave the scratch house as it was found
    wait_text(page, "section[data-child='child_a']", "Готово: доступ закрыт", 60)
    ctx.close()


def schedule_flow(b, base, cookies, w, h):
    tag = "%dx%d" % (w, h)
    ctx = new_ctx(b, base, cookies["household"], w, h)
    page = ctx.new_page()
    calls = []
    page.on("request", lambda r: calls.append((r.method, r.url.replace(base, ""), r.post_data, r.headers)) if r.method != "GET" else None)
    page.goto(base + "/parental#child_a/schedule")
    page.wait_for_selector(".sch-rule")
    body = page.inner_text("main")
    check(tag + " schedule: plain title, no technical words", "Когда открыты YouTube и Roblox" in body and "rule" not in body.lower() and "maps" not in body.lower() and "карты" in body, body[:300])
    chips = page.query_selector_all(".sch-rule .daychip")
    pressed = [c.get_attribute("aria-pressed") for c in chips[:7]]
    check(tag + " schedule: seven day chips, Monday-Saturday on, Sunday off", pressed == ["true"] * 6 + ["false"], str(pressed))
    sels = page.query_selector_all(".sch-rule select")
    opts = [o.inner_text() for o in sels[0].query_selector_all("option")]
    check(tag + " schedule: the start can only be chosen inside 08:00-21:00 in 15-minute steps", opts[0] == "08:00" and opts[-1] == "20:45" and "16:15" in opts, str(opts[:3] + opts[-2:]))
    eopts = [o.inner_text() for o in sels[1].query_selector_all("option")]
    check(tag + " schedule: the end cannot go past 21:00", eopts[-1] == "21:00" and "21:15" not in eopts, str(eopts[-3:]))
    check(tag + " schedule: the week strip shows seven days", len(page.query_selector_all(".wk-day")) == 7)
    # cap stepper: 16:00-19:00 = 180 min; "+" up to 240
    page.click(".cap .step[aria-label='Больше']")
    check(tag + " schedule: the stepper lengthens the window (19:15) and the sentence follows", "Не больше 195 минут в день" in page.inner_text(".cap"))
    page.select_option(".sch-rule select >> nth=1", "20:00")
    check(tag + " schedule: the cap reads 240 minutes at 16:00-20:00 and «+» is then disabled", "Не больше 240 минут в день" in page.inner_text(".cap") and page.is_disabled(".cap .step[aria-label='Больше']"))
    page.click(".sch-rule .daychip >> nth=6")
    check(tag + " schedule: Sunday toggled on", page.get_attribute(".sch-rule .daychip >> nth=6", "aria-pressed") == "true")
    for i in range(7):                                                                         # everything off -> a plain refusal before anything is sent
        if page.get_attribute(".sch-rule .daychip >> nth=%d" % i, "aria-pressed") == "true":
            page.click(".sch-rule .daychip >> nth=%d" % i)
    n0 = len(calls)
    page.click("button.primary:has-text('Сохранить')")
    page.wait_for_timeout(400)
    check(tag + " schedule: no day chosen -> «Выберите хотя бы один день.» and nothing is sent", "Выберите хотя бы один день." in page.inner_text("#sch-msg") and len(calls) == n0)
    for i in range(6):
        page.click(".sch-rule .daychip >> nth=%d" % i)                                         # Monday-Saturday again
    page.click("button.primary:has-text('Сохранить')")
    page.wait_for_selector("#sch-msg:not([hidden])")
    page.wait_for_timeout(600)
    puts = [c for c in calls[n0:] if c[0] == "PUT"]
    check(tag + " schedule: one PUT with If-Match and the edited window", len(puts) == 1 and puts[0][3].get("if-match") and json.loads(puts[0][2])["rules"][0]["end"] == "20:00"
          and json.loads(puts[0][2])["rules"][0]["days"] == [1, 2, 3, 4, 5, 6], str(puts))
    check(tag + " schedule: the read-back sentence", "Сохранено. Пн–Сб, 16:00–20:00." in page.inner_text("#sch-msg"), page.inner_text("#sch-msg"))
    check(tag + " schedule: «Применяется…» then «Готово: устройство получило новое расписание.»", wait_text(page, "main", "Готово: устройство получило новое расписание.", 60))
    page.goto(base + "/parental"); page.wait_for_selector("section[data-child='child_a']")
    check(tag + " the tile sentence follows the new schedule", "По расписанию: Пн–Сб, 16:00–20:00" in page.inner_text("section[data-child='child_a']"))
    # the service's refusal in words (stubbed: the controls cannot produce it)
    page.goto(base + "/parental#child_a/schedule"); page.wait_for_selector(".sch-rule")
    page.route("**/windows", lambda route: route.fulfill(status=422, content_type="application/json", body=json.dumps(
        {"error": {"code": "validation_failed", "message": "x", "details": {"problems": [{"code": "allowed_range", "field": "rule 'a'.end", "message": "22:00 is after the allowed range end 21:00"}]}}}))
        if route.request.method == "PUT" else route.continue_())
    page.click("button.primary:has-text('Сохранить')")
    page.wait_for_timeout(700)
    check(tag + " the refusal is plain: «Дольше 21:00 нельзя — это общее ограничение, его меняет Иван.»", "Дольше 21:00 нельзя — это общее ограничение, его меняет Иван." in page.inner_text("#sch-msg"), page.inner_text("#sch-msg"))
    page.unroute("**/windows")
    # restore the original schedule (the scratch stack is shared by the next flows)
    page.select_option(".sch-rule select >> nth=1", "19:00")
    page.click("button.primary:has-text('Сохранить')")
    wait_text(page, "#sch-msg", "Сохранено", 20)
    # a second window
    page.click("button:has-text('Добавить другие часы')")
    check(tag + " schedule: a second window appears with no day chosen (nothing is lost, nothing is guessed)", len(page.query_selector_all(".sch-rule")) == 2)
    ctx.close()


def collapsed_blocks(b, base, cookies, w, h):
    tag = "%dx%d" % (w, h)
    ctx = new_ctx(b, base, cookies["household"], w, h)
    page = ctx.new_page()
    page.goto(base + "/parental#child_a")
    page.wait_for_selector(".kd")
    main = page.inner_text("main")
    for t in ("Что открывали", "Разрешённые и запрещённые сайты", "Подробности для взрослых", "Расписание"):
        check(tag + " child page lists «%s»" % t, t in main)
    check(tag + " the collapsed blocks are closed and show none of their content", page.eval_on_selector_all("details.kd", "els => els.every(e => !e.open)") and "DNS" not in main and "youtube.com" not in main and "MAC" not in main, main[:400])
    page.click("details.kd-seen > summary")
    page.wait_for_selector(".seen-row", timeout=15000)
    check(tag + " «Что открывали» states that sites, not pages, are shown and that nothing is stored", "а не отдельные страницы" in page.inner_text("details.kd-seen") and "не хранится" in page.inner_text("details.kd-seen"))
    page.click("details.kd-tech > summary")
    page.wait_for_timeout(500)
    tech = page.inner_text("details.kd-tech")
    check(tag + " «Подробности для взрослых» holds the technical truth (rules applied, DNS, counters)", "Правила применены: да" in tech and "DNS" in tech and "Запросов сегодня" in tech, tech[:300])
    page.click("details.kd-sites > summary")
    page.wait_for_timeout(300)
    check(tag + " «Разрешённые и запрещённые сайты» has «Всегда можно» and «Никогда нельзя»", "Всегда можно" in page.inner_text("details.kd-sites") and "Никогда нельзя" in page.inner_text("details.kd-sites"))
    ctx.close()


def roles(b, base, cookies):
    for who, expect_more in (("household", False), ("admin", True), ("app_admin", False)):
        ctx = new_ctx(b, base, cookies[who], 360, 800)
        page = ctx.new_page()
        page.goto(base + "/parental")
        page.wait_for_selector("section[data-child]")
        has_more = page.query_selector("details.kd-more") is not None
        check("role %s: the «Ещё» menu (filters, add device) is %s" % (who, "present" if expect_more else "absent"), has_more == expect_more)
        if expect_more:
            page.click("details.kd-more > summary")
            check("role admin (web): «Списки блокировки» and «Добавить устройство ребёнка» are in «Ещё»", "Списки блокировки" in page.inner_text("details.kd-more") and "Добавить устройство ребёнка" in page.inner_text("details.kd-more"))
            page.goto(base + "/parental#child_a"); page.wait_for_selector(".kd")
            check("role admin (web): the child page has «Ещё» with rename and remove", page.query_selector("details.kd-more") is not None)
            page.click("details.kd-more > summary")
            page.wait_for_timeout(500)
            check("role admin (web): «Переименовать устройство» is offered", "Как назвать устройство" in page.inner_text("details.kd-more"))
        else:
            page.goto(base + "/parental#child_a"); page.wait_for_selector(".kd")
            check("role %s: the child page has no «Ещё» (no rename, no remove)" % who, page.query_selector("details.kd-more") is None)
        if who == "app_admin":
            r = page.request.get(base + "/parental/filters")
            check("role app session: the filters page is refused", r.status in (401, 403), str(r.status))
        ctx.close()
    ctx = new_ctx(b, base, cookies["household"], 360, 800)
    page = ctx.new_page()
    page.goto(base + "/settings/plant_rack1")
    page.wait_for_selector(".rule")
    body = page.inner_text("main")
    check("household editor: plain words (no «Правило», no AM/PM pickers), weekday chips, times in 15-minute steps",
          "Правило" not in body and "Включается в" in body and "Выключается в" in body and len(page.query_selector_all(".rule .daychip")) == 7 and page.query_selector(".rule select") is not None, body[:300])
    check("household editor: the limits are one sentence («… может работать не дольше …»)", "может работать не дольше" in body and "Ограничения:" not in body, body[:400])
    ctx.close()


def screens(b, base, cookies, outdir):
    import os
    os.makedirs(outdir, exist_ok=True)
    shots = [("parental-list", "/parental", None), ("child-page", "/parental#child_a", None), ("child-schedule", "/parental#child_a/schedule", None),
             ("household-schedule", "/settings/plant_rack1", None), ("child-sheet", "/parental", "sheet"), ("child-open", "/parental", "granted")]
    for (w, h) in VIEWPORTS:
        for scheme in ("light", "dark"):
            ctx = new_ctx(b, base, cookies["household"], w, h, scheme)
            for name, path, mode in shots:
                page = ctx.new_page()
                page.goto(base + path)
                page.wait_for_timeout(1200)
                if mode == "sheet":
                    page.click("[data-open-sheet='child_a']"); page.click(".choice[data-what=youtube]"); page.wait_for_timeout(300)
                    page.screenshot(path="%s/%s-%dx%d-%s.png" % (outdir, name, w, h, scheme))
                elif mode == "granted":
                    page.click("[data-open-sheet='child_a']"); page.click(".choice[data-what=youtube]"); page.click(".dur[data-dur='60']")
                    wait_text(page, "section[data-child='child_a']", "Готово: открыто до", 60)
                    page.screenshot(path="%s/%s-%dx%d-%s.png" % (outdir, name, w, h, scheme))
                    page.click("section[data-child='child_a'] .chip button"); wait_text(page, "section[data-child='child_a']", "Готово: доступ закрыт", 60)
                else:
                    page.screenshot(path="%s/%s-%dx%d-%s.png" % (outdir, name, w, h, scheme), full_page=True)
                page.close()
            ctx.close()


def main(argv):
    base = argv[1].rstrip("/")
    cookies = {"household": argv[2], "admin": argv[3], "app_admin": argv[4]}
    outdir = argv[argv.index("--screens") + 1] if "--screens" in argv else None
    with sync_playwright() as p:
        b = p.chromium.launch()
        part1(b, base, cookies)
        sheet_layout(b, base, cookies)
        for (w, h) in VIEWPORTS:
            open_access_flow(b, base, cookies, w, h)
            failure_flow(b, base, cookies, w, h)
            schedule_flow(b, base, cookies, w, h)
            collapsed_blocks(b, base, cookies, w, h)
        roles(b, base, cookies)
        if outdir:
            screens(b, base, cookies, outdir)
        b.close()
    print("\n%d check(s) failed" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
