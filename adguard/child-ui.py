#!/usr/bin/env python3
"""Small admin page for the children's DNS policy (Task 20): open YouTube / Roblox / Maps /
everything for a limited time, close early, see what the phone is being blocked on.

Reachable only through Caddy on the tailnet (bound to 127.0.0.1 here), behind HTTP Basic
auth: user `ivan`, password in /home/skit/child-ui-password.txt (mode 600, read on every
request so rotating it needs no restart). Standard library only.
"""
import base64
import datetime
import hmac
import ipaddress
import html
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import childpolicy  # noqa: E402
import audit  # noqa: E402
import devices  # noqa: E402
import overrides  # noqa: E402

BIND = ("127.0.0.1", 8099)
PASSWORD_FILE = Path("/home/skit/child-ui-password.txt")
USER = "ivan"
DURATIONS = [("30 мин", "30"), ("1 час", "60"), ("2 часа", "120"), ("до конца дня", "eod")]
DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--mut:#6a7280;--line:#e2e5ea;--acc:#2563eb;--ok:#16794a;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--fg:#e8eaee;--mut:#9aa3b2;--line:#2c313a;--acc:#6ea0ff;--ok:#4cc38a;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:16px}h1{font-size:1.25rem;margin:.2rem 0 1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
h2{font-size:1rem;margin:0 0 .5rem}.mut{color:var(--mut);font-size:.9rem}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:.4rem 0}
.t{min-width:7.5rem;font-weight:600}
button{font:inherit;padding:8px 12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}button.d{color:var(--bad)}
.ex{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:.35rem 0;border-top:1px solid var(--line)}
.ex:first-of-type{border-top:0}code{font-size:.85rem;word-break:break-all}
.ok{color:var(--ok)}.bad{color:var(--bad)}ul{margin:.3rem 0;padding-left:1.1rem}
a{color:var(--acc)}.sm{padding:4px 9px;font-size:.85rem}.nav{margin:0 0 .8rem}
.dom{padding:.55rem 0;border-top:1px solid var(--line)}.dom:first-of-type{border-top:0}
.badge{display:inline-block;font-size:.75rem;padding:1px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.badge.w{border-color:var(--bad);color:var(--bad)}.acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:.3rem}
input[type=text]{font:inherit;padding:7px 10px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--fg);min-width:0;flex:1 1 12rem}
.tabs a{margin-right:.8rem}.tabs b{margin-right:.8rem}
"""


def esc(s):
    return html.escape(str(s))


def fmt_left(expires, now):
    mins = int((datetime.datetime.fromisoformat(expires) - now).total_seconds() // 60) + 1
    if mins >= 60:
        return f"ещё {mins // 60} ч {mins % 60} мин"
    return f"ещё {mins} мин"


def blocked_recent(ip, limit=12):
    try:
        api = childpolicy.Api()
        q = urllib.parse.quote(ip)
        data = api.call("GET", f"/querylog?limit=400&search={q}")["data"]
    except Exception:  # noqa: BLE001 - page must render even if AGH is unreachable
        return None
    seen, out = set(), []
    for e in data:
        if e.get("client") != ip or not str(e.get("reason", "")).startswith("Filtered"):
            continue
        name = e["question"]["name"].lower().rstrip(".")
        if name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= limit:
            break
    return out


def page(msg=""):
    now = datetime.datetime.now(overrides.TZ)
    sunday, in_window = childpolicy.schedule_state(now)
    active = overrides.active(now)
    parts = ['<h1>Ограничения для детей</h1><div class="nav"><a href="/log">Журнал посещений →</a></div>']
    if msg:
        parts.append(f'<div class="card ok">{esc(msg)}</div>')
    parts.append(
        f'<div class="card"><h2>Сейчас: {DAYS[now.weekday()]} {now:%H:%M}</h2>'
        f'<div>YouTube и Roblox по расписанию: '
        f'<b class="{"ok" if in_window else "bad"}">{"открыты" if in_window else "закрыты"}</b> '
        f'<span class="mut">(пн–сб 16:00–19:00)</span></div>'
        f'<div>Карты по расписанию: <b class="{"bad" if sunday else "ok"}">{"закрыты (воскресенье)" if sunday else "открыты"}</b></div>'
        f'<div class="mut">Telegram, погода и служебные домены телефона открыты всегда.</div></div>')
    for ip, name, _mac in childpolicy.load_clients():
        mine = [e for e in active if e["client"] == ip]
        parts.append(f'<div class="card"><h2>{esc(name)} <span class="mut">{esc(ip)}</span></h2>')
        if mine:
            parts.append('<div class="mut">Временно открыто:</div>')
            for e in mine:
                parts.append(
                    f'<div class="ex"><span><b>{esc(overrides.TARGETS[e["target"]])}</b> · '
                    f'{esc(fmt_left(e["expires"], now))}</span>'
                    f'<form method="post" action="/close"><input type="hidden" name="client" value="{esc(ip)}">'
                    f'<input type="hidden" name="target" value="{esc(e["target"])}">'
                    f'<button class="d">Закрыть</button></form></div>')
        else:
            parts.append('<div class="mut">Временных исключений нет.</div>')
        parts.append('<h2 style="margin-top:.9rem">Открыть на время</h2>')
        for target, label in overrides.TARGETS.items():
            btns = "".join(
                f'<button class="{"p" if target == "all" else ""}" name="minutes" value="{v}">{t}</button>'
                for t, v in DURATIONS)
            parts.append(
                f'<form method="post" action="/open" class="row"><span class="t">{esc(label)}</span>'
                f'<input type="hidden" name="client" value="{esc(ip)}"><input type="hidden" name="target" value="{esc(target)}">{btns}</form>')
        if mine:
            parts.append(f'<form method="post" action="/close"><input type="hidden" name="client" value="{esc(ip)}">'
                         f'<button class="d">Закрыть все исключения</button></form>')
        rec = blocked_recent(ip)
        parts.append('<h2 style="margin-top:.9rem">Что блокируется сейчас</h2>')
        if rec is None:
            parts.append('<div class="mut">Журнал AdGuard недоступен.</div>')
        elif not rec:
            parts.append('<div class="mut">Блокировок в последних запросах нет.</div>')
        else:
            parts.append('<div class="mut">Последние заблокированные домены (если что-то нужное не работает — '
                         'скажи, какой домен добавить в список):</div><ul>'
                         + "".join(f"<li><code>{esc(d)}</code></li>" for d in rec) + "</ul>")
        parts.append("</div>")
    parts.append('<div class="mut">Закрытие по времени — с точностью до минуты; уже идущее видео доиграет, '
                 'кэш DNS на телефоне держит имена ещё несколько минут.</div>')
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Ограничения для детей</title><style>{CSS}</style></head><body><main>"
            + "".join(parts) + "</main></body></html>")


def _shell(body):
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Журнал посещений</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def _list_form(kind, op, domain, back, label, cls=""):
    return (f'<form method="post" action="/list"><input type="hidden" name="kind" value="{kind}">'
            f'<input type="hidden" name="op" value="{op}"><input type="hidden" name="domain" value="{esc(domain)}">'
            f'<input type="hidden" name="back" value="{esc(back)}"><button class="sm {cls}">{label}</button></form>')


def page_log(ip, hours, show, msg=""):
    kids = {c[0]: (c[1], c[2]) for c in childpolicy.load_clients()}
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = next(iter(kids))
    is_kid = ip in kids
    hours = hours if hours in (24, 168) else 24
    show = show if show in ("all", "blocked", "allowed") else "all"
    back = f"/log?ip={urllib.parse.quote(ip)}&h={hours}&show={show}"
    api = childpolicy.Api()
    devs = devices.list_devices(api, kids)
    me = next((d for d in devs if d["ip"] == ip), {"ip": ip, "nick": "", "kid": False, "mac": None,
                                                     "vendor": None, "random": False, "host": "", "queries": 0})
    rows, truncated = audit.collect(api, ip, hours)
    filters = {f["id"]: f["name"] for f in api.call("GET", "/filtering/status").get("filters", [])}
    allow_x, deny_x = childpolicy.extra_list("allow"), childpolicy.extra_list("deny")
    if show == "blocked":
        rows = [r for r in rows if r["blocked"]]
    elif show == "allowed":
        rows = [r for r in rows if r["allowed"]]
    total = len(rows)
    rows = rows[:300]

    def tab(label, h, s):
        sel = (h, s) == (hours, show)
        return f"<b>{label}</b>" if sel else f'<a href="/log?ip={urllib.parse.quote(ip)}&h={h}&show={s}">{label}</a>'

    parts = ['<h1>Журнал посещений</h1><div class="nav"><a href="/">← Ограничения</a></div>']
    if msg:
        parts.append(f'<div class="card ok">{esc(msg)}</div>')
    def dev_line(d):
        who = d["nick"] or d["host"] or "без имени"
        mac = ""
        if d["mac"]:
            what = "случайный/виртуальный MAC" if d["random"] else (d["vendor"] or "производитель неизвестен")
            mac = f' · <code>{esc(d["mac"])}</code> ({esc(what)})'
        use = f'{d["queries"]} запр. за 30 дн.' if d["queries"] else "этот DNS пока не использует"
        return f'<b>{esc(who)}</b> <code>{esc(d["ip"])}</code>{mac} · {use}'

    rows_html = []
    for d in devs:
        mark = '<span class="badge">ребёнок</span> ' if d["kid"] else ""
        cur = " ← открыто" if d["ip"] == ip else ""
        rows_html.append(f'<div class="dom">{mark}{dev_line(d)}{cur}<div class="acts">'
                         f'<a href="/log?ip={urllib.parse.quote(d["ip"])}&h={hours}&show={show}">Журнал</a></div></div>')
    dev_card = ('<div class="card"><h2>Устройства в сети</h2><div class="mut">Чтобы узнать детское устройство: '
                'сверьте IP и MAC с таблицей DHCP на роутере. Ник для устройства задаётся в '
                '<code>child-policy/clients.conf</code> — скажите мне, добавлю.</div>'
                + "".join(rows_html) + '</div>')
    parts.append(dev_card)
    title = me["nick"] or me["host"] or "устройство"
    sub = "" if is_kid else ('<div class="mut">Это не детское устройство: журнал только для просмотра, правила '
                              '«разрешить / блокировать» к нему не применяются.</div>')
    parts.append(
        f'<div class="card"><h2>{esc(title)} <span class="mut">{esc(ip)}</span></h2>{sub}'
        f'<div class="tabs">{tab("24 часа", 24, show)}{tab("7 дней", 168, show)}</div>'
        f'<div class="tabs">{tab("все", hours, "all")}{tab("заблокировано", hours, "blocked")}{tab("разрешено", hours, "allowed")}</div>'
        '<div class="mut" style="margin-top:.5rem">Это <b>домены</b>, а не адреса страниц: HTTPS прячет путь и '
        'параметры, DNS их не видит. «Нет в списках» не значит «безопасно» — только что ни один список не '
        'помечал этот домен. Журнал AdGuard хранит 7 дней.</div></div>')
    cards = []
    for r in rows:
        n = r["name"]
        label, threat = audit.list_label(api, n, filters)
        if threat:
            badge = f'<span class="badge w">⚠ угроза: {esc(label)}</span>'
        elif label:
            badge = f'<span class="badge">реклама/трекер: {esc(label)}</span>'
        else:
            badge = ""
        state = ("заблокирован" if not r["allowed"] else "разрешён" if not r["blocked"] else "и то и другое")
        acts = ""
        if not is_kid:
            pass  # read-only for devices that are not configured as children's
        elif childpolicy.is_under(n, deny_x):
            acts += '<span class="badge w">всегда блокируется</span>' + _list_form("deny", "remove", n, back, "снять блок")
        elif childpolicy.is_under(n, allow_x):
            acts += '<span class="badge">всегда разрешён</span>' + _list_form("allow", "remove", n, back, "снять")
        else:
            if r["blocked"]:
                acts += _list_form("allow", "add", n, back, "Разрешить всегда")
            acts += _list_form("deny", "add", n, back, "Блокировать навсегда", "d")
        cards.append(
            f'<div class="dom"><code>{esc(n)}</code> {badge}<div class="mut">{state} · '
            f'{r["allowed"]} разр. / {r["blocked"]} блок. · последний раз {DAYS[r["last"].weekday()]} {r["last"]:%H:%M}</div>'
            f'<div class="acts">{acts}</div></div>')
    note = ""
    if total > 300 or truncated:
        note = '<div class="mut">Показаны последние 300 доменов' + (' (журнал за период длиннее, чем читаем за раз)' if truncated else '') + '.</div>'
    parts.append(f'<div class="card"><h2>Домены ({total})</h2>{"".join(cards) or "<div class=mut>Пока ничего нет.</div>"}{note}</div>')

    def listing(kind, items, title):
        lis = "".join(f'<div class="ex"><code>{esc(d)}</code>{_list_form(kind, "remove", d, back, "убрать")}</div>' for d in items)
        return f'<h2 style="margin-top:.8rem">{title}</h2>{lis or "<div class=mut>пусто</div>"}'

    parts.append(
        '<div class="card"><h2>Списки вручную (для всех детских устройств)</h2>'
        '<form method="post" action="/list" class="row"><input type="text" name="domain" placeholder="example.com" '
        'autocapitalize=none autocorrect=off required>'
        f'<input type="hidden" name="op" value="add"><input type="hidden" name="back" value="{esc(back)}">'
        '<button class="sm" name="kind" value="allow">Всегда разрешать</button>'
        '<button class="sm d" name="kind" value="deny">Всегда блокировать</button></form>'
        '<div class="mut">Поддомены включены. «Всегда блокировать» действует и тогда, когда открыто «Всё».</div>'
        + listing("allow", allow_x, "Всегда разрешено") + listing("deny", deny_x, "Всегда заблокировано") + "</div>")
    return _shell("".join(parts))


class Handler(BaseHTTPRequestHandler):
    server_version = "child-ui"

    def log_message(self, fmt, *args):  # journald gets one line per request
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authed(self):
        hdr = self.headers.get("Authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                want = PASSWORD_FILE.read_text().strip()
                ok = hmac.compare_digest(user, USER) & hmac.compare_digest(pw, want)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            time.sleep(1)  # slow down guessing
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="child-ui"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return ok

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, msg):
        q = urllib.parse.quote(msg)
        self.send_response(303)
        self.send_header("Location", f"/?m={q}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._authed():
            return
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path == "/log":
            try:
                h = int(q.get("h", "24"))
            except ValueError:
                h = 24
            try:
                return self._send(200, page_log(q.get("ip", ""), h, q.get("show", "all"), q.get("m", "")[:200]))
            except Exception as e:  # noqa: BLE001
                return self._send(502, f"AdGuard Home недоступен или ответил ошибкой: {e!r}", "text/plain; charset=utf-8")
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        self._send(200, page(q.get("m", "")[:200]))

    def do_POST(self):
        if not self._authed():
            return
        # CSRF: the browser re-sends Basic credentials on its own, so a form on any other
        # page could otherwise submit here. Same-origin Origin/Referer is required.
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        if urllib.parse.urlparse(origin).netloc != host:
            return self._send(403, "bad origin", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(min(length, 4096)).decode()).items()}
        if self.path == "/list":
            kind, op, domain = form.get("kind", ""), form.get("op", ""), form.get("domain", "")
            back = form.get("back", "/log")
            if kind not in ("allow", "deny") or op not in ("add", "remove") or not back.startswith("/log"):
                return self._send(400, "bad request", "text/plain")
            try:
                childpolicy.extra_change(kind, domain, add=(op == "add"))
                childpolicy.run()
            except ValueError as e:
                return self._send(400, str(e), "text/plain; charset=utf-8")
            except Exception as e:  # noqa: BLE001
                return self._send(502, f"Ошибка: {e!r}", "text/plain; charset=utf-8")
            dom = domain.strip().lower().rstrip(".")
            msg = {("allow", "add"): f"{dom}: теперь всегда разрешён.", ("deny", "add"): f"{dom}: теперь всегда блокируется.",
                   ("allow", "remove"): f"{dom}: убран из «всегда разрешено».", ("deny", "remove"): f"{dom}: убран из «всегда заблокировано»."}[(kind, op)]
            self.send_response(303)
            self.send_header("Location", back.split("&m=")[0] + "&m=" + urllib.parse.quote(msg))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ips = [c[0] for c in childpolicy.load_clients()]
        client, target = form.get("client", ""), form.get("target") or None
        if client not in ips or (target and target not in overrides.TARGETS):
            return self._send(400, "bad request", "text/plain")
        try:
            if self.path == "/open":
                if not target:
                    return self._send(400, "bad request", "text/plain")
                m = form.get("minutes", "")
                minutes = None if m == "eod" else int(m)
                if minutes is not None and not 1 <= minutes <= 24 * 60:
                    return self._send(400, "bad request", "text/plain")
                exp = overrides.add(client, target, minutes)
                msg = f"Открыто: {overrides.TARGETS[target]} до {exp:%H:%M}."
            elif self.path == "/close":
                n = overrides.remove(client, target)
                msg = f"Закрыто исключений: {n}."
            else:
                return self._send(404, "not found", "text/plain")
            childpolicy.run()
        except Exception as e:  # noqa: BLE001
            return self._redirect(f"Ошибка: {e!r}"[:200])
        self._redirect(msg)


if __name__ == "__main__":
    ThreadingHTTPServer(BIND, Handler).serve_forever()
