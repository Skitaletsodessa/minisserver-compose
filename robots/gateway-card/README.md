# Gateway card for the ETA Tiger (Task 32) — DORMANT, NOT DEPLOYED

A robot without a plan script gets a card (start / pause / dock / locate) behind the feature-flag file `<gateway data>/robots.json`
(`{"tiger": {"enabled": true, "vacuum": "vacuum.<entity>"}}`; or `$GW_ROBOTS_FILE`). Without the file nothing changes: the H40 cards are untouched
(the whole existing suite passes with this patch: 322 tests = 311 + 11 new).

- `gateway-card.diff` — the change as a reviewable diff against the live tree of 2026-10-24 (config, core, api, strings, home.html, app.js, openapi).
- `apply_patch.py ROOT` — applies the same change with anchor assertions to a copy of the gateway tree; `test_tiger.py` goes into its `tests/`.
- To deploy (needs Ivan or the planning Claude to agree, a gateway recreate, `check-bind.sh`, the copy deck regenerated with `tools/gen_copy_deck.py --write`
  because a new UI string `locate` exists, and the entity of the real robot to exist in HA): apply, run the suite, `docker compose build && up -d --force-recreate`,
  then write `robots.json`.
- The HA service allow-list gains `vacuum.locate` (global for vacuums, still limited to the entities in scope).
