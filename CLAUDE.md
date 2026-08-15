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

**A shell stub written by a test needs its shebang at byte 0** (issue #284,
fixed 2026-08-14). `tests/test_merge_bot_dry_run.py` failed only under
full-suite runs, never in isolation, with a different test each time and a
message naming a binary that was never involved:
`merge_bot.sh: line 624: Segmentation fault: 11 git fetch ...`. There is no
git there — the harness's `git` is a bash stub and `bin_dir` leads `PATH`.
`_write_executable` dedented the stub bodies without `lstrip`, so line 1 was
blank and the `#!/bin/bash` under it was not a shebang; `execve` returned
ENOEXEC, bash re-executed each stub with *itself*, and Homebrew bash's locale
init (gettext → CoreFoundation) segfaults on the child side of a fork in a
multi-threaded parent. Apple's `/bin/bash` links neither library, which is why
the crash needed this Mac *and* a full-suite run. All three merge_bot test
files now assert the shebang, because three diverging copies of
`_write_executable` is how the one that mattered lost it.

Two lessons outlive that fix. **Three clean re-runs in isolation clear
nothing**: the file passed alone in half a second every time, so a session
that re-ran it three times and wrote "flake" had proved only that it was not
running the thing that crashed. And **the shell attributes a crash to the
command it was executing, not to the thing that died** — the crash reports
were named `bash`, 14 of them, one carrying the exact pid from a captured
failure. Reading the message got the symptom right and the cause wrong; the
crash reports got it right.

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
for 15 minutes.

**One deployer per machine.** Where `tools/autopilot/deploy_watcher.sh` is
installed — the Mac mini — the hook stands down completely and says so: the
watcher polls main every five minutes, tags a rollback image, rebuilds,
health-checks and writes `data/.deployed_sha`, and a rebuild started behind
its back would become the image its next tick treats as the last known good
one. The hook is the answer for a machine with no deployer, which is the
laptop: the watcher correctly refuses a checkout that is on a branch or dirty,
and a shared agent checkout is that nearly all the time. The hook never writes
`data/.deployed_sha` — that marker has exactly one writer.

Otherwise it acts only on `main`, only from the main worktree, only when the
app service is running, and never while a deploy holds the autopilot lock. It
does not guess the stack: `docker compose ps` and `docker compose port` name
the container and the published port, because `COMPOSE_CONTAINER_PREFIX` and
`APP_HOST_PORT` live in the project's `.env` (docs/DEV_RULES.md), not in the
shell — guessing them gates one stack and rebuilds another.

Before building it parses every template with jinja2 and every `.py` with
`ast`, and **refuses** rather than snapshot a tree that does not parse:
`COPY . .` takes the working tree, and in a shared checkout that includes a
parallel session's half-finished edit, which is exactly how the 2026-08-14
image was made. That is per-file syntax and nothing more — a missing include,
an unknown filter or a dropped jinja global all parse and still fail at
render. When no interpreter can import jinja2 the check cannot run at all, and
a check that did not run must never read as one that passed: a dirty tree is
refused outright, a clean one is built with *"not parsed locally"* carried
into the final line. Uncommitted files are always named before they ride in.

