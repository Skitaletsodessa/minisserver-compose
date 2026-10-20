# portal/schedules — the single source of truth for schedules (Task 27, step a)

One service holds every schedule of the house (plugs, vacuum plans; the child policy is modelled and imported read-only). Everything
else reads it: the Home Assistant configuration is **rendered** from it, `ha-alerts` reads its snapshot, the future portal UI will call
its API. Decisions: `docs/portal/adr/0001-0003` (docs repo). Report: `docs/report-27.md`.

```
you / portal / schedctl ──HTTP /v1──> portal-schedules (container, 127.0.0.1:16444, Caddy tailnet-only :16443)
                                         │ validates against the guardrails (REJECTS, never clamps), audits, writes files:
                                         ▼
                      /srv/apps/portal/schedules/out/{schedules.json, render/, render.json, READY}
                                         │ systemd path unit (portal-apply.path) + 5-min timer
                                         ▼
                      portal-apply (host, user skit): files -> homeassistant/generated/, HA check_config, local webhook reload,
                      VERIFY through the recorder (aliases carry a content hash), git commit+push, apply-status.json
                                         ▼
                      Home Assistant EXECUTES the generated automations and scripts. If the service, the applier or the portal are
                      stopped or being updated, HA keeps firing every edge from what it has loaded.
```

## Files

| Path | What |
|---|---|
| `sched/model.py` | domain: rules, guardrails, DST rule, edges, effective state (pure) |
| `sched/render.py` | HA YAML from the data (deterministic; hash in every alias) |
| `sched/store.py` | SQLite, hash-chained append-only audit, snapshot + render output |
| `sched/api.py`, `openapi.yaml` | the `/v1` API; the spec is checked against every response in the tests |
| `sched/child_render.py` | child-policy rules block (read-only phase) |
| `schedctl` | CLI (`schedctl --help`); token in `~/.config/schedctl/token` (mode 600) |
| `applier/portal_apply.py`, `applier/systemd/` | the host-side applier and its units (`applier/install.sh`) |
| `tools/cutover.py` | per-entity cutover / rollback; `hand_edit.py`, `coverage_check.py`, `parity_edges.py`, `shadow_compare.py`, `child_policy_parity.py`, `import_child_policy.py` |
| `baseline/` | the hand-written `automations.yaml` / `scripts.yaml` as they were before the project (the parity reference) |
| `tests/` | `python -m unittest discover -s tests -t .` (97 tests); `tests/integration_scratch_ha.py` = rehearsal against a real HA in a throw-away container |
| `/srv/apps/portal/schedules/` | data (outside git): `schedules.db`, `audit.jsonl`, `out/`, `apply-status.json` |
| `/srv/apps/portal/applier/` | applier state: `webhook-id` (secret, 600), `cutover/` (previous hand-written files, rollback stack) |

Tests need PyYAML etc.: `/srv/apps/portal/venv-test` (rebuild: `python3 -m venv ...; pip install --require-hashes -r requirements-test.txt`).
The image itself has 7 hash-pinned packages (`requirements.txt`); `pin-requirements.py` regenerates the pins.

## Daily use

```bash
schedctl entities                      # what exists, managed = rendered into HA
schedctl show plant_rack1              # rules + guardrails + ETag
schedctl set-window plant_rack1 day 07:30 20:00     # change a window; reaches HA in ~10 s (render -> check -> reload -> verify)
schedctl preview plant_rack1 file.json # validate + diff + next 7 days, changes nothing
schedctl undo plant_rack1              # revert the last change as a new change (guardrails apply to it too)
schedctl exception add plant_rack1 skip_date 2026-11-01
schedctl health                        # in_sync = HA runs exactly what was rendered
schedctl audit plant_rack1 10
```

A refused change prints every reason, e.g. `[max_on] rule 'day': the window is 870 min, the guardrail max_on_minutes is 810`.
Guardrails (admin only): `schedctl guardrails ENTITY file.json`. Numbers confirmed by Ivan 2026-10-19: dryer 120 min / 06:00-21:00 / gap 30;
UV lamp 435 / 08:00-19:00 / 30; plant lights 810 / 05:00-22:00 / 360; vacuum plans 08:00-22:00, gap 180, max 3 per day.
**The fail-safe OFF** (generated per plug): every minute, a plug that has been ON longer than `max_on_minutes` is switched OFF — also a hand-started run.
After an HA restart the age counts from the restart (a run can be extended by at most one restart).

