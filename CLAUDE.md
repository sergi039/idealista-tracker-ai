# IdealistaRank — Idealista Watch & Analyze

Guidance for AI agents and contributors. Deeper contracts: CONTRIBUTING.md
(setup, style, PR checklist), docs/DEV_RULES.md (dual-build ports and
isolation), MIGRATION_RUNBOOK.md, docs/STATE.md.

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

```bash
docker compose up -d --build                 # app on http://localhost:5001/
docker compose -f docker-compose.dev.yml up  # dev variant with --reload
pytest tests/ -v                             # test suite
pytest tests/ --cov=app --cov-report=html    # coverage report
```

### Local CI gate

`tools/ci/local_ci.sh` runs the same checks as `.github/workflows/ci.yml`
(ruff check, ruff format --check, no-source-bundles, `uv run pytest tests/
-q`) locally, standalone or as a git `pre-push` hook — a red run costs agent
cycles even though the repo's Actions minutes are free (issue #74). Enable it
once per clone:

```bash
tools/ci/install_hooks.sh    # git config core.hooksPath .githooks
```

Bypass a single push with `SKIP_LOCAL_CI=1 git push`. `.github/workflows/
ci.yml` stays the merge gate for autopilot; since issue #81 it runs the same
ruff commands, so the two really are in sync.

