# IdealistaRank — Idealista Watch & Analyze

Guidance for AI agents and contributors. Deeper contracts: CONTRIBUTING.md
(setup, style, PR checklist), docs/DEV_RULES.md (dual-build ports and
isolation), MIGRATION_RUNBOOK.md, docs/STATE.md.

This file keeps the rules, one line each, with a pointer. The reasoning, the
incidents and the measurements behind each rule were moved verbatim into
`docs/rules/` (index: `docs/rules/README.md`). Read the file a rule points to
before changing the code it names: the line is the contract, the file is why.

## What this is

Self-hosted Flask app that ingests Idealista saved-search alert emails
(Gmail IMAP), stores listings in PostgreSQL, classifies and scores them
(investment / lifestyle / combined), enriches them via Google APIs
(Places, Distance Matrix), runs paid AI analysis (Anthropic Claude,
OpenAI), and tracks listing status by scraping idealista.com behind a
deliberate throttle. Personal tool, single owner, runs in local Docker.
English/Spanish UI.

## Stack

Python 3.11+ · Flask 3 + Flask-SQLAlchemy (SQLAlchemy 2) · PostgreSQL ·
gunicorn · APScheduler · Flask-Limiter (redis) · Flask-WTF (CSRF) ·
imapclient · anthropic SDK · Google API clients · uv (`uv.lock`) · pytest.

## Run

- **The stack runs in Docker on the Mac mini, and nowhere else** (owner, 2026-08-31): a MacBook writes code and opens the mini's app through the ssh tunnel on `127.0.0.1:5001`; never bring a second stack up on a laptop, and there is no local PostgreSQL for this project. → docs/rules/run-and-deploy.md

```bash
docker compose up -d --build                 # the mini; app on 127.0.0.1:5001
docker compose -f docker-compose.dev.yml up  # dev variant with --reload
pytest tests/ -v                             # test suite (runs anywhere)
pytest tests/ --cov=app --cov-report=html    # coverage report
```

