# portal/schedules — schedule service (step a)

Not built yet. Planned layout (ADR 1 and 2, `docs/portal/adr/`):

- `service/` — the `/v1` API (Starlette + uvicorn, SQLite, append-only audit)
- `applier/` — host-side systemd path unit: check_config, reload via webhook, verify, commit generated files, roll back on failure
- `schedctl` — CLI
- `openapi.yaml` — the API contract

Nothing here may change how any device behaves until parity with the hand-written schedules is proven (Task 27 Phase D).
