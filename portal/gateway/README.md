# portal/gateway — Home Portal gateway (Task 28, step b)

One service in front of Home Assistant and the schedule service, used by the web UI today and the Android app (Task 30) later. **The API is the product** (`openapi.yaml`); the web UI is a thin server-rendered client of it. Design and decisions: `docs/portal/adr/0007-people-accounts-and-device-enrolment.md` and `docs/report-28.md` in the docs repo.

**Rules that matter operationally**
- Home Assistant stays the executor of every schedule. The gateway only *asks* HA to switch a plug / start a script, then reads the state back; it never owns a schedule (the schedule service does) and nothing in the house depends on it. Gateway stopped ⇒ the house keeps running exactly as before.
- Tailnet only: the process binds `127.0.0.1:17444` (the code refuses anything else); Caddy publishes `https://minisserver.tail6bf4d5.ts.net:17443` on the tailnet address only. No Funnel, no tunnel, no router port. After every start/restart of the gateway or Caddy run `./check-bind.sh` (reads the kernel socket table).
- Secrets are files in `/srv/apps/portal/gateway-secrets` (mode 600, mounted read-only at `/secrets`): `ha-token` (dedicated **non-admin** HA user `portal-gateway`, long-lived token), `sched-admin`, `sched-household` (schedule-service tokens flagged *gateway*). They are in the restic backup set (Tier-0 material), never in git, never printed.
- HA allow-list inside the gateway: `switch.turn_on/turn_off`, `script.turn_on/turn_off`, `vacuum.pause/return_to_base`, and only for entities of the six plugs, the two vacuum scripts and the two robots.

**Files**
| | |
|---|---|
| `gw/` | the service (Starlette + uvicorn, SQLite, Jinja2 templates, own CSS/JS/SVG icons, no external resource) |
| `openapi.yaml` | the contract; `tests/` validate every response against it and fail if an operation is never exercised |
| `Dockerfile`, `compose.yaml` | image by digest, hash-pinned dependencies, non-root, read-only rootfs, `cap_drop ALL`, 256 MB, host network (loopback only) |
| `gwctl` | administration on the server (`bootstrap`, `person …`, `setup-link`, `reset-credentials`, `enrol-code`, `phones`, `phone-revoke`, `audit`, `verify`, `clean-out`); prints no secret |
| `check-bind.sh` | the `ss` assertion |
| `tools/ha_create_user.py` | one-off, HA stopped: creates the non-admin user and token (refuses if there is no owner yet, or if the user would come out admin) |
| `tools/ha_real_rehearsal.py` | rehearsal of the above plus the gateway's HA client against a real HA container with no devices |

**Tests** (131 + contract coverage): `cd portal/gateway && python -m unittest discover -s tests` in a venv with `requirements-test.txt` (uses the *real* schedule service in-process and a fake HA; synthetic people only; no Telegram, no secret).

**Update**: edit, run the tests, `docker compose build && docker compose up -d`, then `./check-bind.sh`. Change a dependency: edit `requirements.in`, run `pin-requirements.py`.