## When something is wrong

| Symptom | Look at | Do |
|---|---|---|
| Telegram: "portal: render #N NOT applied" | `cat /srv/apps/portal/schedules/apply-status.json`, `journalctl -u portal-apply` | the applier already rolled HA back to the previous files; fix the cause or `schedctl undo ENTITY`; retry: `python3 applier/portal_apply.py --force` |
| "portal: Home Assistant does NOT run the rendered schedule" | `schedctl health`; the difference is in `apply-status.json` | usually HA restarted with a stale mount: `docker exec homeassistant ls /config/generated`; then run the applier once |
| a lamp does something unexpected | `schedctl effective ENTITY`, HA logbook | remember hand switch changes and on-plug (Smart Life) timers are outside the portal |
| service down | `docker logs portal-schedules`, `ss -tlnp \| grep 1644` | `docker compose up -d`; HA is unaffected meanwhile |
| need the OLD behaviour right now | — | `python3 tools/cutover.py rollback all` (hand-written files = baseline + reload hook; nothing managed) |

**Rollback (written before the first cutover):** `tools/cutover.py rollback ENTITY` restores the exact previous hand-written files for the *most recent*
cutover, sets `managed=off`, lets the applier remove the generated items, checks the coverage invariant; `rollback all` goes back to the baseline.
The invariant `coverage_check.py`: every original edge is produced exactly once, by the remaining hand-written files or by `generated/`, never both.

## Security

Loopback + Caddy on the tailnet address only (`portal-watch` alerts, tier A, if any socket is elsewhere). Bearer tokens (SHA-256 at rest, shown once, rate-limited
per client IP), roles admin / household / readonly, children have no access. No Home Assistant admin token exists anywhere: the applier triggers a
`local_only` webhook (id in `secrets.yaml` + `/srv/apps/portal/applier/webhook-id`, both mode 600, both in the encrypted backup).
Create a token: `docker exec portal-schedules python -m sched.admin token-create NAME ROLE /data/FILE` (first admin) or `schedctl token create NAME ROLE`.

## Timed runs — "включить на N минут" (Task 29 Part 1)

A run is a first-class resource (table `runs`, `/v1/runs`, `schedctl run …`), not a schedule exception: it needs history (the gap between runs, the alert watcher), the real start (extensions are counted from it), who started it and an `armed` state. `skip_date` / `override_until` stay exceptions (`override_until` is still model/API only).

- **Guardrails apply exactly as to a schedule, in the service, never clamped:** `minutes` ≤ `max_on_minutes` counted from the real start *including every extension*; start and end inside `allowed_from..allowed_to` on one day; `min_gap_minutes` after the previous run; no overlap with a scheduled window of that plug (a run is for the gaps; a skipped date has no window); never inside 02:00–03:59. A run ends on a **whole minute** (the next one after start + N).
- **The end is generated into Home Assistant** (`sch_<entity>_runend_<run>` in the plug's automation file): every minute from `ends_at` for 20 min it switches the plug OFF if it has been ON since before the end. No dependence on the gateway or this service at that time. Window OFF edges do not cut a run; **the reconcile script never turns a plug ON because of a run** (it only refrains from switching OFF while a run is within its time). The fail-safe OFF is untouched.
- `armed` = the applier verified that HA loaded a render containing this run. **The caller must switch the plug ON only when armed** (the gateway does; it withdraws the run and switches nothing if the end cannot be armed in 30 s).
- The background loop expires runs 20 min after their end (they stop being rendered, the HA automation disappears). A run past its end is `ended` for every reader at once.
- `ha-alerts` reads the runs from the snapshot (`runs`, `recent_runs`): a run is a deliberate window, and the "still ON after the end" ceilings count from the run's real end (tier A 30 min for the dryer/UV).
- Proofs: `tests/test_runs.py` (service + render), `tests/integration_runs.py` (real HA in a throw-away container: end within a minute with the service idle, HA stopped across the end, HA restarted mid-run, OFF plug never switched ON, cancel, extend, expiry — 28 checks).
