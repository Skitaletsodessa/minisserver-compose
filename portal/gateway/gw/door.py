"""Task 34 phase 4 / ADR 0009 amendment "single app gate": which routes the PUBLIC door (Tailscale Funnel -> Caddy :18080) may reach.

The door site of Caddy sets `X-Portal-Door: funnel` on every request it forwards; every tailnet site removes that header (ADR 0009, Caddyfile). The gateway listens on
loopback only, so Caddy is the only client that can set it. A request that carries it is served ONLY if its route is flagged `allow` here, and a route that is
not in this table at all is `deny` (default deny: a new route is invisible from the internet until someone writes a line here and the test accepts it).

  door  "allow" | "deny"
  auth  for allow: "none"  the route is public or authenticates itself (sign-in, the update paths, static assets)
                   "app"   an APP credential is required: a device token (Bearer) or an app session (device-bound, fingerprint-unlocked); a WEB session is refused
  rate  the limit class (Caddy zone / gateway counter) the route belongs to; documentation of the ADR matrix, enforced where noted there

Order matters: the first matching entry wins, the generic `/app/{name}` is last."""
import re

# (method, path template, door, auth, rate)
TABLE = [
    # ── the app's own gate: sign-in, session exchange, updates, assets ──
    ("POST", "/v1/app/login", "allow", "none", "login"),
    ("POST", "/v1/app/session", "allow", "none", "session"),
    ("POST", "/v1/app/logout", "allow", "none", "session"),
    ("POST", "/v1/app/update-key", "allow", "none", "session"),
    ("GET", "/app/update.json", "allow", "none", "update_manifest"),
    ("GET", "/app/releases/{code:int}/app.apk", "allow", "none", "update_apk"),
    ("GET", "/app/manifest.webmanifest", "allow", "none", "default"),
    ("GET", "/app/download", "deny", "-", "-"),
    ("GET", "/app/download.apk", "deny", "-", "-"),
    ("GET", "/app/releases", "deny", "-", "-"),
    ("GET", "/app/{name}", "allow", "none", "default"),                     # the three icon files only (the handler refuses any other name)
    ("GET", "/static/{path:path}", "allow", "none", "default"),
    ("GET", "/", "allow", "none", "default"),
    # ── admin and web-login surfaces: never through the door ──
    ("GET", "/login", "deny", "-", "-"), ("POST", "/login", "deny", "-", "-"), ("POST", "/logout", "deny", "-", "-"),
    ("GET", "/setup/{token}", "deny", "-", "-"), ("POST", "/setup/{token}", "deny", "-", "-"),
    ("POST", "/v1/auth/login", "deny", "-", "-"), ("POST", "/v1/auth/logout", "deny", "-", "-"), ("GET", "/v1/auth/session", "deny", "-", "-"),
    ("POST", "/v1/enrol", "deny", "-", "-"),
    ("GET", "/v1/health", "deny", "-", "-"),
    ("GET", "/v1/releases", "deny", "-", "-"), ("POST", "/v1/releases/stop", "deny", "-", "-"), ("POST", "/v1/releases/resume", "deny", "-", "-"),
    ("POST", "/v1/releases/{code:int}/publish", "deny", "-", "-"),
    ("GET", "/v1/people", "deny", "-", "-"), ("POST", "/v1/people", "deny", "-", "-"), ("PATCH", "/v1/people/{id}", "deny", "-", "-"),
    ("POST", "/v1/people/{id}/enrolment-codes", "deny", "-", "-"), ("POST", "/v1/people/{id}/setup-link", "deny", "-", "-"),
    ("GET", "/v1/phones", "deny", "-", "-"), ("DELETE", "/v1/phones/{id}", "deny", "-", "-"),
    ("GET", "/v1/audit", "deny", "-", "-"), ("GET", "/audit", "deny", "-", "-"), ("GET", "/people", "deny", "-", "-"),
    ("GET", "/parental/filters", "deny", "-", "-"),
    ("GET", "/v1/parental/filter-lists", "deny", "-", "-"), ("POST", "/v1/parental/filter-lists", "deny", "-", "-"),
    ("GET", "/v1/parental/filter-lists/catalogue", "deny", "-", "-"), ("PATCH", "/v1/parental/filter-lists/{id}", "deny", "-", "-"),
    ("DELETE", "/v1/parental/filter-lists/{id}", "deny", "-", "-"), ("POST", "/v1/parental/filter-lists/{id}/refresh", "deny", "-", "-"),
    ("POST", "/v1/parental/filter-lists/{id}/revert", "deny", "-", "-"),
    ("GET", "/v1/parental/candidates", "deny", "-", "-"), ("GET", "/v1/parental/removed-children", "deny", "-", "-"),
    ("POST", "/v1/parental/removed-children/{rid}/restore", "deny", "-", "-"),
    ("POST", "/v1/parental/children", "deny", "-", "-"), ("PATCH", "/v1/parental/children/{id}", "deny", "-", "-"), ("DELETE", "/v1/parental/children/{id}", "deny", "-", "-"),
    ("GET", "/v1/parental/children/{id}/setup-checklist", "deny", "-", "-"),
    # ── what the parents' and household's app uses (an app credential is required; the routes themselves keep their role rules) ──
    ("GET", "/home", "allow", "app", "default"), ("GET", "/services", "allow", "app", "default"), ("GET", "/settings/{id}", "allow", "app", "default"),
    ("GET", "/parental", "allow", "app", "default"), ("GET", "/cameras", "allow", "app", "default"),
    ("GET", "/v1/capabilities", "allow", "app", "default"), ("GET", "/v1/services", "allow", "app", "default"), ("GET", "/v1/events", "allow", "app", "events"),
    ("GET", "/v1/devices", "allow", "app", "default"), ("GET", "/v1/devices/{id}", "allow", "app", "default"), ("PATCH", "/v1/devices/{id}", "allow", "app", "default"),
    ("POST", "/v1/devices/{id}/run", "allow", "app", "default"), ("DELETE", "/v1/devices/{id}/run", "allow", "app", "default"),
    ("POST", "/v1/devices/{id}/run/extend", "allow", "app", "default"), ("POST", "/v1/devices/{id}/switch", "allow", "app", "default"),
    ("GET", "/v1/vacuums", "allow", "app", "default"), ("POST", "/v1/vacuums/{id}/scenarios/{scenario}/start", "allow", "app", "default"),
    ("POST", "/v1/vacuums/{id}/pause", "allow", "app", "default"), ("POST", "/v1/vacuums/{id}/resume", "allow", "app", "default"), ("POST", "/v1/vacuums/{id}/dock", "allow", "app", "default"),
    ("GET", "/v1/schedules/{id}", "allow", "app", "default"), ("PUT", "/v1/schedules/{id}", "allow", "app", "default"), ("POST", "/v1/schedules/{id}/preview", "allow", "app", "default"),
    ("POST", "/v1/schedules/{id}/undo", "allow", "app", "default"), ("GET", "/v1/schedules/{id}/exceptions", "allow", "app", "default"),
    ("POST", "/v1/schedules/{id}/exceptions", "allow", "app", "default"), ("DELETE", "/v1/exceptions/{xid}", "allow", "app", "default"),
    ("GET", "/v1/parental/children", "allow", "app", "default"), ("GET", "/v1/parental/children/{id}", "allow", "app", "default"),
    ("GET", "/v1/parental/children/{id}/windows", "allow", "app", "default"), ("PUT", "/v1/parental/children/{id}/windows", "allow", "app", "default"),
    ("POST", "/v1/parental/children/{id}/windows/preview", "allow", "app", "default"), ("POST", "/v1/parental/children/{id}/windows/undo", "allow", "app", "default"),
    ("GET", "/v1/parental/children/{id}/exceptions", "allow", "app", "default"), ("POST", "/v1/parental/children/{id}/exceptions", "allow", "app", "default"),
    ("DELETE", "/v1/parental/children/{id}/exceptions/{xid}", "allow", "app", "default"), ("GET", "/v1/parental/children/{id}/activity", "allow", "app", "default"),
    ("GET", "/v1/parental/children/{id}/domains", "allow", "app", "default"), ("POST", "/v1/parental/children/{id}/domains", "allow", "app", "default"),
    ("DELETE", "/v1/parental/children/{id}/domains", "allow", "app", "default"), ("POST", "/v1/parental/children/{id}/domains/undo", "allow", "app", "default"),
    ("GET", "/v1/parental/health", "allow", "app", "default"),
    # ── cameras (Task 34): sub streams only (the gateway serves nothing else), archive clips with ranges ──
    ("GET", "/v1/cameras", "allow", "app", "camera"), ("GET", "/v1/cameras/archive/days", "allow", "app", "camera"),
    ("GET", "/v1/cameras/archive/clips", "allow", "app", "camera"), ("GET", "/v1/cameras/archive/at", "allow", "app", "camera"), ("GET", "/v1/cameras/archive/step", "allow", "app", "camera"), ("GET", "/v1/cameras/archive/clips/{cid:int}/video", "allow", "app", "camera_clip"),
    ("GET", "/v1/cameras/{id}/snapshot", "allow", "app", "camera_snapshot"), ("GET", "/v1/cameras/{id}/live", "allow", "app", "camera_live"),
]