The hook's shared-`.git/config` canary (issue #74) compares config keys and
skips the four a parallel session writes (`branch.<name>.remote`, `.merge`,
`.rebase`, `.vscode-merge-base`): sessions share this clone, so a parallel
`git push -u` or `git worktree add -b` is not this gate leaking (issue #155).
Any other changed key is named and blocks the push. Only `core.bare`,
`core.worktree`, `core.repositoryformatversion`, `extensions.*` and
`include.*` are written back — only git's plumbing writes those, while
`user.email` or `core.hooksPath` may belong to another session.
`SKIP_LOCAL_CI=1` is not the answer to a config complaint any more.

Ruff itself is a locked dev dependency (`uv.lock`), so `uv run ruff` is one
pinned version for CI, the hook and you. Rule selection is explicit in
`pyproject.toml` (`[tool.ruff.lint] select`) because ruff's *default* set is
not stable across releases — do not delete it expecting the default to be
equivalent.

### Keeping the container current

The same installer enables `.githooks/post-merge`: **when main lands in a
clone, the running container is rebuilt from it.** The image is a `COPY . .`
snapshot and nothing re-takes it, so a merged fix does not reach the app until
someone rebuilds — on 2026-08-14 a container served a template that had been
fixed 15 seconds after the build, through the fix, its commit and its merge,
for 15 minutes. `/api/healthz` cannot catch that: it renders no template, and
`routes/main_routes.py` turns a `TemplateSyntaxError` into a redirect.

The hook is narrow on purpose. It acts only on `main`, only when the app
container is already running, only when the merge touched a path the build
context contains, and never while a deploy holds the autopilot lock — the
watcher's own `git merge --ff-only` fires this hook, and the lock is what
tells the two apart. Before building it parses every template and **refuses**
rather than snapshot one that does not, because `COPY . .` takes the working
tree: in a shared checkout that includes a parallel session's half-finished
edit, which is exactly how the 2026-08-14 image was made. Uncommitted files
are named in the output rather than allowed to ride in quietly. A single pull
skips it with `SKIP_AUTO_REBUILD=1 git pull`;
`tests/test_post_merge_hook.py` pins all of it.

This is the laptop's answer, not a replacement for
`tools/autopilot/deploy_watcher.sh`: the watcher polls main on the Mac mini
every five minutes with a health check and a rollback, and correctly refuses
to deploy a checkout that is on a branch or dirty — which is what a shared
agent checkout looks like nearly all the time. The hook writes no
`data/.deployed_sha`; that marker has exactly one writer.

**There is exactly one listing surface: `/properties`** (owner decision,
2026-08-09, superseding the 2026-08-08 one that kept `/lands` as a second,
archived page). `/`, the navbar and `/lands` itself all lead there, and that
is where new work goes. The reason is where the data is. `lands` froze at 168
rows with nothing newer than 2026-02-18; every ingested listing goes to
`properties` (`INGESTION_TARGET` defaults to it), and the legacy `Land` model
cannot represent the houses that now arrive at all, so switching ingestion
back was rejected.

`/lands` is a redirect, `templates/lands.html` is gone, and **the archive
banner it carried is gone with it** — the owner never asked for it. Do not
reintroduce a second listing page. The legacy rows are still readable: they
are mirrored into `properties` under the "Legacy Lands" subscription, which
is inactive and therefore offered in the subscription filter under *Archive*.
`Land` itself stays for `/lands/<id>`, `/export.csv` and the migration
service; nothing else reads it.

**Subscriptions are the organising idea of that surface.** They are
`search_profiles` rows — the owner's saved searches on idealista.com, matched
to incoming mail by search URL (`source_search_key`, #102) and by label. The
filter offers them in one dropdown: live ones first, retired ones under
*Archive*, each with its listing count; a subscription holding nothing (the
`Default` catch-all) is not offered unless it is selected. A bare
`/properties` shows **every live subscription at once** — it used to open on
one profile picked for the owner, which hid the other saved search behind a
control they had to know about. `profile_id=all` means the active ones only,
so retiring a subscription (`is_active = false`) is what moves it into the
archive without touching its listings. When more than one is on screen the
rows carry a subscription badge, and the travel columns fall back to the
preset targets — a *custom* target id belongs to one profile, so it would
label a column with a destination most rows were never measured against.

`/properties` carries the controls the owner actually used on `/lands`: the
cards/list toggle
(`view_type`), the combined/investment/lifestyle modes (`mode`, with
`score_total` / `score_investment` / `score_lifestyle`), the investment-rating
filter (`inv_metr`), and the Score / ★ / Title / Price / Area / Coords /
Travel / Inv. Metr. / Type / Added / Actions table. A bare `/properties`
opens on **that table, not the cards** (owner decision, 2026-08-09;
`DEFAULT_PROPERTY_VIEW_TYPE` in `routes/main_routes.py`) and still sorts by
date so the freshest listings stay on top.

**Sea view is a four-state verdict, not a flag** (`services/sea_view_service.py`):
`yes` needs the listing text and the terrain to agree, `likely` is one source
unopposed, `no` is computed and negative, and `unknown` means it could not be
computed — an approximate coordinate, or a source that refused. `unknown` is
never folded into `no`, and the filter never counts it as a match. Geometry
alone stops at `likely` on purpose: Copernicus EU-DEM is a *bare-earth* model,
so trees and buildings are invisible to it. A hand-set verdict on the property
page outranks both: a recalculation reads the row under a lock and leaves a
hand-set one untouched. Both sources are free and
keyless — OpenStreetMap coastline, EU-DEM 25 m via OpenTopoData — so Google
billing is not involved. Fill it with `python -m utils.backfill_sea_view`;
`enrichment.environment.sea_view` is where it lands, and mirrored `Land` rows
keep their old boolean at `enrichment.legacy_land.environment.sea_view`, which
reads as `likely` (it came from the same weak keyword pass) and never as `yes`.

**The "to beach" sort stays rendered as unavailable**: it needs Google Distance
Matrix and per issue #98 not one row holds a travel time. Do not "fix" that by
wiring the control to empty data — it comes back when #98 does.

**The page uses the width it has, and the table fits a tablet without
scrolling sideways.** Two rules in `static/css/style.css` do that, and both
are easy to break by accident. Above 768px `.container` drops Bootstrap's cap
(720px at md through 1320px at xxl) — **with no upper bound**, because the cap
is as wrong on a 2560px monitor, where the table became a strip down the
middle, as it is on an iPad. A first attempt stopped that rule at 1399.98px
and therefore changed nothing on the owner's own screen; do not reintroduce a
ceiling. Between 768px and 1200px the list table also shrinks its padding,
wraps its badges and (in portrait, under 992px) hides the Coords column, whose
municipality moves under the title instead. All of it only works because the
column widths live in `.col-*` classes rather than inline
`style="min-width: … !important"` attributes — an inline `!important` outranks
every media query, which is what made the table 1344px wide at any viewport.
`tests/test_tablet_list_layout.py` fails if those widths move back into the
markup or if the cap comes back.

**Every one of those controls exists exactly once** (owner decision,
2026-08-09, superseding "the page is a copy of the /lands layout"). The page
had grown a duplicate of nearly everything: a subscription dropdown in the
filter panel repeating the chips above it, an Archive dropdown repeating the
archive section inside that dropdown, Export CSV in the navbar *and* over the
list, Manual Sync in the header *and* in the navbar slot the page had taken.
What the page has now, top to bottom:

1. **Toolbar** (`#subscription-switcher`) — the subscription chips, one
   `More` menu holding what a chip cannot say (several at once, the archive,
   `unassigned`, an empty selection), and the Favorites / Hide removed
   switches. Those two are links, not form fields: they apply on click, and
   the filter form re-posts their state as hidden inputs so Apply cannot
   switch them off. The menu's checkboxes belong to the filter form through
   `form="filter-form"`, so a no-JavaScript Apply still carries them; with
   JavaScript they apply when the menu closes.
2. **Filter bar** (`#filters-card`) — one wrapping row, no captions above the
   controls. Each names itself in its first option (`All Types`, `All
   Municipalities`, `Inv. metr: all`); search leads it. Anything that changes
   *which* rows are shown lives here, and nothing else does.
3. **Result row** — the count, the modes, the cards/list toggle and Export
   CSV: the controls that change how the same rows are drawn.

Keep it that way. Adding a control means deciding which of the three it
belongs to, not adding a second copy near where it is needed.

**The beaches block is informational, and it must never move a score** (owner
decision, 2026-08-11). `Property.travel["beaches"]` holds every beach within a
20-minute drive (`BEACH_MAX_DRIVE_MIN`), nearest first, each linked to Google
Maps; a listing with none is shown no block at all, which is why an inland
property's right-hand column looks exactly as it did. It is *not* a travel
preset: presets resolve one place and feed the scorer, so a beach lookup Google
refuses is kept out of `travel["targets"]` and out of the run tally — a beach
must not turn a good travel run into a degraded one. The candidates ride in the
preset's own Distance Matrix batch, so the feature costs one extra Places call
per property and no extra route request — which holds only because the beaches
take the room the presets leave in that one request (six presets plus twenty
beaches is 26 destinations against a 25-destination limit, and the split would
be billed twice), and only candidates within 30 km in a straight line are
measured at all (a road is never shorter than that, so the
rest cannot come in under the limit and paying for them is waste). The four
statuses — `ok`, `none_within_limit`, `not_found`, `unavailable` — exist for the
#98 reason: only a *measured* absence hides the block, and a refusal still
renders, saying it was not measured. `natural_feature` + keyword `playa` is the
Places pair, measured against the live API on 2026-08-11: it returns real
beaches in Asturias *and* in Galicia (where they are named "Praia"), while the
legacy `tourist_attraction` + `playa` pair returned a fountain, a swimming pool
and the town itself. Google lists the same beach under several place ids, so
identical names are collapsed to the nearest one.

**Distance to the sea is a scoring criterion, and it reuses the sea-view
coastline client.** `services/sea_distance_service.py` scores straight-line
metres to the OSM coastline, but it does **not** fetch that coastline: it calls
`fetch_coastline_points()` in `services/sea_view_service.py`, which already owns
the Overpass side (one query per 0.1° cell, cached a month, throttled, plain
User-Agent token because Overpass answers 406 to anything else). Do not add a
second Overpass client — measured the hard way, a per-property or wide-box query
gets 504s.

The result lands in `Property.enrichment["sea"]` (a JSON column — no migration,
and a different key from sea view's `enrichment.environment.sea_view`) with one
of four statuses: `ok`, `no_coastline_within_radius`, `unavailable`,
`no_coordinates`. Only a measured absence scores 0; a refusal scores `None` and
is dropped from the weighted average, and a refusal never overwrites a previous
measurement taken at the same coordinates. That split is the lesson of #98 — do
not collapse it. The scorer applies logarithmic decay: the shoreline scores 100,
`near_m` (300) is the decay scale rather than a plateau, and `far_m` (10 000) is
where the score reaches 0 — overridable per subscription via
`scoring_config.categories.<cat>.sea_distance`. A `far_m` past the radius the
lookup actually covers scores `None` rather than 0, because nobody looked there.
`utils/recalc_sea_distance.py` backfills; it writes a rollback snapshot of the
score columns first, since rolling the app back does not undo a data rewrite.

The dual-build isolation contract
from the transition — legacy on 5001 vs universal on 5050, unique Docker
names, separate IMAP labels and cookie names — lives in docs/DEV_RULES.md
and TODO.md; respect it if you ever run both side by side.

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
- `models.py` — SQLAlchemy models; `migrations/` — schema migrations
- `utils/` — `auth` (admin_required, rate limits), `security`, `cache`,
  `email_parser`, `idealista_extractors`, bulk tools
  (`bulk_ai_analysis`, `recalc_travel_times`, `backfill_sea_view`)
- `templates/`, `static/` — Jinja2, Bootstrap, minimal vanilla JS/HTMX
- `tests/` — pytest suite; external APIs are mocked
- `docs/` — DEV_RULES.md, STATE.md, UNIVERSAL_PROPERTIES.md,
  PROPERTY_TYPES.md

## Hard rules

- Never read or echo `.env` — it holds IMAP and API credentials. Required
  config is validated at startup and fails fast; do not add silent
  fallbacks around it.
- External APIs cost real money (Anthropic, OpenAI, Google Places /
  Distance Matrix). Never run bulk backfills (`utils/bulk_ai_analysis.py`,
  `utils/recalc_travel_times.py`) without an explicit ticket saying so.
  `utils/backfill_sea_view.py` and `utils/backfill_osm_amenities.py` are the
  exceptions that prove the rule: they spend nothing, because OpenStreetMap
  and OpenTopoData are free. They are still paced — overpass-api.de grants two
  query slots per IP and answers 504 while they are busy, and it refuses the
  default `python-requests` User-Agent outright — so keep the caching, the
  shared `utils/http.py` `OVERPASS_GATE`, and the bare product token
  `HTTP_USER_AGENT`, both used by `services/sea_view_service.py` and the OSM
  amenity call in `services/enrichment_service.py`. The amenity backfill calls
  `EnrichmentService.enrich_osm_amenities` **directly and never
  `PropertyEnrichmentService.enrich_property`**, which would fire the paid
  Google travel and Places calls once per row.
- **Pacing is passed to the transport, never taken around it.** Hand
  `gate=OVERPASS_GATE` to `request_with_retries`; it takes the gate before
  every attempt. A caller that wraps the call in its own `gate.wait()` /
  `gate.mark()` paces its lookups and leaves the retries unpaced, which is the
  traffic a struggling endpoint sees most of. Same for any other
  rate-limited endpoint: give it its own `RateGate`
  (`ELEVATION_GATE` in `services/sea_view_service.py` is the second one) rather
  than a hand-rolled `_last_call_at` global.
- **Overpass is paced at 5 s, and that number is measured** (#152 follow-up).
  A 20-property dry run at the previous 2 s spent 39 requests on 20 answers —
  15 of them refused with `429 Too Many Requests`, more than the 8 refused with
  `504`. Both are retried, so nothing was recorded wrongly, but a backoff is
  not a rate: keep `OVERPASS_MIN_INTERVAL_S` where the measurement put it, and
  re-measure before lowering it. It costs an interactive Enrich nothing,
  because the gate is idle between presses.
- **Every Overpass caller reads three refusals, not one** (#144, all measured
  against the live instance): the `406` above, which also fires for a UA
  carrying a parenthetical comment; the `504` above, which needs a backoff in
  tens of seconds, not the half-second default in `utils/http.py`; and a
  server-side failure delivered *inside a 200* as
  `{"elements": [], "remark": "runtime error: Query timed out ..."}`. Reading
  `elements` off that last one writes a computed negative for a query that
  never ran, and caches it — the #98 defect, in a free API. Treat a `remark`,
  and a body with no `elements` list, as refusals. All three are already
  handled in `EnrichmentService._fetch_osm_amenities` and pinned by
  `tests/test_overpass_user_agent_and_refusal.py`; this rule exists so the
  next Overpass caller does not have to rediscover them.
- **Google Places Nearby Search reaches 50 km, whatever `radius=` asks for.**
  Measured 2026-08-11 at 43.551663,-6.831426 (property 360, La Caridad):
  `radius=50000`, `radius=100000` and `radius=200000` returned the *identical*
  seven places, same seven `place_id`s, farthest 45.21 km. Google clamps to its
  documented 50,000 m maximum silently — no error, no warning, no field saying
  it did — so a call asking for more reads as reaching further than it can, and
  a reviewer cannot tell from the code that it does not. Reach past 50 km comes
  from Places **Text Search**, which takes no `radius` at all (`location` only
  biases its ranking). That is how the legacy `Land` airport lookup finds an
  airport an hour away (`_airport_candidates` in `services/enrichment_service.py`);
  PR #254 does the same for the `/properties` airport preset, where the
  measurement was first taken.
- **What may be recorded as an airport is defined once**, in
  `services/place_rules.py` (the `PlaceRules` matcher) over the patterns on the
  preset in `services/search_profile_service.py` (issue #171). Google's
  `type=airport` covers helipads, aerodromes and aeroclubs; at the coordinate
  above, all seven results were exactly that. `/properties` refused them from
  #171 onward while the legacy `Land` path, holding no copy of the rules, went
  on taking the nearest — which is how 145 of 168 lands came to store a
  "nearest airport" at a median 0.27x the distance of the real one, rendering
  on `/lands/<id>` directly above the correct road distance from
  `Land.distance_airport`. Do not copy the patterns into a third caller; import
  them. `utils/clear_legacy_land_airport.py` removes the values the unfiltered
  search left behind (free — no API call — with a rollback snapshot).
- **Amenities are measured for `Property`, through the same one client**
  (#152). `_fetch_osm_amenities` is the whole Overpass amenity client — cache,
  gate, transport, refusals — and `_enrich_with_osm_data` (legacy `Land`) and
  `enrich_osm_amenities` (universal `Property`) are thin writers over it. The
  property one runs inside `PropertyEnrichmentService.enrich_property`, lands
  in `enrichment["infrastructure_extended"]`, and a refusal never fails that
  run: no score reads these counts. Before this the lookup was reachable only
  from the `Land` endpoints, so 213 of 352 listings had no Extended
  Infrastructure card at all — an absence that reads as "nothing nearby". Do
  not add a second amenity client, and do not let a refusal become empty
  counts.
- **An enrichment run reports how complete it was, not just pass or fail**
  (#153, owner decision 2026-08-09). `EnrichmentService.enrich_land` reduces
  its three sources to `ok` / `degraded` / `unavailable`, stamps that on the
  record as `infrastructure_extended["enrichment_status"]` and returns
  `state != unavailable` — the same shape, and the same boolean facade, as
  `Property.travel["api_status"]` in `services/property_travel_service.py`.
  Google is *decisive*: `_score_infrastructure_extended` reads only the
  `<amenity>_available` keys Places writes, so Google refusing means the run
  did not produce what it was asked for. Overpass is *advisory*: it cannot
  move a score, and it answers 504 whenever both of its two per-IP slots are
  busy, so failing the whole run on it would report failure for lands whose
  Google data arrived intact. That asymmetry is why `degraded` exists — do not
  collapse it into `ok` (a missed source reported as success is #98 again) and
  do not promote it to `unavailable` (option 2 in #153, which the owner
  rejected). `tests/test_issue_153_enrichment_run_state.py` fails on either.
- **The AI bridge runs both CLIs cold** (#201). `tools/ai_bridge.py` reaches
  Claude and ChatGPT through the owner's *subscriptions*, and both CLIs are
  agents: left with their defaults they read the owner's personal config, load
  the repository they were started in, and go to work. Measured, that meant a
  listing valuation taking 4m50s and hitting the 600 s timeout, `multi_agent`
  spawning research sub-agents, a 1100-token prompt arriving as 57k input
  tokens, and claude carrying 21 KB of this file into every valuation. So codex
  gets `--ignore-user-config --ignore-rules --ephemeral`, every interactive
  feature `--disable`d, and `model_reasoning_effort=low`; claude gets
  `--safe-mode --tools "" --effort low --no-session-persistence`; both run in an
  empty `workdir()` outside any repository. Do not restore a *profile* instead
  of `--ignore-user-config` — profiles layer on top of the base user config, so
  anything the owner adds later reaches this service. Do not set a fast/priority
  service tier: it buys 1.5x speed for 2.5x the credit rate, on every listing.
  A timeout kills the whole **process group**, because `codex` is a node wrapper
  whose grandchild does the work and survives a kill aimed at the wrapper —
  measured, five extra minutes of billed work nobody read.
  `tests/test_ai_bridge_isolation.py` fails if any of that is undone.
- Preserve the scraping throttle in `services/listing_status_service.py`
  (randomized sleeps between listing fetches). No bulk re-scrape loops.
- **There is no authentication** (owner decision, 2026-08-08): the admin
  login, `ADMIN_API_TOKEN` and `utils/auth.py` were removed, so every page
  and endpoint is open. This holds only because `docker-compose.yml`
  publishes the app on `127.0.0.1:5001` — never widen that binding, and
  never expose the app through a tunnel or reverse proxy without putting
  authentication back first.
- The rest of the security posture is hard-won and still applies: CSRF
  protection on form POSTs, rate limiting, parameterized queries only.
  Treat any weakening as a defect. Note that the JSON API blueprints are
  CSRF-exempt (`app.py`) because they used to be token-gated — with the
  token gone, a browser page can reach them; do not add new state-changing
  JSON endpoints without thinking about that. An open security/bug audit
  lives in GitHub issues #16–#30.
- Never change existing primary key types. Schema changes go through
  migrations (see MIGRATION_RUNBOOK.md).
- Mock external API calls in tests. Suites needing live services or
  credentials are reported as skipped, never as passed.

## Workflow

- GitHub issues are the source of truth for tasks:
  https://github.com/sergi039/idealista-tracker-ai/issues. An AgentsRoom
  board card, when used, is only a launch wrapper titled `#NN: …`.
- Branch from `main` (agent branches use `claude/**` or `codex/**`
  prefixes), merge back via PR. `main` is protected — no force-push, no
  deletion, changes only through a PR, admins included — so a direct
  push is rejected.
- CI exists (`.github/workflows/ci.yml`; issue #31 closed 2026-08-07):
  it runs on every PR and on push to `main`, with actions pinned by SHA.
  Three jobs — `pytest` does `uv sync --frozen` + `uv run pytest tests/ -v`
  on Python 3.11; `no-source-bundles` fails when an archive or source dump
  is tracked (issue #29); `ruff` runs `ruff check .` and `ruff format
  --check .` on the same uv setup (issue #81). **All three are *required*
  status checks on `main`** (strict: the branch must be up to date), so a
  red `ruff` blocks the merge exactly as a red `pytest` does — run
  `uv run ruff check .` and `uv run ruff format --check .` before you push,
  or the PR costs a cycle. `ruff` was the optional one until the owner added
  the context; this file said so until issue #264.
- Run `uv run pytest tests/ -q` locally and paste the real output before
  claiming done. That is a standing owner requirement in its own right,
  not a stand-in for CI.
- **Writing a migration?** Everything in `migrations/` is PostgreSQL-only and
  multi-statement, so SQLite cannot execute it and `db.create_all()` proves
  nothing about it. `tests/test_postgres_migrations.py` runs the real files
  against a real server; it skips unless `TEST_DATABASE_URL_POSTGRES` points
  at a **throwaway** database (never `idealista-db` on 5434), and the CI
  `pytest` job sets it plus `REQUIRE_POSTGRES_TESTS=1` so a missing server
  fails instead of skipping. A percent sign in migration SQL must be doubled —
  psycopg2 eats a lone one and the statement dies at deploy time.
- A local PostToolUse hook auto-runs `ruff check --fix` and `ruff format`
  on edited Python files — do not fight it. It calls whatever `ruff` is on
  PATH, which is not necessarily the version in `uv.lock`; the CI job and
  the pre-push gate use the locked one, so `uv run ruff` is the verdict
  that counts.
- `tools/autopilot/` runs the loop unattended: issue → PR → CI →
  independent review → squash-merge, with a LaunchAgent that redeploys
  main and rolls back on a red `/api/healthz`. A PR merges only on green
  CI *and* a reviewer PASS; `UNAVAILABLE` is not a pass. One agent per
  issue — see tools/autopilot/README.md before pointing a second one at
  an issue that already has a branch.