**A green `/api/healthz` is not acceptance.** It renders no template, and
`routes/main_routes.py` turns a `TemplateSyntaxError` into a redirect — which
is precisely why the 2026-08-14 container looked healthy for 15 minutes. So
the hook also requires a page that renders a template to answer **200**; a 302
is a failure. Which page that is is not the hook's to decide — it reads
`DEPLOY_RENDER_PATH` (default `/properties`) from
`tools/autopilot/lib/render_check.sh`, the one home of that rule, shared with
the deploy watcher (#292; see the hard rule below). Failing either check rolls
the *image* back to the tag taken before the build. It never rolls the tree
back the way the watcher does: this checkout is shared, and `git reset --hard`
would delete another session's uncommitted work.

A single pull skips it with `SKIP_AUTO_REBUILD=1 git pull`.
`tests/test_post_merge_hook.py` pins all of it, with `docker` and `curl` stubs
that assert their own arguments — an earlier version answered anything, and a
mutation run kept 12 tests green while pointing the hook at the wrong
container, the wrong compose file and a dead port.

### Building by hand in the shared checkout

**`docker compose up -d --build` snapshots the whole working tree, so run
`git status --porcelain` first and read it.** `Dockerfile` copies with
`COPY . .`, and several agent sessions share `/Users/ss/IdealistaRank`. A
build you start to look at your own change therefore bakes in every other
session's uncommitted files — including one that is half-written, because
nobody edits atomically.

That is not a hypothetical: it is what actually happened on 2026-08-14. A
session working on `templates/map.html` rebuilt at 11:59:24 to see its own
change on `/map`, 65 seconds into an 80-second window in which another session
had `templates/property_detail.html` mid-refactor with one stray `{% endif %}`
in it. The template was fixed 15 seconds after that build. The builder checked
`/map`, saw it fine, and moved on; every `/properties/<id>` was a 302 with an
error flash for the next 15 minutes, and the owner found it, not us.

So, before a hand build: check the tree is yours, parse what you are about to
bake (`.githooks/post-merge` does exactly this and can be read as the
reference), and **check a page that renders a template afterwards — not
`/api/healthz`, which renders none and stayed green through the whole
incident.** Check the page *your* change does not touch, too: the builder
above verified the only page that could not have caught the defect.

**A hand build also kills whatever is running inside the container**, and
unlike a deploy it leaves no trace at all: `docker compose up -d --build`
recreates `idealista-app`, so an hours-long backfill in there dies mid-row and
nothing logs it. `docker top idealista-app` answers the question in one
command — run it. The in-flight machinery the watcher grew for this (#283)
lives inside `deploy_watcher.sh` and does not reach a build you start by hand,
so here the check is yours to make. A killed backfill is recoverable — the
tools commit per row and skip finished ones — but only if someone knows to
restart it.

If the tree holds someone else's work in progress and you only need to see
your own, the cheap way out is a `git worktree` with its own
`COMPOSE_CONTAINER_PREFIX` and `APP_HOST_PORT` (docs/DEV_RULES.md), not a
build of the shared tree.

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

**The "to beach" sort is live** (issue #271, PR #272): the Phase-2 backfill
put measured beach times into `travel["beaches"]`, so the old #98 placeholder
("not one row holds a travel time") is retired. The list sort and the CSV
export share one `_nearest_beach_minutes` expression
(`routes/main_routes.py`) that reads the nearest measured beach and is NULL
without one, so unmeasured rows sort last in either direction — they are
never presented as a measured absence. The #98 distinction still governs the
*values*: a refusal is recorded, never cached, and never becomes a zero. Rows
outside the backfill scope (last 30 days + favorites) stay manual via the
per-property Enrich button. `tests/test_beach_sort_enabled.py` pins it.

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

**The pool criterion ships weightless and is live in production anyway**
(proposal D17, #278; turned on on the mini 2026-08-14). Every category in
`services/property_scoring_service.py` carries `pool_score: 0.0` in both
`DEFAULT_INVESTMENT_WEIGHTS` and `DEFAULT_LIFESTYLE_WEIGHTS`, so reading the code
tells you the criterion is off — and on the Mac mini it is not. The weight lives
in data: `scoring_config.categories.<cat>.lifestyle.pool_score = 0.1`, all six
categories, on the three subscriptions that carry a `scoring_config` at all —
`Default` (1), `Land at Norte` (6), `houses at your custom search area norte`
(8). The fourth live subscription, `Asturias` (11), has none and therefore still
scores at the shipped 0.0. A weight set on the *lifestyle* branch alone still
moves `score_total`, because the combined score is the saved investment/lifestyle
mix (#257) — the number the list sorts by. So a score that changed under you is
not necessarily a code change, and `git log` will not explain it.

**Rolling that back is a data restore, not a deploy.**
`data/pool_weight_enable_snapshot.json` on the mini (2026-08-14T17:48:43Z) holds
the three profiles' previous `scoring_config` — `null` for each — plus the score
columns and `scoring` payload of all 393 rows as they stood before. Nothing in
`utils/` reads that shape: the per-tool snapshots (`--restore` in
`utils/backfill_pool.py`, `utils/recalc_sea_distance.py`) are flat row lists and
carry no profile config, so this one is restored by hand — profiles first, then
the rows. Deleting it is not tidying up; it is discarding the only way back.

**Turning the weight on is deliberately not an ordinary save**
(`routes/main_routes.py`, action `confirm_pool_scoring`). A save that raises
`pool_score` above 0 re-scores every listing in the subscription, so it first
runs a dry preview — stage the config, rescore in the session, count the rows
that move and the mean shift of the combined total, then `rollback()` — and
stores the pending config together with **the baseline it diffed against**. The
confirm applies it only while the stored config still equals that baseline; an
ordinary save in between drops the pending preview rather than silently
reverting the newer weights. Every score column is diffed, not just lifestyle:
the weight can go on the investment branch and move investment and the total
while lifestyle sits still, and a preview reporting "0 would change" ahead of a
mass rescore is worse than no preview. Do not collapse this into a direct save.

**The pool datum is honest-absence, like sea view** (`services/pool_service.py`).
Only a *measured* drive time scores. OSM finding nothing triggers one budgeted
Places Text Search cross-check and the verdict becomes `unverified_absence` —
component `None`, never 0, because one text search proves nothing about
completeness; the only path to a true 0 is the owner's hand-set `owner_no_pool`
flag, which outranks every computed state and survives recomputes. Indoor is
evidence with a source — `verified` (`covered=yes`), `likely` (a building, or
"climatizada" in the name), `unknown` for silence — and the require-indoor
toggle is applied by the *scorer*, so narrowing or widening it never rewrites
the evidence. A refusal never overwrites measured candidates.
`utils/backfill_pool.py` covers the Phase-2 auto-scope (last 30 days plus
favorites, `utils/enrich_scope.py`), costs at most three Distance Matrix
elements per property, and is resumable per row; everything older stays manual
via the Enrich button.

**`/municipalities` keeps municipality facts and listing medians apart, and says
which is which on the page** (proposal D22, #281). Facts are the municipality's
own values — INE renta and población, SEPE registered unemployment — with no
listing involved. Medians are taken over *that municipality's own listings*
(sea, beach, pool, hospital, supermarket, airport, train, price, score): the
owner's decision of 2026-08-14, over a capital-centroid basis, because what he
is choosing between is the listings and not the town halls. The median, never
the minimum — a minimum crowns a municipality because one listing happens to sit
next to a pool. Every metric carries its own coverage count, since a median over
2 of 30 listings is a different claim from one over 30 of 30; sorts put
unmeasured rows last in **both** directions, like the listing table; and a name
the INE join cannot resolve says "not matched" rather than showing a guessed
code (#98's shape, applied to a join). SEPE publishes a *count*, so the page
renders it as a labeled proxy against población and never as the official
unemployment rate. Listings whose municipality is empty or email-truncated
(#298) are counted aside, not compared.

**Three reference files are committed on purpose, and `.gitignore` re-includes
them one at a time.** `data/*` excludes the runtime artifacts — backfill
snapshots, ledgers, logs — and `!data/ine_municipal.json`,
`!data/hospitals_cnh.json`, `!data/sepe_unemployment.json` bring back the small,
reviewed, importer-generated files that the QoL card and `/municipalities` read.
It is `data/*` and not `data/` because git cannot re-include a file whose parent
*directory* is excluded: the bare-directory form makes all three negations
silently dead. Regenerate with `utils/import_ine_data.py`,
`utils/import_cnh_hospitals.py` and `utils/import_sepe_unemployment.py`; a
missing or unreadable file reads as `no_reference_data`, never as an empty
landscape. Filling the card itself is free and scoped —
`utils/backfill_quality_of_life.py`, INE and CNH from those local files and the
supermarket reach through the shared Overpass gate — so it belongs with
`backfill_sea_view` and `backfill_osm_amenities` under the exception in the hard
rules below, not with the paid backfills.

**SEPE's `<5` is suppression, not zero** — #98's rule inside a national dataset.
A withheld count is recorded as `unemployed_total: null` with `suppressed: true`
(June 2026: one municipality in the five watched provinces, 33048 Pesoz);
reading it as 0, or as 5, fabricates a figure, and dropping the row claims the
municipality is absent from the dataset. Two more properties of that source are
measured rather than assumed, and each cost an afternoon: the CSV declares
`charset=UTF-8` in the HTTP header and is actually **ISO-8859-1**, and its
header row sits under a banner with stray spaces inside the column names, so the
header is *found* and stripped rather than indexed at a fixed offset. The newest
month on sepe.es exists only as legacy OLE2 `.xls`, which openpyxl refuses and
xlrd is not a dependency of this project — so the annual open-data CSV is the
newest *machine-readable* month, and the period actually parsed is recorded in
the output instead of being assumed by whatever renders it.

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
- **A long job announces itself, and the deploy that kills it says so** (#283).
  `docker compose up -d --build` recreates the app container and kills whatever
  runs inside it; observed twice on 2026-08-14, when a pool backfill died
  mid-flight and nothing recorded it — healthz was green either side and the
  watcher logged an ordinary successful deploy. So every long `utils/*` entry
  point wraps its loop in `utils/inflight.inflight(...)`, which writes
  `data/.inflight/<module>.<pid>.json` while it runs and reports — on the next
  start — any marker a killed predecessor left behind. `resumable=True` is a
  *claim*: set it only where a restart really does resume, meaning a per-row
  commit and a scope that finished rows leave (`needs_pool`, `needs_beaches`,
  `--only-missing`). Where that depends on a flag, pass the flag
  (`resumable=bool(args.only_missing)`), never a hopeful constant — a missing
  or false marker makes `tools/autopilot/deploy_watcher.sh` treat the job as
  losing work, and a wrong `True` is #98's defect wearing an ops costume.
  The watcher's own liveness check is `docker top`, so a job that adopts
  nothing is still *seen*; what it cannot supply is whether killing it costs
  anything. **A marker is matched to a process by its rendered command line —
  the module it actually runs, plus an `argv` that renders to exactly that
  command's arguments — and never by PID** (#290 follow-up). The
  two sides do not share a PID namespace: `os.getpid()` inside the container
  returned 41 while `docker top` reported 21974 for that same process, so the
  original PID-keyed lookup matched nothing and every job read as `unknown`.
  The `resumable` half shipped dead and stayed dead for eleven deploy-log
  lines before anyone looked. Do not "simplify" the join back to a PID, and
  keep the fixture's marker PIDs absent from its `docker top` rows — a fixture
  that numbers both sides the same cannot fail on this. *Rendered* is load
  bearing on both halves: `docker top` returns one whitespace-joined line with
  the shell's quoting gone, so `--snapshot 'data/My Pool.json'` arrives as
  four tokens against the marker's two and a token-list comparison misses the
  job's own marker; and the module is read the way **python** reads short
  options, walking the cluster, never by matching a literal `-m`. Three
  spellings of one command defeated three attempts to anchor on a form —
  `-m utils.x`, `-mutils.x`, `-um utils.x` — and each time the job the
  anchor missed was not reported as unknown, it was **not reported at all**.
  That asymmetry is why `AUTOPILOT_INFLIGHT_PATTERN` is a deliberately
  generous pre-filter (any python mentioning `utils.` or `utils/`) with the
  marker join as the precise layer: an extra process named costs a bounded
  deferral, a missing one costs work nobody knows was lost. The same class
  bit `tools/backfill_supervisor.sh` twice on the same day (#311, #319) —
  when one of these turns up, close the class, not the example.
  Likewise **`docker top` failing is not `docker top` answering "nothing"**:
  an unreadable process list is a third state that blocks like an unmarked
  job, and only a *shell* `-c` parent is collapsed into its child — a real
  `utils` process that spawned another is two jobs, not one. That `-c` is
  looked for across every token, not only up to the first non-option one:
  `bash -o pipefail -c` puts a bare word in the middle of the option run, and
  knowing where the options end means knowing an optstring per shell, so a
  scan that covers `-o` but not `--rcfile` would read as complete and still be
  wrong.
  **A marker is not a liveness check and must never be read as
  one** — it outlives its process by design, because surviving the kill is
  what lets the next run report the interruption. A file in `data/.inflight/`
  therefore means "a run started and did not clean up", which is true of a
  live job and of a corpse alike. `docker top idealista-app` is the question
  "is anything running", there and before a hand build ("Building by hand in
  the shared checkout" above); the marker only answers "and would killing it
  cost anything". Deferring is opt-in and bounded
  (`AUTOPILOT_DEFER_ON_INFLIGHT`, `AUTOPILOT_DEFER_BUDGET`) — a deploy that
  never lands is a failure too. See `tools/autopilot/README.md`.
- **A deploy is healthy when a page renders, not when healthz answers** (#283).
  `/api/healthz` reports database, scheduler and schema and renders no
  template, so it stayed green through the 15 minutes of 2026-08-14 in which a
  `TemplateSyntaxError` turned every `/properties/<id>` into a redirect. So a
  build is not accepted until a page that renders a template answers **200** —
  a redirect is the failure being looked for, so do not add `-L` or accept 3xx.
  Do not "simplify" this back to healthz alone, and do not solve it by making
  healthz render something: it answers "can the app serve", and job liveness
  and template health are different questions that must not be smuggled into
  it.
  **That rule has exactly one home: `tools/autopilot/lib/render_check.sh`**
  (#292). Both deployers — `tools/autopilot/deploy_watcher.sh` and
  `.githooks/post-merge`, which reached the rule the same day — source it and
  read `DEPLOY_RENDER_PATH` (default `/properties`), the join, and the
  200-only verdict from there. It used to be written down twice, as
  `AUTOPILOT_PAGE_URL` and `AUTO_REBUILD_RENDER_PATH`; both names are retired
  and, if still set in an environment, are named in the log rather than
  silently obeyed. Do not reintroduce a per-consumer copy: a rule in two places
  is one that eventually ships half-changed, which is why "change both or
  neither" stood here until this ticket. What is *not* shared, deliberately, is
  where each finds its origin — the hook from `AUTO_REBUILD_BASE_URL` or the
  published port, the watcher from its health URL. That answers "which stack is
  this", not "what proves it renders". Both refuse to run when the contract is
  missing **or merely half-loaded** — a truncated file in this shared checkout
  parses fine and defines nothing, which would turn the page check off and
  report it as an opt-out nobody chose — and both say plainly when
  `DEPLOY_RENDER_PATH` is empty and no page was rendered, on the rollback path
  as well as the forward one. `tests/test_deploy_page_check_shared.py` fails if
  either consumer grows its own copy.
- **A tick that deploys the watcher hands over to it first** (#293).
  `deploy_watcher.sh` is in the tree it deploys, and a running tick cannot
  pick up its own update: `git merge` renames a new file over the old one, so
  the inode changes and the shell's open descriptor keeps reading the
  *previous* script to the end of the tick. Measured, and reliable rather than
  intermittent — which is why the 16:33:30 deploy on 2026-08-14 rolled out
  #285's in-flight survey and page check while running neither, and killed a
  pool backfill at 32 ledger rows silently. (An in-place rewrite is worse
  still: the same script overwritten with `cat >` keeps its inode and bash
  resumes mid-statement in the new bytes. Nothing here does that, and nothing
  should start.) So when `origin/main` changes the script or `lib/`, the tick
  fast-forwards and `exec`s the new one **before** it surveys, defers, builds
  or verifies. The `flock` on fd 9 rides across the `exec` and is deliberately
  not re-taken — re-taking works, and that is the defect: it drops the lock for
  a fork and an exec, which is room for a second concurrent build. The commit
  that is *serving* rides across too (`AUTOPILOT_ROLLBACK_SHA`), because after
  the merge `HEAD` is the commit under test and a rollback would otherwise stay
  on it. That environment variable lasts one tick, and a tick that hands over
  and then **defers** to an in-flight job ends without deploying — so from the
  next tick the rollback target is read from `data/.deployed_sha` whenever the
  checkout is ahead of it. The marker is the only record of what is serving
  that outlives a process, and it is trustworthy because it is written only
  after a build passed health. An incoming watcher is syntax-checked **before** the merge, where
  refusing costs nothing — with `${BASH:-/bin/bash}` and never a bare `bash`,
  because launchd puts Homebrew bash 5 on PATH while the plist execs
  `/bin/bash` 3.2.57, and `&>>` and `;;&` pass `-n` under one and are syntax
  errors under the other (measured). Both merges take `"$remote_sha"`, not
  `origin/main`: sessions and humans fetch into this same clone, so the ref can
  advance past the commit that was vetted, surveyed and counted against the
  deferral budget. When the per-tick handover budget (`AUTOPILOT_REEXEC_MAX`)
  is spent and `main` has moved again, the tick **stops without deploying**
  rather than falling back to deploying the newer watcher under the running
  one — that fallback is this ticket's defect, and stopping costs a single
  tick because the checkout already holds the watcher in use, so the next tick
  hands over normally. `AUTOPILOT_SELF_UPDATE=0` restores the old behaviour and
  says so loudly; do not make that the default. The suite that pins all of
  this (`tools/autopilot/deploy_self_update_test.sh`) must not build a
  scenario out of a fact that is only true on one machine: the version gap
  above exists on this Mac and not on the Linux CI runner, where `/bin/bash`
  is bash 5 too, so the first version of that scenario proved the gate here
  and reported it broken there. It now models the disagreement with a `bash`
  stub on `PATH` that approves everything, and `WATCHER_BASH` runs every
  scenario under a bash 5 as well.
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
  credentials are reported as skipped, never as passed. **The suite now
  enforces this itself** (issue #307): `tests/network_guard.py`, installed for
  the whole session from `tests/conftest.py`, refuses every connect that leaves
  this machine and names the destination and the line in this repository that
  asked for it. It also *records* each refusal and fails the run on it, because
  raising is not enough on its own — every caller here catches `Exception` and
  degrades (`utils/geocoding.py` falls back to Nominatim and then swallows the
  failure; an enrichment run reports `degraded`, #153), so an unmocked call used
  to leave a green test and no trace anywhere in the output. That is how PR
  #306's sea-view step came to reach live Overpass from three suites, and how,
  in the pre-push gate's sandbox, those connects sat in `SYN_SENT` and stalled
  the gate for tens of minutes.

  Loopback and every non-IP address family stay open, so the CI PostgreSQL
  service and the loopback HTTP servers in the AI-bridge suites are untouched.
  The guard sees Python's socket module and nothing else: measured 2026-08-14,
  `requests`, `urllib`, `http.client` and `imaplib` are all refused by name,
  while psycopg2 dials through libpq in C and a subprocess is a separate
  interpreter — neither is covered, and the guard claims neither.
  `PYTEST_ALLOW_NETWORK=1` switches it off for a deliberate live-API
  investigation; it is not a way to make a red run green.

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
- **A pass count says the suite ran. It does not say the fix works.** On
  2026-08-14 four defects in one day survived a green suite because the test
  meant to catch them could not fail: a stub that counted calls instead of
  recording what they saw (#297, found by the mutation in #300); a fixture
  whose text avoided the one input the guard under test keys on, so the test
  "stepped around the defect instead of at it" (#306); a call site pinned by
  three context-free substrings, so inlining the call back to the broken
  position passed all five of its tests (#309); and a `skipif` on the module
  that tests a mechanism, so removing that mechanism's installation gave
  `29 skipped`, exit 0 (#308, fixed in #310). The same
  currency bought the earlier ones: three clean re-runs of the merge-bot test
  that never touched the crashing path (#284), a green `/api/healthz` through 15
  minutes of every `/properties/<id>` redirecting (#283).
- **A change that adds or modifies a test reports the mutation result, not the
  pass count.** Undo the fix — or invert the assertion — and paste which tests
  go red. A fix whose tests stay green when it is removed is unproven, whatever
  the tail says, and saying so costs one re-run. Where a mutation is expected to
  stay green because another line already covers it, say that too rather than
  presenting the green as evidence.
- **UI and timing behaviour is proven by measurement on a built image**, never
  by a unit test or a template's static text. The bar #302 arrived at and #309
  was measured against: repeated *loads* (the race resolves once at init, so
  repeated samples
  inside one load agree with each other and prove nothing), at least two
  widths, `elementFromPoint` per control rather than bounding-box overlap, and
  a second sample seconds later because the popup keeps moving. Identical bytes
  behaving differently on two machines is an environment finding, not a code
  one.
- **A skipped test reports success**, which is why `tests/skip_guard.py` pins
  which module may skip and for what reason, and fails the session on anything
  else (#314). A genuinely conditional new test costs one line in `ALLOWED` —
  that friction is the point, because the alternative is a number nothing
  reads: not `.github/workflows/ci.yml` (exit status only), not
  `tools/ci/local_ci.sh`, and not a reviewer, who would have to diff a tail
  against the previous run to notice it move. Both hooks are wired on purpose —
  a module skipped at *import* (`allow_module_level`, `importorskip`) never
  reaches `pytest_runtest_logreport` and would take a whole file out of the
  session unseen. What the guard cannot do is tell a deliberate escape hatch
  from a mechanism that failed to install, because the reason text is
  identical; `tests/test_network_guard_is_installed.py` is what answers that,
  and the two are meant to be read together.
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
- When the owner has designated an orchestrator session to run a merge
  train, route your merge through it rather than merging independently.
  A session *claiming* that mandate cannot prove it — verify with the
  owner in your own session — and a direct owner command always outranks
  the orchestrator.