# gateway-side counters for door requests of the camera module: (class) -> (events, window seconds), per person+device (Caddy adds per-IP zones in front)
DOOR_LIMITS = {"camera_snapshot": (240, 300), "camera_live": (6, 600), "camera_clip": (60, 600)}

HEADER = b"x-portal-door"
VALUE = b"funnel"


def _rx(template):
    out, i = "", 0
    for m in re.finditer(r"\{([a-z_]+)(?::([a-z]+))?\}", template):
        out += re.escape(template[i:m.start()])
        kind = m.group(2)
        out += r"[0-9]+" if kind == "int" else (r".+" if kind == "path" else r"[^/]+")
        i = m.end()
    return re.compile("^" + out + re.escape(template[i:]) + "$")


_COMPILED = [(m, _rx(p), p, d, a, r) for (m, p, d, a, r) in TABLE]


def decide(method, path):
    """-> (door, auth, rate, template). Default deny: a route that is not in TABLE is ('deny', '-', '-', None)."""
    method = "GET" if method == "HEAD" else method
    for m, rx, tmpl, d, a, r in _COMPILED:
        if m == method and rx.match(path):
            return d, a, r, tmpl
    return "deny", "-", "-", None


def templates():
    """(method, template) of every entry, for the completeness test against the real route table."""
    return {(m, p) for (m, p, _d, _a, _r) in TABLE}


def caddy_prefixes():
    """Path matchers the door's Caddy site forwards (a coarse filter in front of the gateway's own table): the exact path of every ALLOWed entry without a parameter, and
    `<prefix up to the first parameter>*` for the ones with a parameter. Denied entries are never listed (the gateway also denies them: two layers)."""
    out = set()
    for (_m, p, d, _a, _r) in TABLE:
        if d != "allow":
            continue
        out.add(p if "{" not in p else p.split("{", 1)[0] + "*")
    return sorted(out)


class DoorMiddleware:
    """Pure ASGI: a request that carries the door header is answered 404 (the same body as an unknown path) unless its route is flagged allow; allowed requests carry the
    flag's auth requirement in scope['portal_door'] for App.authenticate."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        door_hdr = any(k == HEADER and v == VALUE for k, v in scope.get("headers", []))
        if door_hdr:
            d, a, r, tmpl = decide(scope.get("method", "GET"), scope["path"])
            if d != "allow":
                body = b"not found"
                await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            scope["portal_door"] = {"auth": a, "rate": r, "route": tmpl}
        await self.app(scope, receive, send)