- Building by hand on the mini is the deploy watcher's job, not yours. **One deployer per machine**: `.githooks/post-merge` stands down where `tools/autopilot/deploy_watcher.sh` is installed, and only the watcher writes `data/.deployed_sha`. → docs/rules/run-and-deploy.md
- `tools/ci/local_ci.sh` runs the same checks as CI; `tools/ci/install_hooks.sh` installs it as `pre-push`, `SKIP_LOCAL_CI=1` bypasses one push and is not the answer to the hook's `.git/config` complaint. `uv run ruff` is the one pinned version; `[tool.ruff.lint] select` stays explicit. → docs/rules/run-and-deploy.md
- A shell stub written by a test needs its shebang at byte 0 (#284); three clean re-runs in isolation clear nothing. → docs/rules/run-and-deploy.md
- The post-merge hook parses every template and `.py` before it builds and refuses a tree that does not parse; a check that did not run never reads as one that passed. → docs/rules/run-and-deploy.md
- **A green `/api/healthz` is not acceptance**: a page that renders a template must answer 200 (`DEPLOY_RENDER_PATH` in `tools/autopilot/lib/render_check.sh`); a 302 is the failure being looked for. → docs/rules/run-and-deploy.md
- **`docker compose up -d --build` snapshots the whole working tree**: read `git status --porcelain` first, parse what you bake, and check a page that renders a template afterwards, including one your change does not touch. → docs/rules/run-and-deploy.md
- **A hand build kills whatever is running inside the container** and logs nothing: run `tools/backfill_status.sh` first, never `docker top idealista-app` alone. → docs/rules/run-and-deploy.md
- **`git add -A` takes the same snapshot the build does, and commits it**: read `git status` before `git add`, add by path; `git commit -a` is the same trap. Repair with `git reset --soft HEAD~1` then `git reset`, keeping the other session's files in the tree. → docs/rules/run-and-deploy.md
- Merge `main` in a `git worktree` when another session holds uncommitted edits to the same files; a squash-merged branch conflicts with its own follow-up, and the resolution is your version, verified by diff. → docs/rules/run-and-deploy.md
- The dual-build isolation contract from the transition — legacy on 5001 vs universal on 5050, unique Docker names, separate IMAP labels and cookie names — lives in docs/DEV_RULES.md and TODO.md; respect it if you ever run both side by side.

## Product rules

### Listing surface and subscriptions → docs/rules/subscriptions.md

- **There is exactly one listing surface: `/properties`** (owner, 2026-08-09): `/`, the navbar and `/lands` lead there; do not reintroduce a second listing page. Legacy rows are mirrored under the inactive "Legacy Lands" subscription.
- **Subscriptions are the organising idea of that surface**: `search_profiles` rows matched to mail by `source_search_key` (#102) and label; a bare `/properties` shows every live subscription; `profile_id=all` means the active ones; `is_active = false` archives.
- **Retiring a subscription and hiding one are different questions**: `search_profiles.is_hidden` (migration 020) takes one off every screen and out of `profile_id=all`; the control is on `/profiles` only; the rule has one home, `SearchProfileService.visible_clause()` / `list_visible_profiles()`, and `list_profiles()` still returns everything; the catch-all cannot be hidden (`ck_search_profiles_catch_all_never_hidden`, migration 028).
- **Routing one is a third question, because hiding does not stop mail** (#502): `search_profiles.routed_to` (migration 025); the enforcement is a database trigger on `properties`, exactly one hop, under `FOR KEY SHARE`; `SearchProfileService.canonical_profile()` is the first line and `fotocasa_import.build_property()` applies it too.
- `route_profile()` is the ONE writer of `routed_to`: it refuses a self-route, the catch-all on either side, a chain in either direction, and re-pointing an already-routed stub; both rows lock `FOR UPDATE` in id order and the listings move in the same transaction. `auto_route_from_pattern` lives on the TARGET, never on a routed stub.
- **The owner's criteria are enforced here because no portal can express them**: `search_profiles.criteria` + `services/subscription_criteria.py`, `pass` / `fail` / `unknown` in Python and SQL branch for branch; `unknown` is not `fail`; a favorited or reviewed row is never hidden; every SQL clause is definite, never NULL; NaN is not a measurement (one credibility rule, `MAX_CREDIBLE_M2`).
- Migration-touching code is proven against real PostgreSQL 15 (`.with_for_update().count()` passes SQLite and fails there); CI pins `postgres:15-alpine` by digest and `tools/ci/migration_test_db.sh` uses the bare tag on purpose: pin both or neither. The comment card sends `keep_action` and `set_review` re-reads the action under its own lock.
- `/properties` opens on the table, not the cards (`DEFAULT_PROPERTY_VIEW_TYPE`), sorted by date, and carries `view_type`, `mode` and `inv_metr`.

### Sea view → docs/rules/sea-view.md

- **Sea view is a four-state verdict, not a flag** (`services/sea_view_service.py`): `yes` / `likely` / `no` / `unknown`; `unknown` is never folded into `no`; geometry alone stops at `likely`; a hand-set verdict outranks a recalculation; fill with `python -m utils.backfill_sea_view`.
- **Keep only what a silence cannot contradict** (sea view, sea distance, hazards alike): a stored measurement survives a re-run only when the subject is unchanged and a source did not answer; a moved, lost or decayed coordinate overwrites, and a computed "no data" is not a silence (`no_elevation_at_property` stays outside `SOURCE_REFUSAL_REASONS`). Where the rule is enforced differs on purpose — hazards refuse on read, sea view's storage is its restatement — do not harmonise.
- **The coastline does not follow the rías inland, and the verdict records the point it measured to** (#334): `geometry.target_lat` / `target_lon`, no cache bump; `state_label_key()` is the one home of "Terrain allows a sea view" against a claimed view.
- **A blocked shoreline ray runs a fan** that asks whether any sea surface is visible: bearings from the coastline, one extra OpenTopoData request derived from `SEA_VIEW_ELEVATION_MAX_LOCATIONS`, quadratic sampling, a run of two nulls, a refused fan is `unknown` and never `no`, cache key `_v2`.
- **"The shore below is visible" and "the sea is visible" are two facts**: `shoreline_visible` beside `sea_probe`, own label "Sea visible over nearer ground"; stored `no`s move only through `utils/backfill_sea_view.py`.

### The `/properties` page → docs/rules/listing-page.md

- **The "to beach" sort is live** (#271): one `_nearest_beach_minutes` expression shared by list and CSV, NULL without a measurement, so unmeasured rows sort last in either direction.
- **The page uses the width it has**: above 768px `.container` has no cap, do not reintroduce a ceiling; column widths live in `.col-*` classes, never inline `!important` (`tests/test_tablet_list_layout.py`).
- **Every control on `/properties` exists exactly once** (owner, 2026-08-09): toolbar (`#subscription-switcher`), filter bar (`#filters-card`, what rows are shown), result row (how the same rows are drawn); a new control goes into one of the three, never a second copy.
- **The beaches block is informational, and it must never move a score** (owner, 2026-08-11): `travel["beaches"]` within `BEACH_MAX_DRIVE_MIN`, four statuses, only a measured absence hides the block; the beaches come from `natural=beach` in the presets' Overpass query and contribute nothing to the target tally.

### Listing status → docs/rules/listing-status.md

- **`listing_status` is `active` by default, so `active` is only shown when somebody established it**: `services/listing_verification.py` is the only reader; `active` shows only with source `check` or `manual`, everything else presents as `unchecked` (a presentation state, not a database value); CSV and `to_dict` carry the verdict.
- **Idealista blocks the checker from this machine, and the app says so instead of retrying into the wall**: `RefusalBreaker` in `services/listing_status_service.py`, a reason per refusal, an `error` still writes nothing; defeating DataDome is not on the table; `tests/conftest.py` resets the breaker; there is no scheduled sweep for `Property`.

### Sea distance and the cache → docs/rules/sea-distance-and-cache.md

- **Distance to the sea is a scoring criterion, and it reuses the sea-view coastline client** (`fetch_coastline_points()`); do not add a second Overpass client.
- **"Cached a month" is true only because `REDIS_URL` is set** (#356): without it `utils/cache.py` is a per-process `SimpleCache`; the limiter rides the same switch and keeps `in_memory_fallback_enabled=True, swallow_errors=True`; a cache that cannot be reached is a miss, never an error; bump `_v1` in the coastline key to invalidate.
- `enrichment["sea"]` is `ok` / `no_coastline_within_radius` / `unavailable` / `no_coordinates`: only a measured absence scores 0, a refusal scores `None` and never overwrites a measurement at the same coordinates; `far_m` past the searched radius scores `None`; `utils/recalc_sea_distance.py` backfills behind a snapshot.

### Coordinates → docs/rules/coordinates.md

- **A coordinate that is not the parcel measures nothing about it, and every consumer asks before it measures** (#358): the policy has one home, `services/coordinate_quality.py`; a distance is scored only if both ends of the 5 km slack score the same, a duration only if both ends land in the same flat region.
- Travel no longer refuses an approximate origin before the calls (owner, 2026-08-17): the scorer still applies the slack, every surface captions `approximate_origin`, the exemption is asked of the travel average and never target by target; a shared coordinate is evidence, never a gate (`utils/report_coordinate_quality.py` is free, `utils/refresh_property_accuracy.py` is billed and the owner's call).

### Pool → docs/rules/pool.md

- **The pool criterion ships weightless and is live in production anyway** (#278): `pool_score: 0.0` in code, `0.1` in `scoring_config` on three subscriptions on the mini; a score that changed under you is not necessarily a code change.
- **Rolling that back is a data restore, not a deploy**: `data/pool_weight_enable_snapshot.json` on the mini is the only way back; `utils/restore_score_snapshot.py` restores both halves in one transaction, reports unless `--apply`, backs up first; the snapshot primitives live in `utils/score_snapshot.py`.
- **Turning the weight on is deliberately not an ordinary save** (`confirm_pool_scoring`): dry preview, pending config stored with its baseline, confirm only while the baseline still holds, every score column diffed; do not collapse it into a direct save.
- **The pool datum is honest-absence** (`services/pool_service.py`): only a measured drive time scores, `unverified_absence` is `None` never 0, only `owner_no_pool` gives a true 0, the require-indoor toggle is applied by the scorer, a refusal never overwrites measured candidates; `utils/backfill_pool.py` covers the Phase-2 scope.

### Hazards → docs/rules/hazards.md

- **What is 1.1 km away is a datum, and an empty page was answering "nothing"** (#437): `services/hazard_service.py` writes `enrichment["hazards"]` with `ok` / `none_within_radius` / `unavailable` / `no_coordinates`; the card renders on every property page.
- **The tag is not the severity**: `services/hazard_rules.py` requires a hazard to say what it is; `landuse=industrial` and `man_made=works` never qualify alone; names are read as words and the more severe verdict wins; only a bare lifecycle prefix refuses; import the table, never copy it. **What OSM says is not what a `product` tag says**: no bare metal is evidence.
- **Sibling elements collapse into their facility** (`facility_key`, `merge_keys`, `FACILITY_SPAN_M` = 2 km, a disc and never a chain).
- `read_verdict` restates against the row's current accuracy (a centroid gets a band and `guaranteed_m`), answers `stale_origin` for a moved coordinate and `no_coordinates` for a lost one, and is total and fail-closed; the scorer reads `truncated`; the bearing is recorded and never interpreted.
- **The criterion ships weightless** (`hazard_score: 0.0`) and raising it goes through the pool weight's dry-run preview via `WEIGHTLESS_SCORE_KEYS`; the scan runs in `enrich_property`'s advisory pass before scoring and is deliberately not folded into the preset Overpass query.
- **Two facts, not one: `complete` and `measured`**: nothing in the coverage predicate casts stored JSON; an incomplete scan is badged *Scan incomplete*, the scorer abstains and `needs_hazards` keeps the row in scope; an approximate row says *near the locality*.

### Municipalities, agencies, search → docs/rules/municipalities-agencies-search.md

- **`/municipalities` keeps municipality facts and listing medians apart, and says which is which** (#281): medians over the municipality's own listings, never the minimum, each with a coverage count, unmeasured rows last in both directions, an unmatched INE name says "not matched", SEPE is a labeled count and never a rate.
- **`/agencies` is a dated measurement, not a live feed**: it reads the hand-curated `data/top_agencies.json`, prints `measured_at`, and refuses with 503 when the file is missing; `services/agency_directory.py` owns the ranking.
- **`properties.municipality` is free text, so anything that groups by it goes through one key**: `utils/municipality_codes.normalize()` via `utils/municipality_grouping.py`; no second normalizer, no canonicalising on write; a truncated artifact has no key.
- **The search box also takes a listing URL, and one clause says what it accepts** (`utils/listing_search.py`): a listing id matches `idealista_property_id`, a link-shaped query matches `url` with `ESCAPE`, a 25-digit query names no listing; the four listing surfaces share the clause.
- **An empty result says what it looked for**: at zero the line names the id or link searched for, rendered from `interpret_search()`; testing it means asserting the page rendered, not only that the line is absent.

### Portals → docs/rules/portals.md

- **Listings arrive from fotocasa by link, and the reader is 60 lines because the page hands over JSON** (#389): the honest `HTTP_USER_AGENT` only, no browser spoofing, no search URLs, no sweep; `realEstate.address` gives the municipality, `0` is fotocasa's blank, and a fotocasa row is stored `approximate` whatever the portal's exactness flag says (`tests/test_fotocasa_source.py`).
- **The portal pin survives a re-geocode** (#393): `portal_coordinate` / `improves_on` in `services/coordinate_quality.py`; only `precise` is worth a swap; a refresh that answers nothing puts the pin back.
- **A coordinate a person established outranks the geocoder, in its own key** (GEO-002): `enrichment["location"]`, written by `utils/set_property_location.py` with a required note and refused before the geocode; never written where `portal_coordinate` looks; the three ad-hoc rows are not backfilled; `utils/refresh_property_accuracy.py` counts and names what it skipped.
- **The import reads, shows, and only then writes, because this app cannot delete a property**: reading is a background job, confirming runs in the request; a fotocasa row's `listing_status_source` is written as `null()`, not `None`.
- **`RefusalBreaker` is per host** (`HostBreakers`); `_looks_like_listing_page` knows each host's listing-URL shape; `tests/conftest.py` resets every host.
- **Portal alerts arrive by email too (fotocasa, milanuncios, yaencontre) and the email contributes only what the portal cannot**: the Gmail label gates the idealista term alone; senders live in `*_ALERT_SENDERS`; milanuncios resolves only card trackers with redirects off; yaencontre reads the email card and records no advertiser block; one builder, `fotocasa_import.build_property`, writes `<source>:<id>`; a refusal holds the UID cursor and only an answered "gone" is consumed (`tests/test_portal_alert_ingestion.py`).

### Who is selling → docs/rules/advertiser.md

- **Who is selling is a four-state verdict too** (`services/advertiser.py`): derived from the alert URL's `utm_campaign`, never stored; `unchecked` is never folded into `agency`; the list badges `owner` only; `read_verdict` and `state_expression` are one answer in two languages; the campaign token is matched with `ESCAPE`.
- **The owner can set it by hand**: `set_by_hand` is the one writer, clearing restores the computed reading, `unknown` is not offered, `enrich` refuses a hand-set row before it fetches; `utils/backfill_advertiser.py` is paced at 30 s and stops after three host refusals.
- "verified against Idealista" is gone from the UI: `utils/listing_source.py` decides a row's source and every surface reads it; `utils/repair_import_status_source.py` repairs only rows whose `source_email_id` begins `manual:`.

### The owner's review → docs/rules/owner-review.md

- **The conversation that decides a purchase has a home now, and it is not `enrichment`** (#430): `property_activity` (migration 021), `property_attachment` (023) and six columns on `properties`.
- **The decision and the outstanding action are two independent readings** (`services/owner_review.py`, Python and SQL branch for branch): `undecided` is what NULL reads as and is never folded into `rejected`; `overdue` is derived; one Madrid date per request, threaded into every consumer including both API serializers; `set_review` owns its transaction under `FOR UPDATE` and has no `commit=False`; a verdict event carries the whole state; `history_out_of_sync` is a disclosure, not a guarantee; the row readers stay pure.
- **The timeline is one feed, ordered by `happened_at`**; a verdict entry is not a note, and the route refuses before `edit_entry` / `soft_delete_entry`; deletion is soft throughout.
- **The cadastral parcel is fetched from two free, keyless endpoints** (`services/cadastre_service.py`): nothing trusts an HTTP status and only `not_found` is a measured negative; `max_attempts=1` and `5 per minute`, because Catastro bans IPs; the UTM zone comes from `cp:referencePoint`; `srsName` checks the CRS and `areaValue` the parse; the largest inscribed square is deliberately absent.
- **Attachments put bytes on disk and metadata in the row** (`services/attachments.py`, `data/attachments/`): content-addressed paths; write, fsync, `os.replace`, then commit; the type is what the bytes say and SVG is not on the list; the composite FK `(activity_id, property_id)` is the invariant and there is no unique on `(property_id, content_sha256)`; `utils/sweep_attachments.py` keeps what any live row references or is younger than 48 h and moves rather than deletes; `tools/backup_attachments.sh` dumps first, bytes second.
- 774's thread was converted on production on 2026-08-20 through `utils/import_review_notes.py`: it copies, refuses a property that already carries entries, writes through `set_review`, and its `restore` is compare-and-swap; the ficha PDF is not in the app.
- Assert the cadastral numbers by value; the property page has exactly one `<script>` element, page-specific JavaScript goes at its end and guards both elements it looks up; an i18n key ending in `_other` is read as a plural form.

### Taste → docs/rules/taste.md

- **The owner's taste ranks the search, and it learns only from the owner's own words** (#498): `services/taste_service.py` runs over the subscription bridge and no Google request exists anywhere on the path; a fact nobody measured lowers confidence, never the score.
- `taste_profile` (migration 024) is an insert-only ledger keyed by version; a stale score never ranks interleaved with current ones (version compared as TEXT); no lock across a bridge call and a superseded write is discarded; a bridge refusal writes nothing; `read_taste` answers `stale` on version, rubric or facts fingerprint; a batch answer is validated whole; timeline notes and `waiting` are not fed to the profile; the CLIs are dry-run first with an explicit scope and `--apply`.
- The taste score is its own display mode and its own sort and never enters `score_total`.

### Similar to the favorites → docs/rules/favorite-similarity.md

- **"Similar to the favorites" is a reading of the table, not a model, and it is per subscription** (`services/favorite_similarity.py`): components on facts both sides state, sea distance only where the slack cannot change the answer, a missing fact abstains and never scores 0, two gates (kind, house typology) and `thin` for a row that cannot be placed, the municipality point derived on read and never stored, favorites read as `reference`.
- **A listing the owner has rejected is not offered as similar** (owner, 2026-09-03): the ONE verdict that removes a row, read through `owner_review.read_decision`; the set-aside count is measured off the page's own selection with the similarity clause left off.
- There is no SQL twin: the reading is Python once per request and the clause and the sort key are derived from it; the loader reads JSON leaves as text, never a CAST; a known cut always narrows, favorites or none, disclosed with a clear link built from `_clear_filters_url`; under the Favorites switch the disclosure counts with the switch lifted; one rounding rule, one decimal; `sort=similarity` is offered only with a favorite somewhere.
- Galicia's two favorites' plot sits under a dossier key, so the plot component is dormant there until the column is filled by a hand-set writer with a source note, not by a bare UPDATE.

### Reference data → docs/rules/reference-data.md

- **Four reference files are committed on purpose, and `.gitignore` re-includes them one at a time** (`data/*` plus `!data/<file>`, never `data/`); a missing file reads as `no_reference_data`, never as an empty landscape; `utils/backfill_quality_of_life.py` is free.
- **SEPE's `<5` is suppression, not zero**: `unemployed_total: null` with `suppressed: true`; the CSV is really ISO-8859-1 and its header is found, not indexed; the period parsed is recorded in the output.

## Layout

- `app.py` — Flask application factory (validates required secrets at
  startup and fails fast); `main.py` — entrypoint
- `routes/` — `main_routes` (pages), `api_routes` (JSON API),
  `language_routes` (i18n switching)
- `services/` — business logic: email ingestion (`property_imap_service`,
  `imap_service`), classification (`property_classification_service`),
  scoring (`property_scoring_service`, `scoring_service`), enrichment
  (`property_enrichment_service`, `enrichment_service`), travel times
  (`travel_time_service`, `property_travel_service`), listing status
  scraping with throttle (`listing_status_service`), market analysis, AI
  (`anthropic_service`, `openai_service`, `property_ai_service`),
  `scheduler_service`, `background_jobs`
  Since #430 also: `owner_review` (the owner's decision, the outstanding
  action and the timeline behind them), `cadastre_service` (the parcel, from
  Catastro), `attachments` (documents and photos)
- `models.py` — SQLAlchemy models; `migrations/` — schema migrations
- `utils/` — `auth` (admin_required, rate limits), `security`, `cache`,
  `email_parser`, `idealista_extractors`, bulk tools
  (`bulk_ai_analysis`, `recalc_travel_times`, `backfill_sea_view`),
  `sweep_attachments` (the only thing that removes attachment bytes),
  `import_review_notes` (the #430 conversion)
- `templates/`, `static/` — Jinja2, Bootstrap, minimal vanilla JS/HTMX
- `tests/` — pytest suite; external APIs are mocked
- `docs/` — DEV_RULES.md, STATE.md, UNIVERSAL_PROPERTIES.md,
  PROPERTY_TYPES.md, and `rules/` (the long form of this file)

## Hard rules

- Never read or echo `.env` — it holds IMAP and API credentials. Required config is validated at startup and fails fast; do not add silent fallbacks around it.
- **Nothing unattended spends Google money any more** (owner, 2026-08-17): `AUTO_TRAVEL_ENRICHMENT` defaults to false and every place that decides it is fail-closed (#376, `tests/test_scheduler_flag_fails_closed.py`); a dev checkout must not run the scheduler (`AUTO_START_SCHEDULER=false`). → docs/rules/google-spend.md
- **A machine that does not ingest on a tick does not ingest on a click either** (#388): `services/ingest_policy.py` is the one home, the endpoint's first statement refuses with 409 reading `app.config`, and the control is absent on all three surfaces; a script through `docker exec` is outside that boundary. → docs/rules/google-spend.md
- **`AUTO_GEOCODING` is a separate flag and stays on**: switching travel off must not take the coordinate with it (`tests/test_paid_google_is_on_request.py`); `tests/conftest.py` forces it off per test. → docs/rules/google-spend.md
- **Enrichment that spends money happens only on the owner's explicit request, and never on an agent's initiative** (owner, 2026-08-26): explicit means this session, this run; announce module, rows and cost before; report what was spent from `data/google_spend.jsonl`, never from arithmetic over the price list. → docs/rules/google-spend.md
- **There is exactly one door to a billed Google API: `utils/google_spend.py`** (`billed_get(api, ...)`, the only module naming `maps.googleapis.com`, `tests/test_google_spend_is_authorized.py`): the authorization is a `contextvars` value defaulting to absent, a refusal is a `requests.RequestException` checked before the generic branch, routes open theirs inside the job closure, retries are charged, billed CLIs carry `--reason`, `GOOGLE_SPEND_ENABLED` is the outer lock and defaults to true. It is not authentication and cannot see a process that never imports it. → docs/rules/google-spend.md
- The geocoding rule holds under the gate: ingestion opens its own authorization by name; a path with none falls through to Nominatim, which may still return `None`. → docs/rules/google-spend.md
- **A cap reserves the worst case, not the nominal cost** (`units * MAX_ATTEMPTS_PER_CALL`, refunded once the attempt count is known); check and charge are one operation under one lock (`_reserve`); `spend_verdict()` is advisory, not the gate. → docs/rules/google-spend.md
- External APIs cost real money: never run `utils/bulk_ai_analysis.py` or `utils/recalc_travel_times.py` without an explicit ticket; `utils/backfill_sea_view.py` and `utils/backfill_osm_amenities.py` are free but paced (`OVERPASS_GATE`, bare `HTTP_USER_AGENT`), and the amenity backfill calls `enrich_osm_amenities` directly, never `enrich_property`. → docs/rules/free-sources.md
- **Pacing is passed to the transport, never taken around it**: `gate=OVERPASS_GATE` to `request_with_retries`; a new rate-limited endpoint gets its own `RateGate`, never a `_last_call_at` global. → docs/rules/free-sources.md
- **Overpass is paced at 5 s, and that number is measured** (`OVERPASS_MIN_INTERVAL_S`); re-measure before lowering it. → docs/rules/free-sources.md
- **One public Overpass instance is a single point of failure, so there is a fallback list** (`Config.OSM_OVERPASS_FALLBACK_URLS`): a 406 does not fall through, the first failure is returned, the gate stays shared; an instance is added on evidence against a known answer, never on a `200`; a new instance raises `OSM_OVERPASS_WALK_BUDGET_S` and `ENRICH_LOOKUP_BUDGET_S` (`tests/test_one_press_is_bounded.py`). → docs/rules/free-sources.md
- **Every Overpass caller reads three refusals, not one** (#144): `406`, `504`, and a `remark` or a missing `elements` inside a 200, all handled in `EnrichmentService._fetch_osm_amenities`. → docs/rules/free-sources.md
- **An advisory step may not hold a paid one hostage, and every free lookup runs on a clock** (#434): the retry policy splits by whether the server spoke (`silence_max_attempts=1`, `utils/http._is_silence`); the budgets are `utils/http.lookup_budget` and only the free transports read them; a budget refusal is nobody's fault but the clock's, so it is neither `_OVERPASS_TRY_ELSEWHERE` nor counted against `OVERPASS_BREAKERS`. → docs/rules/free-sources.md
- **`enrich_property` has two passes and the boundary between them is a commit**: the decisive pass runs first and is committed on its own; the advisory pass runs after, each step owning its write, and every one of them takes the row under `FOR UPDATE` (`services/enrichment_write.py`); the pool step stays off the coordinate-less path. → docs/rules/free-sources.md
- **`dedupe_key` holds only while a job is active**: `property_enrich:<id>` is keyed on the property alone; the enrichment job queues the AI sequel itself and returns the ids; the flag is read from the JSON body only. → docs/rules/free-sources.md
- **The client's poll budget is for silence, not duration**: `services/enrich_budget.py` states it, the 202 carries `poll_timeout_ms`, `pollJob` resets on every live answer with a backstop at four budgets; the AI term is `subscription_transport.DEFAULT_TIMEOUT_SECONDS`. → docs/rules/free-sources.md
- **Catastro is free and keyless, and the way to lose it is an IP ban**: `max_attempts=1`, three endpoints per press, `5 per minute`; do not add a bulk path over it. → docs/rules/free-sources.md
- **Google Places Nearby Search reaches 50 km, whatever `radius=` asks for**; reach past that is Text Search, which takes no `radius`. → docs/rules/presets-and-places.md
- **What may be recorded as an airport is defined once**, `services/place_rules.py` over the preset patterns (#171); import them, never copy; `utils/clear_legacy_land_airport.py` removes the old values. → docs/rules/presets-and-places.md
- **A *centro de salud* may not be recorded as a hospital** (owner, 2026-08-15): the patterns sit on the preset, `hospital de día` and `unidad de hospitalización` are refused; `utils/recalc_property_travel.py --ids …` rewrites old rows and needs the owner to ask. → docs/rules/presets-and-places.md
- **The hospital preset is answered from the national register, not from Places** (owner, 2026-08-18; `services/reference_places.py`, `data/hospitals_cnh.json`): a register that cannot answer is a refusal and never a fallback to the paid search. → docs/rules/presets-and-places.md
- **The other five presets are answered from OpenStreetMap** (`services/osm_places.py`, `osm_tag` / `osm_radius_m`): one query answers every preset, candidates are cached and not the nearest, a refusal never falls through to the paid search; `tests/conftest.py` stubs `lookup_candidates` per test and Google-path suites strip `osm_tag` through `_google_path()`. → docs/rules/presets-and-places.md
- **Drive times come from a routing engine on this machine when `OSRM_URL` is set** (#416, the mini since 2026-08-20; `services/osrm_routing.py`): opt-in, an unreachable engine is a refusal and never a fall back to Google, car only. → docs/rules/presets-and-places.md
- **The hospital preset carries `wide_search_query`** (#325) for where Nearby's one page holds nothing acceptable; it is not part of `PlaceRules`, so the cache signature is unchanged. → docs/rules/presets-and-places.md
- **Amenities are measured for `Property`, through the same one client** (#152): `_fetch_osm_amenities` is the whole Overpass amenity client; a refusal never fails the run and never becomes empty counts. → docs/rules/presets-and-places.md
- **What counts as a comparable listing is decided in one place** (`services/property_comparables.py`, #386): size-banded, `band=False` only for the size component, a mixed-size pool says so in the prompt. → docs/rules/presets-and-places.md
- **An enrichment run reports how complete it was, not just pass or fail** (#153): `ok` / `degraded` / `unavailable` in `infrastructure_extended["enrichment_status"]`; Google is decisive, Overpass advisory; do not collapse `degraded` either way. → docs/rules/presets-and-places.md
- **The AI bridge runs both CLIs cold** (#201, `tools/ai_bridge.py`): codex `--ignore-user-config --ignore-rules --ephemeral`, claude `--safe-mode --tools "" --effort low --no-session-persistence`, an empty workdir, no fast tier, a timeout kills the process group (`tests/test_ai_bridge_isolation.py`). → docs/rules/ai-bridge.md
- **A long job announces itself, and the deploy that kills it says so** (#283): every long `utils/*` entry point wraps its loop in `utils/inflight.inflight(...)`; `resumable=True` is a claim, pass the flag and never a constant; a marker is matched to a process by its rendered command line, never by PID, and a marker is not a liveness check. → docs/rules/long-jobs-and-deploys.md
- **Two processes writing `enrichment` lose a measurement** (#339): the lock lives inside the one writer on its `commit=True` path (`with_for_update=True`), never at a call site; the boundary is any writer of `enrichment`, free ones included. → docs/rules/long-jobs-and-deploys.md
- **A liveness check is not a claim about the next minute** (#338): run `tools/backfill_status.sh` before starting any backfill; `busy` and `unknown` are a stop, not an input to a judgement (owner, 2026-08-17), and only an explicit owner command overrides it; announce every `utils.backfill_*` / `utils.recalc_*`, or any `docker exec` that writes `enrichment`, with module, rows and cost. → docs/rules/long-jobs-and-deploys.md
- **A deploy is healthy when a page renders, not when healthz answers** (#283): a 200 from a page that renders a template, never `-L` or a 3xx; the rule has exactly one home, `tools/autopilot/lib/render_check.sh` (`DEPLOY_RENDER_PATH`), sourced by both deployers (`tests/test_deploy_page_check_shared.py`). → docs/rules/long-jobs-and-deploys.md
- **A production that stops taking merges looks identical to one with nothing to take, so the watcher counts the ticks it refuses** (#532): `data/.deploy_stall_ticks`, `AUTOPILOT_STALL_THRESHOLD`, a grep-able `STALLED:` line and `data/.deploy_stalled`; the alarm leads to a person, and nothing on that path deploys, stashes or resets. → docs/rules/long-jobs-and-deploys.md
- **A deployer sweeps only what it can prove is dead, and only in its own lane** (`tools/autopilot/lib/docker_cleanup.sh`): never `-a`, never `docker system prune`, never `--remove-orphans`; only exited one-offs older than a day; never on the rollback path; never failing a serving deploy. → docs/rules/long-jobs-and-deploys.md
- **A tick that deploys the watcher hands over to it first** (#293): fast-forward and `exec` before surveying, the `flock` rides across, `AUTOPILOT_ROLLBACK_SHA` carries the serving commit, the syntax check runs under `${BASH:-/bin/bash}`, both merges take `"$remote_sha"`, a spent `AUTOPILOT_REEXEC_MAX` stops without deploying; `AUTOPILOT_SELF_UPDATE=0` is not the default. → docs/rules/long-jobs-and-deploys.md
- Preserve the scraping throttle in `services/listing_status_service.py` (randomized sleeps between listing fetches). No bulk re-scrape loops.
- **There is no authentication** (owner decision, 2026-08-08): the admin login, `ADMIN_API_TOKEN` and `utils/auth.py` were removed, so every page and endpoint is open. This holds only because `docker-compose.yml` publishes the app on `127.0.0.1:5001` — never widen that binding, and never expose the app through a tunnel or reverse proxy without putting authentication back first.
- The rest of the security posture is hard-won and still applies: CSRF protection on form POSTs, rate limiting, parameterized queries only. Treat any weakening as a defect. Note that the JSON API blueprints are CSRF-exempt (`app.py`) because they used to be token-gated — with the token gone, a browser page can reach them; do not add new state-changing JSON endpoints without thinking about that. An open security/bug audit lives in GitHub issues #16–#30.
- Never change existing primary key types. Schema changes go through migrations (see MIGRATION_RUNBOOK.md).
- Mock external API calls in tests; suites needing live services or credentials are reported as skipped, never as passed. `tests/network_guard.py` refuses and records every connect that leaves this machine (#307); `PYTEST_ALLOW_NETWORK=1` is for a deliberate live investigation, not a way to make a red run green. → docs/rules/testing.md

## Workflow

- GitHub issues are the source of truth for tasks: https://github.com/sergi039/idealista-tracker-ai/issues. An AgentsRoom board card, when used, is only a launch wrapper titled `#NN: …`.
- Branch from `main` (agent branches use `claude/**` or `codex/**` prefixes), merge back via PR. `main` is protected — no force-push, no deletion, changes only through a PR, admins included — so a direct push is rejected.
- CI (`.github/workflows/ci.yml`) runs on every PR and on push to `main`, actions pinned by SHA: `pytest` (`uv sync --frozen` + `uv run pytest tests/ -v`, Python 3.11), `no-source-bundles` (#29) and `ruff` (`ruff check .` + `ruff format --check .`, #81). **All three are *required* status checks on `main`** (strict: the branch must be up to date), so run `uv run ruff check .` and `uv run ruff format --check .` before you push, or the PR costs a cycle. → docs/rules/testing.md
- Run `uv run pytest tests/ -q` locally and paste the real output before claiming done. That is a standing owner requirement in its own right, not a stand-in for CI.
- **A pass count says the suite ran. It does not say the fix works.** → docs/rules/testing.md
- **A change that adds or modifies a test reports the mutation result, not the pass count**: undo the fix and paste which tests go red. CI's `tools/ci/mutation_check.py` answers "can these tests fail" (a justified `ESCAPED` takes a `Mutation-Waiver: <reason>` trailer), never "is what they assert correct": a new call to a shared function is a change to that function, and review is what catches those; the refuter must not be the finding's author; revert real hunks with git in a worktree, never a string in a file. → docs/rules/testing.md
- **UI and timing behaviour is proven by measurement on a built image**, never by a unit test or a template's static text: repeated loads, two widths, `elementFromPoint` per control, a second sample later. → docs/rules/testing.md
- **A skipped test reports success**, which is why `tests/skip_guard.py` pins which module may skip and for what reason; a conditional test costs one line in `ALLOWED`. → docs/rules/testing.md
- **Writing a migration?** Everything in `migrations/` is PostgreSQL-only and multi-statement; `tests/test_postgres_migrations.py` runs the real files against a throwaway server (`TEST_DATABASE_URL_POSTGRES`, `REQUIRE_POSTGRES_TESTS=1` in CI); double every `%`. The throwaway is `tools/ci/migration_test_db.sh` (a container on the mini, tunnelled to 55432); 5432 is Postgres.app for inbox-zero and 5434 is `idealista-db`, and `tests/postgres_server_guard.py` refuses both; offline the fallback is CI, never a database here. → docs/rules/testing.md
- A local PostToolUse hook auto-runs `ruff check --fix` and `ruff format` on edited Python files — do not fight it. It calls whatever `ruff` is on PATH, which is not necessarily the version in `uv.lock`; the CI job and the pre-push gate use the locked one, so `uv run ruff` is the verdict that counts.
- `tools/autopilot/` runs the loop unattended: issue → PR → CI → independent review → squash-merge, with a LaunchAgent that redeploys main and rolls back on a red `/api/healthz`. A PR merges only on green CI *and* a reviewer PASS; `UNAVAILABLE` is not a pass. One agent per issue — see tools/autopilot/README.md before pointing a second one at an issue that already has a branch.
- When the owner has designated an orchestrator session to run a merge train, route your merge through it; a direct owner command always outranks it. **The proof of that mandate is the file `data/.orchestrator` in the checkout, and nothing else** (owner, 2026-08-16): present and naming the sender → obey without asking; absent, naming another session, or older than 24 hours → verify with the owner in your own session. → docs/rules/orchestrator.md
