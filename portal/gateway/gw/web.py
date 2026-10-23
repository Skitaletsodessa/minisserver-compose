"""Server-rendered pages (Russian UI) on top of the same core/API as the app. No inline script, no external resource; a strict CSP is added
to every HTML response. The pages use the web session cookie; state-changing calls from the page's JavaScript go to /v1 with X-CSRF-Token."""
import io
import json
import os
import urllib.parse

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from starlette.responses import HTMLResponse, RedirectResponse, Response, FileResponse
from starlette.routing import Route

from . import config
from .auth import GwError, otpauth_uri
from .strings import S, ERRORS

HERE = os.path.dirname(os.path.abspath(__file__))
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; manifest-src 'self'")
HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "X-Frame-Options": "DENY"}
ENV = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")), autoescape=select_autoescape(["html"]), undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def hhmm(iso_s):
    return iso_s[11:16] if iso_s else ""


ENV.globals["t"] = lambda k, **kw: (S[k].format(**kw) if kw else S[k])
ENV.globals["S"] = S
ENV.globals["hhmm"] = hhmm


def routes(app):
    a = app

    def page(request, name, status=200, **ctx):
        p = ctx.get("me")
        ctx.setdefault("csrf", p["csrf"] if p and p.get("csrf") else "")
        ctx.setdefault("nav_active", name.split(".")[0])
        ctx["i18n_json"] = json.dumps({"S": S, "E": ERRORS}, ensure_ascii=False).replace("<", "\\u003c")
        html = ENV.get_template(name).render(**ctx)
        return HTMLResponse(html, status_code=status, headers=HEADERS)

    def me_or_redirect(request):
        try:
            p = a.authenticate(request, csrf=False)
        except GwError:
            return None, RedirectResponse("/login", status_code=303, headers=HEADERS)
        if p["via"] != "session":
            return None, RedirectResponse("/login", status_code=303, headers=HEADERS)
        return p, None

    def form_of(raw):
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def same_origin(request):
        site = request.headers.get("sec-fetch-site")
        return site in (None, "same-origin", "none")

    async def root(request):
        p, r = me_or_redirect(request)
        return r or RedirectResponse("/home", status_code=303, headers=HEADERS)

    async def login_get(request):
        return page(request, "login.html", me=None, error=None, nav_active="login")

    async def login_post(request):
        if not same_origin(request):
            return page(request, "login.html", status=403, me=None, error=S["login_failed"], nav_active="login")
        f = form_of(await request.body())
        try:
            raw, _sess = a.people.login(f.get("username", ""), f.get("password", ""), f.get("totp", ""), a.ip(request))
        except GwError as e:
            return page(request, "login.html", status=e.status, me=None, error=S["login_wait"] if e.status == 429 else S["login_failed"], nav_active="login")
        r = RedirectResponse("/home", status_code=303, headers=HEADERS)
        r.set_cookie("gw_session", raw, httponly=True, secure=True, samesite="strict", max_age=config.SESSION_ABS_S, path="/")
        return r

    async def logout_post(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        f = form_of(await request.body())
        if f.get("csrf") != p["csrf"]:
            return Response("csrf", status_code=403)
        a.people.logout(p)
        r = RedirectResponse("/login", status_code=303, headers=HEADERS)
        r.delete_cookie("gw_session", path="/")
        return r

    async def home(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        devices = await a.core.devices_view(p)
        return page(request, "home.html", me=p, devices=devices, vacuums=a.core.vacuums_view(p), ha_link=a.ha.link)

    async def settings(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        sid = request.path_params["id"]
        ent = dict(await a._sched_entity(sid))
        ent["name"] = (a.core.devices.get(sid) or {}).get("name") or ent["name"]          # the Russian display name when there is one
        st, js, hd = await a.sched.call("GET", "/v1/schedules/" + sid, a._srole(p), a._acting(p))
        if st != 200:
            raise a.sched.translate_error(st, js)
        js["can_edit"] = a._can_edit(p, ent)
        doc = {"id": sid, "etag": hd.get("ETag") or hd.get("etag"), "schedule": js, "kind": ent["kind"], "name": ent["name"]}
        return page(request, "settings.html", me=p, ent=ent, doc_json=json.dumps(doc, ensure_ascii=False).replace("<", "\\u003c"), can_edit=js["can_edit"])

    async def services(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        return page(request, "services.html", me=p, tiles=a.core.tiles(p))

    async def people(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        if p["role"] != "admin" or p.get("app"):
            return page(request, "message.html", status=403, me=p, title=S["nav_people"], text=ERRORS["web_login_required"] if p.get("app") else "403")
        return page(request, "people.html", me=p, people=a.people.list(), phones=a.people.phones(), roles=("admin", "household", "readonly"))

    async def audit(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        if p["role"] != "admin":
            return page(request, "message.html", status=403, me=p, title=S["nav_audit"], text="403")
        qp = request.query_params
        rows = a.db.audit_rows(100, None, qp.get("action") or None, qp.get("person") or None, None, None)
        return page(request, "audit.html", me=p, rows=rows, f_action=qp.get("action", ""), f_person=qp.get("person", ""))

    async def parental(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        if not p["parent"]:
            return page(request, "message.html", status=403, me=p, title=S["nav_parental"], text="403")
        return page(request, "parental.html", me=p, nav_active="parental")

    async def parental_filters(request):
        p, r = me_or_redirect(request)
        if r:
            return r
        if not p["parent"] or p["role"] != "admin" or p.get("app"):
            return page(request, "message.html", status=403, me=p, title=S["par_f_title"], text=ERRORS["web_login_required"] if p.get("app") and p["role"] == "admin" else "403")
        return page(request, "parental_filters.html", me=p, nav_active="parental")

    async def setup_get(request):
        raw = request.path_params["token"]
        try:
            person, seed = a.people.setup_begin(raw)
        except GwError:
            return page(request, "message.html", status=410, me=None, title=S["setup_title"], text=S["setup_invalid"])
        import segno
        buf = io.BytesIO()
        segno.make(otpauth_uri(seed, person["name"]), error="m").save(buf, kind="svg", scale=4, border=2, dark="#000000", light="#ffffff", xmldecl=False, svgns=True, nl=False)
        return page(request, "setup.html", me=None, person=person, qr=buf.getvalue().decode(), seed=seed, token=raw, error=None, done=False)

    async def setup_post(request):
        raw = request.path_params["token"]
        f = form_of(await request.body())
        try:
            a.people.setup_complete(raw, f.get("password", ""), f.get("code", ""), a.ip(request))
        except GwError as e:
            if e.status == 410:
                return page(request, "message.html", status=410, me=None, title=S["setup_title"], text=S["setup_invalid"])
            person, seed = a.people.setup_begin(raw)
            import segno
            buf = io.BytesIO()
            segno.make(otpauth_uri(seed, person["name"]), error="m").save(buf, kind="svg", scale=4, border=2, dark="#000000", light="#ffffff", xmldecl=False, svgns=True, nl=False)
            return page(request, "setup.html", status=422, me=None, person=person, qr=buf.getvalue().decode(), seed=seed, token=raw, error=ERRORS.get(e.code) or e.message, done=False)
        return page(request, "message.html", me=None, title=S["setup_title"], text=S["setup_done"])

    def apk_info():
        """The APK the operator published in <data>/app/ (portal.apk + portal.json written by the build). The hash is recomputed from the file, never trusted from the json."""
        import hashlib
        d = os.path.join(os.path.dirname(a.db.jsonl), "app")
        apk, meta = os.path.join(d, "portal.apk"), os.path.join(d, "portal.json")
        if not (os.path.isfile(apk) and os.path.isfile(meta)):
            return None
        try:
            m = json.load(open(meta, encoding="utf-8"))
            sha = hashlib.sha256(open(apk, "rb").read()).hexdigest()
            return {"path": apk, "sha256": sha, "size": os.path.getsize(apk), "versionName": str(m.get("versionName", "?")), "versionCode": int(m.get("versionCode", 0)),
                    "cert_sha256": str(m.get("cert_sha256", "?")), "sha_ok": sha == str(m.get("sha256", "")).lower()}
        except (OSError, ValueError):
            return None

    def download_gate(request):
        p, r = me_or_redirect(request)
        if r:
            return None, r
        if p["role"] != "admin" or p.get("app"):                         # admin web session only (ADR 0009): a stolen phone must not hand the app to anybody
            return None, page(request, "message.html", status=403, me=p, title=S["dl_title"], text=ERRORS["web_login_required"] if p.get("app") else "403")
        return p, None

    async def download_page(request):
        p, r = download_gate(request)
        if r:
            return r
        info = apk_info()
        return page(request, "download.html", me=p, apk=info if info and info["sha_ok"] else None, nav_active="download")

    async def download_file(request):
        p, r = download_gate(request)
        if r:
            return r
        info = apk_info()
        if not info or not info["sha_ok"]:
            return Response("not found", status_code=404, media_type="text/plain")
        return FileResponse(info["path"], media_type="application/vnd.android.package-archive", filename="dom-%s.apk" % info["versionName"],
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def releases_page(request):                                  # Task 30c: admin web session only, tailnet only (the door keeps it closed)
        p, r = download_gate(request)
        if r:
            return r
        return page(request, "releases.html", me=p, nav_active="releases")

    async def manifest(request):                                       # Task 30: the PWA. Public on purpose (browsers fetch it without credentials); holds no family data
        doc = {"name": S["app_name"], "short_name": S["app_short"], "lang": "ru", "start_url": "/home", "scope": "/", "display": "standalone", "orientation": "portrait",
               "background_color": "#0f1216", "theme_color": "#1b6ef3",
               "icons": [{"src": "/app/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"}, {"src": "/app/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                         {"src": "/app/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
        return Response(json.dumps(doc, ensure_ascii=False), media_type="application/manifest+json", headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    async def app_icon(request):
        name = request.path_params["name"]
        full = os.path.join(HERE, "static", "app", name)
        if name not in ("icon-192.png", "icon-512.png", "icon-maskable-512.png") or not os.path.isfile(full):
            return Response("not found", status_code=404, media_type="text/plain")
        return FileResponse(full, media_type="image/png", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    async def static(request):
        rel = request.path_params["path"]
        full = os.path.normpath(os.path.join(HERE, "static", rel))
        if not full.startswith(os.path.join(HERE, "static") + os.sep) or not os.path.isfile(full):
            return Response("not found", status_code=404, media_type="text/plain")
        ct = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(os.path.splitext(full)[1], "application/octet-stream")
        return FileResponse(full, media_type=ct, headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    return [
        Route("/", root, methods=["GET"]), Route("/login", login_get, methods=["GET"]), Route("/login", login_post, methods=["POST"]),
        Route("/logout", logout_post, methods=["POST"]), Route("/home", home, methods=["GET"]), Route("/settings/{id}", settings, methods=["GET"]),
        Route("/services", services, methods=["GET"]), Route("/people", people, methods=["GET"]), Route("/audit", audit, methods=["GET"]),
        Route("/parental", parental, methods=["GET"]), Route("/parental/filters", parental_filters, methods=["GET"]), Route("/setup/{token}", setup_get, methods=["GET"]), Route("/setup/{token}", setup_post, methods=["POST"]),
        Route("/app/download", download_page, methods=["GET"]), Route("/app/download.apk", download_file, methods=["GET"]),
        Route("/app/update.json", a.update_manifest, methods=["GET"]), Route("/app/releases", releases_page, methods=["GET"]),
        Route("/app/releases/{code:int}/app.apk", a.update_apk, methods=["GET"]),
        Route("/app/manifest.webmanifest", manifest, methods=["GET"]), Route("/app/{name}", app_icon, methods=["GET"]),
        Route("/static/{path:path}", static, methods=["GET"]),
    ]
