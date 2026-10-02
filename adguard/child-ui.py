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
import html
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import childpolicy  # noqa: E402
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
    parts = [f"<h1>Ограничения для детей</h1>"]
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
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        msg = urllib.parse.parse_qs(u.query).get("m", [""])[0][:200]
        self._send(200, page(msg))

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
