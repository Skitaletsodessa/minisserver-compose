# portal — Home Portal (code and config)

Overview, requirements, sequence a–e and the decision records live in the docs repo: `docs/portal/README.md` (`minisserver-docs`). This directory holds only what runs on the server.

| Directory | Step | State |
|---|---|---|
| `schedules/` | (a) one source of truth for schedules: service, host-side applier, `schedctl`, OpenAPI file | skeleton only; built in Task 27 Phase C after Ivan approves ADR 1–3 |
| `gateway/` | (b) gateway, dashboard, service tiles | placeholder, not started |

Rules that apply here: tailnet only (loopback + Caddy tailnet site, never Funnel/Tunnel/router port); Home Assistant stays the executor of every schedule; secrets only in git-ignored mode-600 files with a committed `.env.example`; images by digest, dependencies pinned with hashes.
