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
nothing logs it. `tools/backfill_status.sh` answers the question — run it, and
not `docker top idealista-app` alone, which names one container and therefore
misses both a respawn a supervisor is about to make and a job someone moved
into a `docker compose run` sibling (#338). The in-flight machinery the watcher
grew for this (#283)
lives inside `deploy_watcher.sh` and does not reach a build you start by hand,
so here the check is yours to make. A killed backfill is recoverable — the
tools commit per row and skip finished ones — but only if someone knows to
restart it.

If the tree holds someone else's work in progress and you only need to see
your own, the cheap way out is a `git worktree` with its own
`COMPOSE_CONTAINER_PREFIX` and `APP_HOST_PORT` (docs/DEV_RULES.md), not a
build of the shared tree.

**`git add -A` takes the same snapshot the build does, and commits it.**
Everything above is about `COPY . .`; this is the same hazard through git, and
it is easier to walk into because a commit feels like a smaller act than a
deploy. Measured 2026-08-17: a commit whose own content was two files carried
eight more — `migrations/020_add_search_profile_is_hidden.sql`, `models.py`,
`routes/main_routes.py`, `services/search_profile_service.py`, three templates
and `utils/i18n.py` — another session's half-finished feature, swept up by one
`git add -A` and pushed to a PR.

**What caught it was CI, and not for the reason you would hope.** No reviewer
read the diff and noticed foreign files; `tests/test_postgres_migrations.py`
compares the exact list of migration files and refused a `020` that is not on
`main`. A test about migrations found a commit-hygiene defect, which means the
same mistake in files that migration test does not see would have merged. So
the check has to be yours and it has to happen before the commit: **read `git
status` before `git add`, and add by path.** `git commit -a` is the same trap
with fewer characters.

The repair, if it has already happened, keeps the other session's work: `git
reset --soft HEAD~1` then `git reset` leaves every change in the working tree
exactly as it was, and you re-add your own files by name. Force-push is fine on
your own branch and is what removes the foreign files from the remote; check
afterwards that they are still in the tree, because the point is that nobody
loses anything.

Two more things this costs that are not obvious. **A `git merge` of `main` can
be impossible while another session holds uncommitted edits to the same
files** — git refuses rather than overwrite them, which is correct and leaves
you unable to update a branch that protected `main` requires to be current. Do
the merge in a `git worktree`, the same escape the paragraph above offers for
builds. And **a squash-merged branch conflicts with its own follow-up**: a
branch cut before the squash landed carries the same file as an unrelated
`add/add`, and the resolution is your version, since it is the squashed one
plus whatever you fixed since — verify that by diffing the two rather than
assuming it.

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

**Retiring a subscription and hiding one are different questions** (owner
request, 2026-08-17). `is_active = false` *archives*: the subscription leaves
the chips and is offered under *Archive*, one tick away, because a saved
search that stopped still holds listings worth reaching. That is the wrong
answer for a search that is still running and that the owner does not want on
screen — production carries fourteen subscriptions, eleven of them active,
three of those created by the ingester and holding one listing each, and every
one takes a chip on the one working page. So `search_profiles.is_hidden`
(migration 020) takes a subscription off the screens: no chip, no menu entry,
not under *Archive*, and its listings are out of `profile_id=all`, which is
what /properties, /map and `properties/export.csv` all define "all
subscriptions" against. The control is on `/profiles` and only there — the
page that lists every subscription side by side, including the hidden ones,
because a page that hid them from their own control would leave no way back.

Four things about it are deliberate. The rule has one home,
`SearchProfileService.visible_clause()` / `list_visible_profiles()`, and
`list_profiles()` still returns **everything** by default: ingestion reads that
list to match an email against each profile's `email_matchers`, and a hidden
subscription that stopped matching its own mail would send those listings to
the catch-all — a data change wearing a UI change's clothes. A hidden id named
explicitly in `profile_id=<id>` still renders, under its own heading in the
menu with its own checkbox, because a selected id with no checkbox reads as
"nothing ticked" to the page's own script and the next Apply would silently
widen the view (the same reason an unknown id keeps one, #104). The menu
carries a line saying how many subscriptions and how many listings it is not
showing — the owner chose this, so it is a disclosure and not a warning, but
without it "all subscriptions" reads as the whole table. And the **catch-all
cannot be hidden at all**: it receives every email that matches nothing else,
so hiding it would take listings off the page as they arrive, which is why
`edit_profile` already forces the default profile active.
`tests/test_hidden_subscriptions.py` pins all of it.

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

**The coastline does not follow the rías inland, and the verdict now records
the point it measured to** (#334, the "Selorio report" of 2026-08-15 — it
arrived as a direct owner request and was filed retroactively). The reported
defect was that OSM's
`natural=coastline` runs up an estuary to the tidal limit, so a plot 4 km from
a ría scores a sea view against it. Measured against real OSM data for all four
candidate rías, it does not: the coastline **closes across each mouth** and the
estuary is mapped separately as a `natural=water` / `water=river` multipolygon
reaching much further in — Villaviciosa 7.13 km past the nearest coastline
node, Avilés 4.60, Navia 4.90, the Nalón 4.37 and 7.29. Not one coastline way
in any of those four boxes carries a name either (the only named ones are rocks
and islets near the Nalón, *Islote La Ñera*, *El Peñón*), so a rule keyed on a
named ría way has nothing to read. The nearest coastline to an inland plot is
therefore the mouth, which *is* open sea, and property 125's `likely` is
correct: re-run against live EU-DEM it gives `clear_line_of_sight` to the mouth
at 4173.8 m, and so does a ray on the same bearing to open water 5839 m out,
with 9 null (over-water) samples confirming the far end really is sea.

What was wrong is what the verdict *said*. Its sight line runs 2.8 km up the
ría channel before it reaches the sea, and the page announced "Sea view likely"
either way. So `geometry.target_lat` / `target_lon` now record the coastline
node — additively, with no cache bump, because the field changes no verdict and
re-querying 67 cells through a 5 s gate to gain it is not worth it — and the
property page, the list and `properties/export.csv` carry it. A distance and a
bearing are not a substitute: both are stored rounded to one decimal, so
casting the ray back out lands metres off the node, which is enough to put the
reconstruction on the far bank of a 300 m channel. And a `likely` resting on
terrain alone is now named *"Terrain allows a sea view"* rather than asserting
one — `state_label_key()` in `services/sea_view_service.py` is the single home
of that distinction, for the three templates that draw the badge. What it must
not do is soften a `likely` the listing itself claims, or a hand-set one.
`tests/test_sea_view_target_recorded.py` pins all of it against the real
coastline in `tests/data/`.

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
renders, saying it was not measured. **The beaches come from `natural=beach` in OpenStreetMap since 2026-08-19**,
in the same Overpass query that answers the presets -- so the seventh and last
Places call a listing cost is gone, and the beaches read a cache entry the
presets have already filled. One consequence is worth knowing before it
surprises someone: sharing the query couples the two, so a refusal now refuses
both. The rule "a beach must never turn a good travel run into a degraded one"
survives in the only form it still has content in -- the beaches contribute
nothing to the target tally, and the run's verdict is whatever the presets
alone make it -- because the old scenario, a beach lookup failing while the
presets are fine, cannot occur when one query answers for all six. The
Places-era rules stay and mostly idle: they exist because `natural_feature` +
"playa" returned campsites and beach bars, and a claim about the ground has far
less to refuse. `natural_feature` + keyword `playa` was the
Places pair, measured against the live API on 2026-08-11: it returns real
beaches in Asturias *and* in Galicia (where they are named "Praia"), while the
legacy `tourist_attraction` + `playa` pair returned a fountain, a swimming pool
and the town itself. Google lists the same beach under several place ids, so
identical names are collapsed to the nearest one.

**`listing_status` is `active` by default, so `active` is only shown when
somebody established it.** The column is written at ingestion and nothing
verifies it; measured 2026-08-15, 1 of 311 land rows had ever been checked, and
`/properties` drew the other 310 exactly like confirmed live listings — property
192, withdrawn by the advertiser on 08/05/2026, among them. That is #98 in the
status column: an absence of measurement rendered as a measurement.
`services/listing_verification.py` owns the rule and is the only thing the
surfaces read. `removed`/`sold` always show, because no writer sets them by
default; `active` shows only with `listing_status_source` of `check` (the
scraper read the page) or `manual` (the owner looked); everything else,
including `ingest`, NULL and the stored `unknown`, presents as **`unchecked`**.
That is a fourth *presentation* state, not a fourth database value — no
migration, no row rewritten, the database keeps what it knows. The module holds
both readings of the rule, `read_verdict` for a row and `verified_expression`
for a query, because the list draws them together: the coverage line beside the
result count ("3 of 4 verified against Idealista") is the disclosure an
unchecked row cannot make for itself, since badging ~100% of the table
"unverified" is noise, and a header disagreeing with its own badges would be a
third wrong number. A check older than 30 days keeps its badge and loses the
green — it verified something, just not about today. The CSV export and
`to_dict` carry the verdict too; a report built off the raw column is how a dead
listing got recommended.

**Idealista blocks the checker from this machine, and the app says so instead of
retrying into the wall.** Measured 2026-08-15 over 76 properties, one at a time
behind the service's own throttle: every call hit DataDome, zero listing pages;
`curl` from the *host* with full browser headers gets 403 with the same block
body, so it is not the container, and only the owner's real logged-in Chrome
renders a listing. Defeating that is not on the table — it is bot-detection
circumvention, and a headless profile would be one more thing to lose to
DataDome's next update — so the honest half is what shipped. `RefusalBreaker`
(`services/listing_status_service.py`) counts refusals *across* calls, and after
three in a row the service answers from what it already knows for 30 minutes
instead of spending a request per press; the cooldown buys back exactly one
probe, and a refusal re-arms it, because it heals on evidence and not on a
timer. Each refusal carries a reason — `blocked`, `backing_off`,
`not_the_listing_page`, `http_error`, `timeout` — so the page can say "idealista
is refusing this machine" rather than reporting 76 unrelated failures. None of
this changes the #136 storage contract: an `error` still writes nothing, not
even `listing_last_checked`. The breaker is process-local and shared by every
instance (each caller builds its own service), which makes it exactly the kind
of state `tests/conftest.py` resets between tests — it is reset there, and
skipping that reset makes 21 tests in three other files fail with no reference
to the file that armed it. There is still **no scheduled sweep for
`Property`** and no `check_all_active_properties` to run — the bulk paths select
on `Land` — so the per-listing button and the hand-set status are the way in.

**Distance to the sea is a scoring criterion, and it reuses the sea-view
coastline client.** `services/sea_distance_service.py` scores straight-line
metres to the OSM coastline, but it does **not** fetch that coastline: it calls
`fetch_coastline_points()` in `services/sea_view_service.py`, which already owns
the Overpass side (one query per 0.1° cell, cached a month, throttled, plain
User-Agent token because Overpass answers 406 to anything else). Do not add a
second Overpass client — measured the hard way, a per-property or wide-box query
gets 504s.

**"Cached a month" is true of the deployment only because `REDIS_URL` is set**
(#356). `utils/cache.py` falls back to `SimpleCache` without it, and that lives
in the process — so until 2026-08-16 the 30-day intent lasted exactly as long as
the interpreter that filled it, and every deploy restart, every `docker exec`
and every `compose run` sibling re-paid Overpass for cells the last one had
already fetched. `docker-compose.yml` now runs a `redis` service with
`--appendonly` so the cells survive the container too. If a run is suddenly
slow, check that the app really has `REDIS_URL` before looking anywhere else:
the code, the docs and the deployment disagreeing is what this cost.

Two consequences worth knowing before an incident, not during one. **Rate
limiting rides the same switch** — `app.py` builds its `Limiter` with
`storage_uri=REDIS_URL`. What that buys here is *persistence across restarts*,
not sharing between workers: the Dockerfile runs `--workers 1 --threads 4`, so
there has only ever been one process counting. What it costs is a failure mode
the limiter never had, because `memory://` is a dict and cannot refuse. The
limiter therefore carries `in_memory_fallback_enabled=True, swallow_errors=True`
— measured before they were added, stopping Redis made the 15 rate-limited
routes in `routes/api_routes.py` raise `ConnectionError` out of the request,
since flask-limiter checks the limit inside it and only `HTTPException` is
handled. Those are the AI analysis, the bulk and manual enrichment, the email
ingest and the status check: the buttons someone presses when something is
already wrong. With the flags, an outage relaxes a shared limit to a
per-process one and never fails a request. Do not remove either — `swallow_errors`
alone deletes the limit instead of degrading it. And **a shared cache is a shared blast radius
for 30 days**: a poisoned coastline entry used to die with its process and now
does not. The key carries a version (`sea_view_coastline_cell_r25000_v1`), so
bumping `_v1` is the escape hatch — that is what it is for, and it is cheaper
than reasoning about which cells are wrong. A cache that cannot be reached is a
*miss*, never an error: `utils/cache.py` guards every read and write, because
on the Google paths the write happens after the billed call.

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

**A coordinate that is not the parcel measures nothing about it, and every
consumer asks before it measures** (#358). `location_accuracy` is Google's own
word for what it matched: `precise` is an address, anything else is a locality
centroid. At 11:08 UTC on 2026-08-16 that was **532 of the 725 located rows**,
280 of them sharing a point with another listing across 78 points (21 listings
on the worst one, 39 of the sharers labelled `precise`). Two hours earlier the
same query said 466 of 652: the set grows with every ingest, so re-measure
rather than quoting these. `sea_view_service` has refused such
a point since #196; `sea_distance_service` and `property_travel_service` did
not, so a listing scored the centroid's sea distance and the centroid's drive
times, and nothing on the page said so. Properties 460, 461, 574 and 641 are
four different streets of Santa María del Mar resolved to one point 23.8 m from
the coastline — including `San Miguel de Quiloño s/n`, which is inland by the
airport. It is the *fourth* way a geocode goes wrong and the first three guards
all pass it: not a coarse type (#331), the right province (#348), an accepted
accuracy label (#321). Right place, wrong precision.

The policy has one home, `services/coordinate_quality.py`, and it is
`sea_view_service`'s idea rather than a second one: an approximate coordinate
may still decide a question **when the answer cannot change anywhere inside the
5 km slack**. So a measured distance is scored only if its lower and upper
bounds score the same — which for a precise row is one number twice, so nothing
about that path is special-cased — and a travel duration only if both ends of
the slack, converted at the mode's own assumed speed by
`estimate_duration_seconds`, land in the same flat region. "No coastline within
17 km of the centroid" survives it, which is why `searched_m` on an approximate
row is 12 km: the radius the answer is guaranteed for *around the parcel*.

Three consequences worth knowing before changing it. **Travel used to refuse
before the Places and Distance Matrix calls, and since 2026-08-17 it does not**
(owner decision). #358 built three things and only the purchase refusal is
gone: the scorer still applies the slack target by target, and every surface
still derives `approximate_origin` from the row's accuracy and captions it, so
what comes back is a measurement and not the claim that it is the parcel's —
the state 115 rows in `Plots 0–50 km` have carried since before #358 landed.
The reason it can go is that the rows the owner wants numbers for cannot become
precise: their adverts give a hamlet or hide the address, so a re-geocode
returns the same centroid, and the choice was a measured duration that says
what it is against no answer at any price. What it costs is that a recalc over
such rows is **no longer free** — ~$0.36 a listing where it used to walk past —
so read the billing rule above before pointing one at a wide scope. Nothing
unattended reaches that code (`AUTO_TRAVEL_ENRICHMENT` is false), which is what
keeps the lift a spending decision rather than a spending default. The run
records `api_status.origin_accuracy`, and the only refusal left is a row with
no coordinate at all. The scorer and the templates read the row's *current*
accuracy through `parcel_measurement` and `effective_travel_state`, not the
stored block, because 264 sea blocks and 532 travel blocks say `ok` from runs
that predate the rule and no amount of re-reading them reveals it. The
exemption is asked of the travel *average*, never target by target: 690 of
those durations sit under `best`, where the slack cannot move them, against 9
past `worst`, so keeping the individually-safe ones would keep the near ones
and report ~100 for a listing whose real mix is nothing of the sort. And a
shared
coordinate is surfaced as evidence, never as a gate: two flats in one building
share a point legitimately, the coordinate alone cannot tell that from four
plots on a centroid, so it is shown next to the coordinate and counted by
`utils/report_coordinate_quality.py` (free, read-only) — repairing the rows is
`utils/refresh_property_accuracy.py`, which is billed and therefore the owner's
call.

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
columns and `scoring` payload of all 393 rows as they stood before. Deleting it
is not tidying up; it is discarding the only way back.

`utils/restore_score_snapshot.py` puts both halves back, in one transaction,
because weights restored without their scores (or the other way round) is a
state the app never had. It **reports and exits** unless `--apply` is given,
writes a backup of what it is about to overwrite first (`--no-backup` is a
thing you say out loud, never a default), parses every row before writing any,
and restores exactly the columns a row carries — this snapshot has no
`enrichment`, and nulling it would erase measurements the weight change never
touched. Rows ingested *after* the snapshot are the honest hole: they were
scored under the config being rolled back and nothing knows their earlier
values, because they had none, so the tool names them and `--rescore-uncovered`
recomputes them under the restored config. The snapshot primitives now live in
`utils/score_snapshot.py` — `backfill_pool`, `recalc_sea_distance` and
`recalc_property_travel` had three copies of them and this was nearly the
fourth.

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

**What is 1.1 km away is a datum, and an empty page was answering "nothing"**
(#437). Property 793 is a plot advertised as a "quiet environment surrounded by
nature" with a cement works 1.12 km off, a coal yard at 1.57, Repsol's LPG
spheres at 1.79 and a coal-fired power station at 2.13; the page showed net
income, supermarkets, hospitals, beaches and drive times, and not one word about
any of it. `services/hazard_service.py` writes `enrichment["hazards"]` -- free,
OpenStreetMap, one query per listing on the *free* pass through
`EnrichmentService._overpass_elements` -- with the four states this file keeps
insisting on: `ok`, `none_within_radius` (a measurement), `unavailable` (a
refusal, never cached, never allowed to overwrite an earlier measurement) and
`no_coordinates`. The card renders on **every** property page, including the
rows nobody has scanned, because the question it answers is "is anything bad
near this plot" and silence reads as no.

**The tag is not the severity, and that was measured before it was written.**
One live Overpass answer at 793's own coordinate, 6 km, ten candidate tags,
committed verbatim as `tests/data/osm_hazards_xivares_793.json`: **144
elements**, of which 82 are storage tanks and 42 are `landuse=industrial`. On
the identical tags sit *Alskin Cosmetics* (`industrial=laboratory`), *Neoalgae*,
*Fábrica de Hielo* -- an ice factory -- *Talleres Prendes*, eight *polígonos
industriales* and a lorry park. So `services/hazard_rules.py` is the sibling of
`services/place_rules.py` and the rule is that **a hazard has to say what it
is**: `plant:source=coal`, `content=gas`, `industrial=steelmaking`,
`landuse=landfill`, or the word *cementos* in its name. `landuse=industrial`
and `man_made=works` never qualify alone. The name has to be read at all
because OSM is sometimes coarser in its tags than in its labels -- El Musel's
coal yard is mapped `landuse=quarry` and the cement works carries no `product`
tag -- but it is read as **words** and the **more severe** of the two verdicts
wins. Both halves of that are review findings with a reproduction behind them:
substring matching read *Bioquímica* as a chemical works and *La Cantera* (the
ordinary Spanish word for a club's youth academy) as a quarry, and letting the
name win outright reported an LPG tank on *Polígono La Cantera* as a moderate
quarry over its own `content=gas` -- understating a real hazard, which is
strictly worse than reporting a spurious one. In the same family, a lifecycle
*prefix* is not a closure: `was:name=Ensidesa` on
*Acería de Veriña - ArcelorMittal* records that plant's renaming history, and
reading it as "gone" erased the one hazard the feature exists to catch, so only
bare `disused`/`abandoned`/`ruins`/`demolished`/`razed` refuse, and `historic`
refuses by value (`monument`, never `archaeological_site`, which describes what
is *under* a working landfill). Do not copy the table into a second caller;
import it.

**Sibling elements collapse into their facility**, which is #325's
hospital-indexed-room-by-room in a new place: `facility_key` takes the operator
before the name, and `merge_keys` folds a name that *contains* an operator into
it, so *Turbina A*, *Turbina B*, two stacks, *Vertedero ArcelorMittal* and
*Acería de Veriña - ArcelorMittal* are one entry and not six. Elements with
neither operator nor name cluster by position at 500 m. But **a key says who
runs it and never where it is**: keyed members are split again by
`FACILITY_SPAN_M` (2 km), because `operator=Enagás` names a national gas
transporter and two of its installations 5.6 km apart collapsed into one item
wearing the near one's distance and bearing. 2 km is measured, not chosen --
the widest real facility in the fixture, ArcelorMittal's tip, acería, turbines
and stacks, spans 1346 m. A cluster is a disc around its anchor and never a
chain, or a line of tanks 400 m apart walks a "facility" across kilometres.
The direction of the guards matters: two rows for one plant over-reports, one
row for two plants hides one.

Four more things are load bearing. **An approximate coordinate cannot support
"1.1 km"** -- `read_verdict` restates the stored block against the row's
*current* accuracy exactly as `sea_distance_service.parcel_measurement` does,
so a centroid gets a band (0.0-6.1 km) and never a point, and the block reports
`guaranteed_m` -- the 6 km scan around the point guarantees only 1 km around the
parcel. A coordinate that has *moved* since the scan is worse than an
imprecise one and gets no restatement at all: `read_verdict` compares the
stored `origin` through `services/enrichment_origin.py` and answers
`stale_origin`, because re-applying today's slack to yesterday's point printed
a centroid's 1.1 km as an exact measurement. For the same reason a row that
*loses* its coordinate keeps its measurement rather than having it replaced by
`no_coordinates` -- that status is not retryable, so overwriting took the row
out of the backfill's scope for good. And the scorer reads `truncated`: a scan
that hit Overpass's element cap and found nothing qualifying is not a clean
neighbourhood, it is a short list, and scoring it 100 was #98 one layer under
a card that disclosed it correctly. **The bearing is recorded and never interpreted**: whether 1.1 km
matters depends on the wind, there is no free per-listing wind rose here, and
writing "downwind" into a measurement field would be the STATUS-002 mistake.
**The criterion ships weightless**, `hazard_score: 0.0` in all six scorers, and
raising it goes through the same dry-run preview the pool weight does --
`WEIGHTLESS_SCORE_KEYS` in `services/property_scoring_service.py` is now what
that gate reads, so a third such criterion cannot be added to a scorer and
forgotten by the gate. And **the scan is deliberately not folded into the
preset Overpass query** the issue suggested folding it into: those presets run
only on the paid path, this runs on every ingest, so sharing would drag a 100 km
aerodrome query into every ingested row to save one round trip on an Enrich
press -- and would invalidate every cached preset cell at a moment when Overpass
was refusing the mini outright (#434).

What OSM cannot answer is named on the card rather than left to be assumed
away: emissions (PRTR-España publishes those, and it is worth its own issue),
measured air quality (Asturias runs a station named *Xivares* inside this very
urbanisation), and a plant approved but not yet built. `utils/backfill_hazards.py`
fills the Phase-2 scope, free and resumable per row; announce it before running
it on the mini, and read `tools/backfill_status.sh` first.

**And what OSM says is not what a `product` tag says.** Three review rounds
against live objects settled the shape of that table, and the settled rule is
narrower than the one it started with. `product=X` names what comes *out*, not
the process that made it: `way/1068457365` is *Balumco*, `man_made=works` +
`product=aluminum`, and the Catalan environmental register describes extrusion
and anodising. Nothing structural separates it from `relation/11519713`,
*Asturiana de Zinc*, which really is a smelter — so no bare metal is evidence,
and AZSA classifies as **nothing** until somebody tags it with a process. That
is the price, and it is written into the table rather than left to be
discovered. In the same family: a lifecycle prefix refuses only the *name*
(`was:name=Ensidesa` on the acería is renaming history, `disused:power=plant`
on a live chemical works is one dead plant on its site), `end_date` refuses on
every documented range form, an operator may absorb a name and is never
absorbed itself, and a *central térmica solar* burns nothing while a nuclear
station is not harmless for burning nothing either.

**Two facts, not one: `complete` and `measured`.** "Carries a complete scan" is
answerable in SQL; "is about this coordinate" is not, and conflating them is
what put a cast over stored JSON in the coverage predicate — where on
PostgreSQL a hand-edited value raises and takes the whole `/properties` count,
and the page, down with it. Nothing in that predicate casts now: the truncation
flag is read as text against one list both languages share, which is also what
makes them agree across a JSON boolean PostgreSQL renders `false` and SQLite
renders `0`. `read_verdict` is the other side and it is **total and
fail-closed** — a readable matching origin, `items` a list, `item_count` an
integer that agrees with it, every measurement finite — because six malformed
shapes each scored a clean 100 or raised into the redirect
`routes/main_routes.py` turns a template error into. A block nobody can read
reads as a block nobody has read.

The scan runs in `enrich_property`'s **advisory pass** (#434/#443), where every
score-neutral step owns its own locked write and nothing decisive waits behind
it. That position is load bearing in both directions: the decisive pass ends by
assigning the whole `enrichment` column from a copy loaded before its network
calls, so a locked write placed *ahead* of it is restored to the older value by
its commit — reproduced with two sessions — and scoring runs *after* the
advisory pass, so it reads the block this scan just wrote.

The same rule governs the scan itself: an element cap reached, a hazard OSM
could not place, an element nobody can parse — each makes the scan *incomplete*
rather than empty, the list badges *Scan incomplete* even with nothing to name,
the CSV carries `Hazard Scan Complete`, the scorer abstains, and
`needs_hazards` puts the row back in the backfill's scope. And the badge on an
approximate row says *near the locality*: 532 of 725 rows share a centroid, so
one scan claiming "Industry nearby" would claim it for every listing in the
village.

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

**`properties.municipality` is free text, so anything that groups by it goes
through one key.** The same place arrives under several spellings — measured
2026-08-16, "Gijón" (57 rows) beside "Gijon" (16), "Castrillon" (28) beside
"Castrillón" (18), 247 rows across 8 municipalities — and both surfaces that
grouped by the raw string reported a partial result as a complete one: the
`/properties` dropdown offered each spelling separately, so picking "Gijón"
showed 57 of 73 listings and said nothing about the rest, and `/municipalities`
keyed on `name.lower()`, which lowercases without stripping accents, and drew
one municipality as two rows with two medians and two coverage counts. The key
is `utils/municipality_codes.normalize()` — the function the INE join already
folds *both* sides of its lookup with — wrapped by `utils/municipality_grouping.
py`, which owns the grouping, the shared filter clause the four listing surfaces
use, and the rule for which stored spelling a human is shown (accents beat
frequency, `MUROS DE NALON` never wins, and the label is always a string that is
really in the table). Do not add a second normalizer, and do not canonicalise on
write: the stored string is what the email said and the input the #298 repair
reads, a derived column would have to be maintained by every writer, and the one
that forgot would hide rows from their own filter — the defect being removed,
relocated. A truncated artifact ("Ovi...") has **no** key: `normalize("Ovi...")`
is `"ovi"`, and folding it into `oviedo` by prefix is exactly the wrong-pick
hazard `resolve_truncated_municipality` refuses. It stays out of the dropdown,
out of `/municipalities` and out of every group's rows.

**The search box also takes a listing URL, and one clause says what it
accepts** (`utils/listing_search.py`). It read `title`, `description` and
`municipality` only, so the most natural way to look one listing up — paste the
link from the alert email — answered "0 properties found" for a row the table
was holding: measured 2026-08-17, `idealista.com/en/inmueble/91523456/` found
nothing while property 351 carried that very id. Two ways in, because two
kinds of URL are stored. The **listing id** (`/inmueble/<id>/`, or typed on its
own) matches `idealista_property_id`, which survives both the `?utm_…` tail the
stored link carries and a language segment that differs from the pasted one.
The **URL itself** matches `url` for the 57 rows of 730 that are fotocasa or an
agency's own site and have no Idealista id at all — after the tracking
parameters are dropped, and only for a query really shaped like a link, since
matching every search against `url` would quietly widen ordinary ones ("terreno"
would pull in every fotocasa link by its path). The four listing surfaces share
the clause the way they already share `municipality_filter_clause`; do not give
one of them its own. Two details are measured rather than assumed: a pasted URL
is matched with `ESCAPE`, because `_` is a LIKE wildcard and slugs are full of
them, and a query of 25 digits names no listing — against this deployment's
PostgreSQL the untyped literal psycopg2 sends returns no rows while the same
value bound to a `bigint` parameter fails with `ERROR: bigint out of range`, so
the guard is what makes the two agree.

**And an empty result says what it looked for.** "0 properties found" was one
sentence for two different facts: no such listing here, and *the query was read
differently from how you typed it* — a pasted link is read as the listing it
names, not as text to match. So when the count is really zero and the query
named a listing, the line beside it says which id, or which link, was searched
for. It renders from `interpret_search()`, the same reading
`listing_search_clause()` is built from, because a page describing a search
that did not happen would be the defect it exists to remove, relocated. Rows on
screen say it already, so the line appears only at zero. **Testing that line
means asserting the page rendered**, not only that the line is absent:
`routes/main_routes.py` turns a template error into a flash and a second render
with no rows, which also shows "0 properties found" and no line — the first
version of `tests/test_listing_search_by_url.py` stayed green through a
mutation that broke exactly that path.

**Listings arrive from fotocasa by link, and the reader is 60 lines because
the page hands over JSON** (#389). Fotocasa is not Idealista, measured
2026-08-17 from this machine: it answers **200** to the bare product token in
`utils/http.HTTP_USER_AGENT` and 403 to `python-requests`, `curl` and a bare
`Mozilla/5.0`, with no DataDome, no captcha and no JS challenge in either body.
The filter is on the client name, so identifying ourselves honestly is
*sufficient* — nothing here spoofs a browser, and if that stops being true the
answer is to stop fetching, not to dress up as one. `robots.txt` allows the
listing page for `*` and disallows `/buscar/`, which is why nothing accepts a
search URL and there is no sweep. The data is in
`<script type="application/json" id="__initial_props__">`, so `parse_listing`
is one `json.loads` — no LLM, no HTML parser, and `trafilatura` (declared in
`pyproject.toml`, imported nowhere) stays unimported.

Three things in that payload are traps and all three are pinned by
`tests/test_fotocasa_source.py` against the real 40 KB block in `tests/data/`.
**The two address blocks disagree about `municipality`**: `realEstate.address`
says `Avilés` (`cityId: 33004`, its INE code) and
`realEstateAdDetailEntityV2.address` says `Llaranes`, the district — read the
wrong one and `utils/municipality_grouping.py` groups four surfaces on a
municipality no INE join can resolve. **`0` is fotocasa's blank**, not a
measurement: the measured plot carries `rooms: 0, bathrooms: 0, heating: 0`.
And **the portal declares its own coordinate inexact** (`coordinates.accuracy:
0`, `address.isExact: false`), so a fotocasa row is stored `approximate` and
never `precise` — `precise` grants zero slack in `services/coordinate_quality.py`
and unlocks a ~$0.36 travel run, and no page claiming exactness has ever been
seen. Both portal flags ride verbatim into `enrichment["import"]` so that
measurement, when somebody takes it, needs no re-fetch.

**And so does the pin itself**, because a re-geocode used to throw it away
(#393). `refresh=True` clears the coordinate *before* geocoding, and
`_build_geocoding_queries` reads the text after "in" in the title -- which for
a plot is a district, not a street. Measured on property 733: the refresh
answered with the Llaranes district centroid, 2447 m from fotocasa's pin, still
`approximate`, so nothing was unlocked and the listing-specific point was gone;
the advert text places the plot in Valliniello, so the query named the wrong
neighbourhood as well. `services/coordinate_quality.py` owns the rule now --
`portal_coordinate` reads the pin off `enrichment["import"]["coordinate"]` and
`improves_on` says only `precise` is worth a swap, because every consumer reads
`approximate` and `approximate` identically. A refresh that answers nothing
puts the pin back too: clearing first meant a refusal left the row with *no*
coordinate, which is worse than the one it started with. Only a portal pin is
defended; a coordinate this geocoder wrote last month has no better claim than
the one it writes today. The 56 rows the out-of-band script imported carry no
such block and are therefore unprotected -- their coordinates *are* portal
pins by that script's own docstring, but the row does not say so, and writing
an inference into a provenance field is the STATUS-002 mistake in a new
column.

**And a coordinate a *person* established outranks the geocoder, in its own
key** (GEO-002). Only a portal pin was defended, so a `precise` somebody
curated returned to `approximate` on the next refresh and the components that
label unlocks went with it. The curation is not hypothetical and neither was
the exposure: measured 2026-08-20, three production rows carried a
hand-established location in **three different ad-hoc shapes** -- 161 and 792
under `enrichment["coordinate_provenance"]` with `method` values that do not
match and timestamps under two different names, 774 under
`enrichment["cadastre"]` -- and **nothing in the repository read any of them**.
161 and 792 both carry a `precise` their own `enrichment["geocoding"]` record
contradicts, the fingerprint of a write made outside the geocoder; 161 survives
today only by accident, because it also happens to carry a portal pin, and 130
of the 132 `precise` rows carry none. **Re-measure rather than quoting those
two**, the way the `location_accuracy` paragraph above says: the morning's
answer was 129 of 130, because the set grows with every ingest.

**And within the same afternoon the pressure this creates produced the wrong
write.** By 15:02Z rows 161 and 792 both carried
`enrichment["import"]["coordinate"]` -- `source: cadastre_manual` and
`cadastre_parcel` -- put there by hand-run scripts. That field means *the
coordinate the source portal published*, and a cadastral parcel centroid is not
that. It works, because `_apply_geocode_outcome` defends the portal pin, which
is precisely why it is the STATUS-002 mistake and not merely untidy: the row now
answers "the portal placed this pin" to anyone who asks, and no reader can tell
those two rows from the 57 fotocasa ones. Nobody was being careless -- there was
nowhere honest to put it, which is the hole this section closes. Moving them is
one `utils/set_property_location.py --source cadastre` per row, by the person
who established them, and is deliberately not done here.

The reason those blocks were ad-hoc is that there was no hand-set path for a
coordinate at all -- the only writers of `location_accuracy` are the geocoder,
the fotocasa import, the `Land` migration and the restore half of
`utils/refresh_property_accuracy.py`, so everything else went through
`docker exec`, the boundary `services/ingest_policy.py` records as the one a
flag cannot close. So the defence ships with its writer:
`enrichment["location"]`, read by `manual_coordinate` and written by
`record_manual_coordinate` in `services/coordinate_quality.py`, set by
`utils/set_property_location.py`. `ensure_coordinates` refuses in front of it
**before the geocode**, making no request at all, the shape `advertiser.enrich`
uses for a hand-set seller verdict; `improves_on` is not consulted, because a
person outranks a better label.

Five things about it are deliberate. It is **not** written where
`portal_coordinate` looks -- a conclusion drawn from the cadastre stored under
"the pin the portal published" is the STATUS-002 mistake above, in a new
column, and that is why the three rows are **not backfilled**: no column
distinguishes a curated `precise` from a Google one, so a person converts them
with the note their own block already holds, or nobody does. A **malformed
block does not stop a geocode**, since the alternative is a row pinned to a
coordinate nothing can correct and nothing can explain, and a **note is
required** for the same reason -- `owner`/`agency` describe themselves, two
numbers do not. **Clearing leaves the coordinate columns alone**: the block is
not guaranteed to be newer than them, so restoring what it displaced could undo
a later deliberate act rather than the one being cleared. And
`utils/refresh_property_accuracy.py` **counts and names what it skipped** --
it is the one caller that runs `refresh=True` over a scope, and folding a
hand-set row into the rows that came back unchanged would report
`precise -> precise` for a row Google was never asked about, which is #98's
defect inside a report. That last one is worth more than its size: it is a
defect of *existing* code that only review could find, because a new call to a
shared function is a change to that function and neither side's mutation can
see it. `tests/test_hand_set_location_survives_a_refresh.py` pins all of it.

**The import reads, shows, and only then writes, because this app cannot delete
a property.** There is no delete route and no `db.session.delete` on `Property`
anywhere in the tree, so a row built from a misread page stays in the table, in
the `/municipalities` medians and in its subscription's comparable pool. The
preview is the only undo there is; do not collapse the two steps. They are also
split by time — ninety links at the 3 s courtesy gate is four and a half
minutes against one gunicorn worker with four threads and the default 30 s
timeout — so reading is a background job and confirming, which makes no network
call, runs in the request. A deploy that kills the container mid-fetch (#283)
therefore costs nothing.

A fotocasa row is created with `listing_status_source` **NULL**, written as
`null()` and not `None`: the column carries a Python-side default of `"ingest"`,
which SQLAlchemy applies to any attribute that is None at flush, so the obvious
assignment reads like the intent and stores the opposite.

**`RefusalBreaker` is per host** (`HostBreakers`). It was process-wide, which
was right while every listing was on idealista.com and became wrong the moment
a second site arrived: idealista refuses this machine permanently, so its
breaker is open essentially always, and a shared one did not degrade a fotocasa
check — it forbade it for thirty minutes at a time without a request going out.
`tests/conftest.py` resets **every** host between tests. In the same family,
`_looks_like_listing_page` knew only `/inmueble/<id>/` and fell through to "any
200 is the listing" for everything else, so a fotocasa URL redirected to a
search page would have been recorded as live — #136's false confirmation at a
second host. All 56 stored fotocasa URLs end in `/<id>/d`, which is what the
second anchor matches.

**Who is selling is a four-state verdict too, and most of it was already in
the table** (`services/advertiser.py`). The owner asked to see the listings
sold by their owners rather than through an agency, from the list. Idealista
answers that for free: the alert email's link carries its own word for the kind
of advert -- `utm_campaign=express_newAd_sale_particular` against
`..._sale_professional` -- and nothing strips the query string, so 408 of the
730 rows answer with no request, no key and no cost. That is why the reading is
*derived* rather than stored, the same decision `utils/listing_source.py` and
`utils/municipality_grouping.py` record: a derived value cannot drift out of
agreement with the URL it came from, and a stored one would have to be written
by every future ingest path. A fotocasa import records what the page said
(`publisher.type`, with `agency.type` as the fallback and `clientTypeId` kept
as evidence) on the way past, since it has the page open anyway.

The remaining 322 rows are the hand-imported batches, and **169 of them cannot
be answered from this machine at all**: they are idealista.com links with no
campaign token, and idealista answers `403` with a DataDome captcha to every
request from here (re-measured 2026-08-17). So the fourth state is
`unchecked` -- nobody looked -- and it is never folded into `agency` because
agencies are the common case. The list badges `owner` only, for the reason the
source and sea-view badges give: a badge on 294 rows marks nothing. The
disclosure lives in the seller dropdown, which carries a count for all four
states, and on the property page, which names the evidence row by row.

Two things about it are load bearing. `read_verdict` and `state_expression` are
one answer in two languages -- the badge reads Python, the dropdown counts read
SQL, and `tests/test_advertiser.py` runs one matrix through both, because a
count that disagrees with the badges under it is a third wrong number rather
than a disclosure. And the campaign token is matched with `ESCAPE`, since every
token is full of `_`, which LIKE reads as "any character" (the lesson
`utils/listing_search.py` already records).

**And the owner can set it by hand, because for 268 rows nothing else ever
will.** Both production runs are in: every listing that arrived by alert email
is answered, and what is left is the hand-imported idealista links this machine
is refused by. So the badge on `/properties/<id>` is also the control -- a
dropdown recording `owner` or `agency`, next to what the app currently believes,
because the person with the page open in their own browser is the only reader
left. `set_by_hand` in `services/advertiser.py` is its one writer, and clearing
restores the reading the hand-set verdict displaced rather than deleting the
key: on a fotocasa row that reading cost a fetch and a 30 s wait, and a second
hand-set press keeps the *computed* one underneath rather than the first press.
`unknown` is deliberately not offered -- somebody who looked and cannot tell
leaves the row alone, and a hand-set silence would only overwrite a computed
answer. What a hand-set verdict does *not* need is a precedence branch in
`read_verdict`: it is stored under the same key as a computed reading, so the
branch that returns a measured state already returns it, and an earlier version
carrying one was dead code that stayed green when it was removed. Where it
really outranks something is on the write side, and `enrich` refuses a hand-set
row before it fetches anything.

`utils/backfill_advertiser.py` reads the pages of the rows nothing else can
answer. Free, and paced at 30 s rather than the import's courtesy 3: measured
2026-08-17, fotocasa began serving its "SENTIMOS LA INTERRUPCIÓN" page with a
`200` status after 5 requests spaced 3 s apart, and kept doing so for several
minutes. A run that collects three host refusals in a row stops instead of
walking the rest of the scope into the same wall; nothing is written for a
refusal, and the scope is "no established seller", so stopping early costs
nothing and the next run resumes. The per-listing path is the Enrich button,
which refuses the fetch outright when the row already answers for itself.

**"verified against Idealista" is gone from the UI**, because it was a
hardcoded string rendered for every row whatever site it was on, and the 56
fotocasa rows are not on Idealista at all. The per-row note names the row's own
source (`utils/listing_source.py` decides which, from the URL, and the badge,
the filter and the counts all read that one function so they cannot disagree);
the coverage line above the table says "on the source site", because the rows
it counts may come from several.

And `utils/repair_import_status_source.py` takes back the claim on the rows the
out-of-band importer left (STATUS-002 in #265). Its condition is **narrower
than the defect** and that narrowness is its whole safety: `manual` is also
what the owner's status button writes, so it repairs only rows that *also*
carry a `source_email_id` beginning `manual:`, the prefix only the importer
writes. The corroboration is that a real check stamps `listing_last_checked`
and not one of the 324 rows had one. Production was repaired on 2026-08-17 --
324 rows, snapshot in `data/status_source_manual_snapshot_20260817.json` -- so
the script finds nothing there now; it exists because the importer that
produced those rows is unchanged, and a repair that can recur should have
tests, a snapshot and a tested `restore` rather than being improvised again.

**The conversation that decides a purchase has a home now, and it is not
`enrichment`** (#430, five PRs on 2026-08-20). Everything the app stored about a
listing had been *measured*; what it never held was the part a person produces
-- what the agency answered, what is still owed, and what the owner concluded.
On 2026-08-20 property 774 collected all four kinds in one day (a cadastral
document by WhatsApp, a promise with a date, two verbal answers, a rejection)
and every piece went in by hand through `docker exec` as JSON, because there was
nowhere else. `property_activity` (migration 021), `property_attachment`
(migration 023) and six columns on `properties` are that place.

**The decision and the outstanding action are two independent readings, not
one.** `services/owner_review.py` owns both, each in Python and in SQL, branch
for branch, the `advertiser.py` contract -- and `tests/test_owner_review.py`
runs one matrix through both, because a count that disagrees with the badges
under it is a third wrong number. The decision is `interested` / `waiting` /
`rejected`, with **`undecided` as what NULL reads as** rather than a fourth
stored value: it is its own filter option with its own count and is never folded
into `rejected` (#98, in the column the owner filters on most). The action is
`none` / `pending` / `overdue` and is legal under *any* decision -- "interested;
call the architect on Friday" is an ordinary state, and hanging the reminder off
`waiting` loses it. Nothing writes `overdue`; it is derived, so the badge and
the column cannot drift.

Four things about that module are load bearing, and three of them were found by
mutation rather than by review:

* **One date per request, and it is Madrid's.** A due date is a calendar date
  somebody reads off a calendar in Spain, so `owner_review.today()` is
  `Europe/Madrid` -- the one place in this application that is not UTC, and the
  docstring says why. Every collection endpoint computes it **once** and threads
  it into the filter, the counts, the badge, the CSV and **both** API
  serializers. The compact `/api/properties` response is hand-built and is the
  *default* one, so a field added to `to_dict` alone is missing exactly where
  most consumers look. Testing this needs a clock that *moves*: a frozen `today`
  cannot tell "the request's date was threaded through" from "every consumer
  recomputed it", and a mutation removing `review_today=` from the serializer
  stayed green until the second call answered differently.
* **`set_review` owns its transaction.** It takes the row `FOR UPDATE` before
  reading the old state, because two presses on four gunicorn threads otherwise
  append two contradictory transitions, each atomic and both wrong -- #339's
  shape, one column over. There is deliberately **no `commit=False`**: a lock
  whose release the callee cannot see is worse than the race, which is what
  `services/enrichment_write.py` already says. SQLite has no row lock to
  observe, so the test asserts the *call and its position* and says in its
  docstring what it therefore does not prove.
* **A verdict event carries the whole review state**, not a from/to pair: a
  changed reason or a moved due date under an unchanged decision is a real
  change and a pair loses it. Pressing Save twice writes one entry -- and
  `was_edited` compares `created_at` to `updated_at` **exactly**, which is only
  honest because both writers stamp them from one value. A tolerance was tried
  and is the wrong shape: wide enough to swallow two column defaults and narrow
  enough to notice a typo corrected three seconds later is not a number that
  exists.
* **`history_out_of_sync` is a disclosure, not a guarantee** -- detail page
  only, one query, comparing the whole snapshot. Direct SQL is a supported
  workflow here (`curate_on_mini.sh`, `docker exec … psql` -- 774's own data
  arrived that way), and a column written that way leaves no entry behind. It
  can see that the newest entry no longer describes the row; it cannot see a
  transition nobody recorded, and it does not claim to. The row readers stay
  **pure** -- no session, no query -- because the list calls them once per row.

**The timeline is one feed, ordered by when things happened.** Notes, exchanges
and verdict changes share `property_activity` because they share one screen: the
material is causally ordered (asked, answered, promised, decided) and two lists
make the reader re-interleave by date what a feed already says. `happened_at` is
the owner's and editable; `created_at` is when the row was typed, and a feed
ordered by the second tells the story in the order somebody sat down to write
it. **A verdict entry is not a note**: `edit_entry` and `soft_delete_entry`
refuse any other kind and *the route refuses before reaching them* -- hiding the
control in the template is not the guard, and the test posts at one directly.
Deletion is soft throughout: everything else here can be recomputed, and a
sentence the owner typed cannot.

**The cadastral parcel is fetched from two free, keyless endpoints**
(`services/cadastre_service.py`), both verified live on 2026-08-20 at 0.15-0.28 s
against `33016A003001530001HQ`. The INSPIRE WFS `GetParcel` stored query gives
the outline -- the parameter really is spelled `STOREDQUERIE_ID`, which is
Catastro's typo and not one here -- and `Consulta_DNPRC` gives the class, the
polígono/parcela locator, the paraje and the rustic subparcels. Its parameter is
`RefCat`; the older ASMX endpoint spells it `RC`, and the wrong name returns a
200 carrying an error.

Five rules there, each measured:

* **Nothing trusts an HTTP status.** Every Catastro error arrives as `200 OK`
  with the failure in the body; both real refusals are committed under
  `tests/data/`. Only `not_found` is a measured negative -- `refused`,
  `unavailable`, `malformed` and `unsupported_metric_crs` are absences of
  measurement, never cached and never written over an answer somebody has, per
  source rather than per run (#153's shape: the metric outline is decisive, the
  map outline and the attributes advisory).
* **Three requests per press, exactly, because there are no retries.**
  `max_attempts=1`: a press is one attempt per endpoint and the retry is the
  owner pressing again, which they can see the result of. Catastro publishes no
  numeric rate limit and does publish an **~10-day IP ban** for abuse, so the
  arithmetic has to be exact rather than approximately bounded; the route's
  `@limiter.limit("5 per minute")` then caps it at fifteen a minute. Proving
  that needs a test *under* the client -- mocking `_get` proves only what the
  mock does, and a mutation restoring `max_attempts=3` stayed green until
  `requests.get` itself was watched.
* **The zone comes from the parcel's own `cp:referencePoint`**, and the EPSG
  code is stored beside the metrics. The WFS reprojects into whatever zone it is
  asked for, silently and wrongly: 25831 on an Asturian parcel returns a
  negative easting and an area 1.17% out. Bayas sits at -6.027, three kilometres
  west of the 29/30 meridian, so its zone is genuinely **25829** even though
  Asturias is spoken of as 30 -- the first fixture assumed otherwise and the
  `srsName` check caught it.
* **`srsName` checks the CRS and `areaValue` checks the parse -- not the other
  way round.** The declared area catches a dropped ring, a truncated `posList`
  or the wrong units at `max(1 m², 1%)`, and it cannot see a wrong projection at
  all: measured, the same parcel computes 6193.5 m² in 25830 and 6192.8 in
  25829 against a declared 6193. One fixture in the tree is deliberately the
  *neighbouring* zone, to keep that distinction from being re-collapsed.
* **The largest inscribed square is deliberately absent.** A grid over the
  axis-aligned case underestimates a diagonal parcel by an amount nobody has
  bounded, and a number that decides a purchase must not be an unlabelled
  approximation. 774's own 27×27 m figure stays a sentence in the rejection
  reason, which is what it was.

**Attachments put bytes on disk and metadata in the row**
(`services/attachments.py`, `data/attachments/`). `./data` is the one bind
mount, so a file written there survives the `COPY . .` rebuild; the database is
17 MB and photos are megabytes each, so `bytea` would carry every one of them
through every `pg_dump`. Four rules:

* **Content-addressed**, sha256, two-level shards -- so no path is ever built
  from anything a client sent. Measured: a PDF uploaded as
  `../../../../etc/passwd` lands at `75/c0/75c0….pdf` with the name kept as
  text and nothing written outside the root.
* **Write, fsync, `os.replace`, THEN commit the row.** The two systems share no
  transaction and the failure directions are not equal: an orphan *file* is
  inert and the sweeper reclaims it, an orphan *row* is a download that 404s and
  is indistinguishable from a sweep that has not run.
* **The type is what the bytes say.** `puremagic` reads the signature and a
  short allowlist narrows it -- the narrowing is the boundary, since "puremagic
  identified something" is not "we accept this". **SVG is not on the list at
  all**: it is XML, it can carry `<script>`, and `nosniff` does not help a
  document the browser is right to render. Measured through the real form: an
  SVG named `photo.jpg`, an HTML file named `plan.pdf`, a zip named `photo.png`
  and an empty file are all refused, **and the entry the owner typed survives
  the refusal**. The download route passes the *stored, sniffed* `mimetype`
  explicitly -- left to Werkzeug it guesses from the client's own filename --
  sets `nosniff`, and serves `as_attachment` for everything but the raster
  formats a browser really draws. A row whose bytes are gone answers **410 and
  logs**, because 404 reads as "no such attachment".
* **The composite foreign key is the invariant**: `(activity_id, property_id)`
  references `property_activity (id, property_id)`, which is what migration
  021's `UNIQUE (id, property_id)` exists for. An attachment on one property can
  therefore never name another property's exchange. There is deliberately **no**
  unique constraint on `(property_id, content_sha256)`: the same document may be
  attached to two exchanges, and a soft-deleted row would otherwise hold the key
  against re-uploading the file it refers to. Dedup is on disk; a row is a link,
  which also means a file has no single owner -- so `utils/sweep_attachments.py`
  keeps any hash **any live row** references, keeps anything younger than 48 h
  (the fsync-to-commit window every upload passes through), skips `tmp/`, and
  *moves* rather than deletes. `tools/backup_attachments.sh` exists because
  "back up this app" stopped meaning one `pg_dump`: the dump goes first and the
  bytes second, since the other order puts a row in the backup whose file is in
  no archive.

**774's own thread was converted on production on 2026-08-20**
(`utils/import_review_notes.py`, snapshot at
`data/review_import_774_20260820.json` on the mini). It **copies** rather than
moves -- `enrichment` still holds the review and the cadastral block, because
nothing here deletes a measurement -- **refuses a property that already carries
entries** under `FOR UPDATE` (that, and not a marker column, is its
idempotency), and writes the verdict **through `set_review`** so the columns and
the verdict entry land in one transaction. Its `restore` is compare-and-swap: it
touches only the rows its snapshot names and *stops* rather than deleting one
that was edited afterwards. The ficha catastral PDF itself is **not** in the
app: it arrived in the owner's WhatsApp, so the converted entry says a document
was received and names it. Writing it as a stored attachment because its name is
known would be this ticket's own defect, relocated.

Three things this feature cost that are not about the feature:

* **Two defects were found by looking at the page and could not have been found
  otherwise.** The cadastral block rendered `None × None m` (the template read
  `bbox`, the service writes `bbox_m`) and then `measured in EPSG:None` (774's
  hand-written block predates the feature and carries no `epsg`). Both passed
  every unit test, because a test asserting the section is *present*, and that
  the reference and the paraje appear in it, is satisfied by a block full of
  `None`s. Assert the numbers **by value**.
* **The property page has exactly one `<script>` element**, and
  `tests/test_issue_23_xss_and_prompt_injection.py` extracts it *by index*. A
  second `<script>` beside a form made that harness read the wrong one and
  failed eleven tests across three files; page-specific JavaScript goes at the
  end of the existing block, and guards **both** elements it looks up, because
  those harnesses run it in node against a DOM stub where one lookup answers and
  the other does not.
* **An i18n key ending in `_other` is read as a plural form.**
  `tests/test_subscription_copy_is_translated.py` then demands a `_one` beside
  it, which is why the channel labels carry a `_label` suffix.

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
  PROPERTY_TYPES.md

## Hard rules

- Never read or echo `.env` — it holds IMAP and API credentials. Required
  config is validated at startup and fails fast; do not add silent
  fallbacks around it.
- **Nothing unattended spends Google money any more** (owner decision
  2026-08-17, after a billing overrun). `AUTO_TRAVEL_ENRICHMENT` defaults to
  **false**; it was `true`, and it was the only automatic caller of a billed
  Google API in this repository — everything else is behind a button press or
  a CLI backfill. One automatic run is 6 preset Places Nearby lookups + 1 for
  the beaches + a Distance Matrix request of ~26 elements: about **$0.36 a
  listing**, twice a day, for however many alert emails arrived. Travel is
  measured on request now — the Enrich button on `/properties/<id>`, or
  `utils/recalc_property_travel.py`.

  What made it expensive rather than merely wasteful is that **the scheduler
  ran on both machines against one mailbox.** `AUTO_START_SCHEDULER` used to
  arrive as `true` in every container whatever the machine intended, so the
  laptop ingested the same alert emails as the mini and paid Google a second
  time for every listing — into a database that is thrown away and restored
  from the mini's dump. Measured
  2026-08-17 from both databases: the mini's ingest is ~7 listings a day, and
  on 2026-08-16 four new saved searches delivered **306 listings to the laptop
  between 07:00 and 10:00** — roughly $110 of Google credit in one morning
  that nobody asked for and nobody read. A dev checkout must not run the
  scheduler; the laptop's `.env` says `AUTO_START_SCHEDULER=false`, at the
  cost of `/api/healthz` answering 503 there, which is correct and is what
  `.env.example` already warned about.

  **Every place that decides that default is fail-closed** (#376). `config.py`
  had defaulted it to false outside `DEV_MODE` all along and it made no
  difference: `docker-compose.yml` set the variable in the container
  environment as `${AUTO_START_SCHEDULER:-true}`, so the code never saw an
  unset variable, and `docker-compose.dev.yml` forced a flat `true` that won
  the Compose merge over whatever the machine's own `.env` said — through
  `docker compose -f docker-compose.dev.yml up`, the workflow documented under
  *Run* above. All three now say false, so an environment that says nothing
  produces no ingester, and the machine that IS the deployment says so in its
  own `.env`. Two consequences worth knowing: a fresh clone or worktree
  copying `.env.example` is silent by default and needs the flag set on
  purpose to ingest; and `docker inspect` cannot tell you which machine is
  which, because it shows the variable in the container environment without
  distinguishing a compose default from a value in `.env` — that mistake was
  made here and nearly stopped production ingestion on deploy.
  `tests/test_scheduler_flag_fails_closed.py` pins the three places.

  **A machine that does not ingest on a tick does not ingest on a click
  either** (#388). The flag governed the *scheduler* and nothing else, while
  `POST /api/ingest/email/run` — the Manual Sync button, CSRF-exempt and
  behind no authentication — read the same mailbox on one press regardless of
  it, with a 5/minute rate limit as the only friction. The rule now has one
  home, `services/ingest_policy.py`, and two readers: the endpoint refuses
  with 409 and a reason, and the control is absent exactly where the endpoint
  would refuse. No second flag was introduced — the configuration already
  answers "is this the machine that ingests?", and a second flag is a second
  thing to forget.

  Three things about it are load bearing. The guard is the endpoint's **first
  statement**, before the request body is parsed and before any service is
  constructed: a guard that refuses after the mailbox is open has already read
  the mail. It reads `app.config`, **where the scheduler reads it**
  (`services/scheduler_service.py`, `should_start_scheduler`) and not the
  `Config` class — those are two separate readings of the environment taken at
  different moments, and a guard consulting the other one could refuse a manual
  run on a machine whose scheduler is running, which is the very disagreement
  the module exists to prevent; four pre-existing tests set
  `app.config["AUTO_START_SCHEDULER"]` and none set the class attribute. And
  the control lived in **three** templates, not one — the navbar, the empty
  state of `/properties`, and Full Sync in settings — so a test that checked
  only one surface found the other two; where the button goes the copy goes
  with it, since the empty state used to read "run a manual sync to fetch new
  listings" directly above it.

  What this does **not** close, and the module says so in its own docstring: an
  ad-hoc script run through `docker exec -i idealista-app python -` builds the
  service directly and never reaches Flask. That is not hypothetical: 326 rows
  across five hand-made subscriptions were written into the laptop's database
  that way, entirely outside the email pipeline, and had to be merged into the
  deployment's database by hand on 2026-08-17. The boundary
  here is the interface, not the process. Also note there is no CLI path for
  ingestion at all, so on a non-ingester machine ingestion is unavailable
  outright — debugging it locally means setting the flag on purpose.
  `tests/test_manual_ingest_needs_an_ingester.py` pins the refusal, that the
  mailbox is never touched, the guard's position, both surfaces, and the
  `app.config` precedence.

  **`AUTO_GEOCODING` is a separate flag and stays on**, because the paid step
  was also the *geocoding* step: `calculate_for_property` opens with
  `ensure_coordinates`, so switching travel off silently took the coordinate
  with it — and with the coordinate the sea distance, the sea-view verdict,
  the OSM amenities and the quality-of-life block, four free measurements lost
  to a flag about a paid one, leaving a row that reads "nothing nearby" when
  the truth is "nobody looked". That is #98's defect arriving through the back
  door of a cost control. Geocoding is $0.005 a listing, ~$1 a month here
  against ~$75 for the travel step. Set it false only for a machine that must
  reach no Google API at all; ingestion then makes no billed call whatsoever.
  `tests/test_paid_google_is_on_request.py` pins the defaults (read from a
  clean interpreter, not from the suite's own patched `Config`), that
  ingestion fires no Places/Distance Matrix call, that it still geocodes
  exactly once, and that turning travel back on does not geocode twice.
  `tests/conftest.py` forces `AUTO_GEOCODING` off per test — nine ingestion
  modules assert something else entirely and mock no geocoder, and left on
  they reached live Nominatim.
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
- **One public Overpass instance is a single point of failure, so there is a
  fallback list** (2026-08-19). Measured the night the presets moved onto
  Overpass: **overpass-api.de refused every connection from the Mac mini** --
  `curl` from the host itself timed out at 25 s, repeatedly, for over an hour
  -- while answering the laptop in 0.27 s. An IP-level block or throttle,
  brought on by that evening's own free backfills, which is exactly the traffic
  this project generates. `overpass.kumi.systems` answered the mini with 200 in
  3.5 s throughout and `overpass.private.coffee` in 2.1 s, and both are in
  `Config.OSM_OVERPASS_FALLBACK_URLS` now.
  It matters more than it would have a day earlier: an Overpass refusal used to
  cost an amenity count nobody scored, and since the presets left Places there
  is no paid path behind them, so an instance that will not talk to this
  machine means a listing measures nothing. Three things keep the list from
  being worse than none. A **406 does not fall through** -- that is this
  client's User-Agent being refused and every instance runs the same software,
  so moving hosts repeats it and doubles the traffic; a network error, an HTTP
  error and the `remark`-inside-a-200 do fall through, because those are one
  instance being unreachable or loaded. The failure returned is the **first**
  one, not the last: it names the instance the deployment is configured
  against, and "kumi.systems timed out" sends an operator to the wrong place.
  And the shared gate stays shared rather than becoming per host -- moving to a
  second instance because the first is loaded is not a reason to be less polite
  to the second. `tests/test_overpass_fallback_instance.py` pins all three.
  The lesson underneath is worth more than the list: **this project can get
  itself blocked by its own backfills**, and the moment a free source becomes
  load bearing that stops being an inconvenience.
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
- **An advisory step may not hold a paid one hostage, and every free lookup
  runs on a clock** (#434, measured on the mini 2026-08-20 on property 793).
  The owner pressed **Enrich**, saw nothing, pressed three more times, and
  sixteen minutes later the travel block filled in; the AI analysis never ran
  at all. One Overpass lookup spent **888 s** without completing a single
  request -- three instances x four attempts at a scalar 60 s timeout plus
  8+16+32 s of backoff each -- and the step spending it was
  `PoolService.enrich`, whose criterion ships at weight 0. Meanwhile the
  Distance Matrix request billed at 12:59 sat in an uncommitted session until
  13:12:55, behind it.

  **The retry policy splits by whether the server spoke, not by connect
  versus read.** `429` and `504` mean the instance is alive and busy, and
  #144's patient 8-16-32 budget is still right for them. Silence means the
  host is unreachable or hung, and a caller with a fallback list says
  `silence_max_attempts=1` and moves on. It is not a connect-only rule and the
  measurement is why: on the mini, overpass-api.de refused the connection
  outright but kumi.systems **connected in 0.109 s and then sent nothing for
  30 s**, so a connect-only rule would have walked straight past the instance
  that cost the most. `utils/http._is_silence` therefore reads
  `ConnectionError` and `Timeout` alike.

  **The budgets are `utils/http.lookup_budget`, and only the free transports
  read them.** `OSM_OVERPASS_WALK_BUDGET_S` (210 s) bounds one walk across
  every instance; `ENRICH_LOOKUP_BUDGET_S` (240 s) bounds every free lookup of
  one Enrich press together, because a run makes up to eleven of them. 210 is
  derived rather than chosen -- #144's patient budget on the first instance
  (~76 s with the gate) plus one complete attempt on each fallback (2 x 65 s)
  -- and the guarantee it buys is conditional and says so where the number
  lives: *a prompt refusal on the primary* leaves a complete attempt for each
  fallback, while a primary spending 30 s per `504` leaves the first fallback
  a clamped read. Making it unconditional costs seven and a half minutes for
  one lookup, and the price of the gap is a retry, never a wrong answer.
  Google's paid transports are deliberately outside all of it: abandoning a
  billed Distance Matrix request because a free source spent the clock is the
  same defect with the roles swapped, and the owner pays for a measurement
  nobody receives (#178).

  **A budget refusal is nobody's fault but the clock's**, and three places
  have to know it. It is not in `_OVERPASS_TRY_ELSEWHERE` -- the next instance
  would answer the same one gate wait later. It is not counted against
  #438's `OVERPASS_BREAKERS`, in the amenity client or the coastline one
  (`SeaViewBudgetExceeded` exists for that and for nothing else), or five
  minutes of silence gets armed against a healthy host on the strength of
  somebody else's slow run. And **a silence produced by a clamped attempt is
  the budget's too**: review reproduced three calls with a 0.1 s clamped read
  arming the breaker against a host never given the 60 s it is configured for.
  Conversely a server that already answered outranks the clock -- a `504`
  observed on an earlier attempt is returned rather than replaced by a budget
  error, because it is what the caller classifies the host by.

  **`enrich_property` has two passes and the boundary between them is a
  commit.** The decisive pass -- the coordinate, the sea distance, the travel
  times -- runs first and is committed on its own, so a container recreated
  mid-run (#283) cannot take a paid measurement with it. The advisory pass
  runs after, each step owning its write. Three of its four take the row under
  `FOR UPDATE` (`services/enrichment_write.py`); `enrich_osm_amenities` does
  not, which is #352's open gap and is not made worse here -- the old
  shared-transaction form lost a concurrently written block just as
  thoroughly. Order is a budget decision as much as a commit one: the free
  lookup the *paid* call depends on is `services/osm_places.py`, and no
  destinations means no Distance Matrix request, so the decisive steps must
  hold the clock while there is any. The pool step stays off the
  coordinate-less path, because `no_coordinates` is not one of the two
  statuses its "a refusal never overwrites an answer" guard defends against.

  **`dedupe_key` holds only while a job is *active*, and that is a trap that
  bit three times.** `property_enrich:<id>` is keyed on the property alone --
  keying on `(property, refresh_coords)` would let a `refresh=True` press race
  an ordinary one, which is #339. The AI sequel is where the trap lives: the
  enrichment job queues `property_ai_analysis` itself, so the analyses survive
  the tab, and it **returns the ids in its result** so the page attaches to
  them. A page that POSTs instead pays twice whenever the server's own job
  finished first and freed the key -- including on the path where the poller
  gave up and there are no ids to attach to, which is why the page does not
  dispatch there at all. The flag asking for the sequel is read from the JSON
  body only: these blueprints are CSRF-exempt and unauthenticated, and a
  simple cross-origin form POST cannot set `Content-Type: application/json`.

  **The client's poll budget is for silence, not duration.** No constant can
  be a run's worst case -- the executor's queue is unbounded, a `FOR UPDATE`
  can wait on a database with no statement timeout, and `requests` measures
  its read timeout *between* reads -- so `JOB_POLL_TIMEOUTS.enrichment` went
  stale the moment the fallback list was added and nobody re-derived it, which
  is #178. `services/enrich_budget.py` states what the server allows and the
  202 carries it as `poll_timeout_ms`; `pollJob` spends that on the gap
  between two answers, resetting whenever the server confirms the job is
  alive, with a backstop at four budgets because "still running" is not "will
  finish". Read the AI term from the transport the caller actually reaches:
  `classify_text_with_ai` passes no `timeout=`, so it takes
  `subscription_transport.DEFAULT_TIMEOUT_SECONDS` (300) and not
  `AI_ANALYSIS_TIMEOUT_SECONDS`.

  One thing the deadline does **not** promise, measured against a loopback
  server rather than assumed: it bounds when an attempt may *start* and what
  its socket timeouts are, not how long one attempt may run. A server dripping
  a byte often enough held a request open for 0.63 s under a 0.20 s deadline.
  That is stated rather than fixed because the outage this bounds is the
  opposite one -- instances that connect and say nothing -- and a total-time
  bound needs a streamed body with its own clock, which the coastline client
  already has. `tests/test_one_press_is_bounded.py` pins all of the above on a
  virtual clock, so what it asserts is the arithmetic and not how the machine
  felt on the day.
- **Catastro is free and keyless, and the way to lose it is an IP ban.** Its
  terms publish no requests-per-second figure at all and do publish a block of
  "generally ten days" for abuse, so the interactive path is bounded by
  arithmetic rather than by a backoff: `max_attempts=1` in
  `services/cadastre_service.py` (a press is one attempt per endpoint, the
  retry is the owner pressing again), three endpoints per uncached press, and
  `@limiter.limit("5 per minute")` on the route -- fifteen requests a minute at
  the very worst. `CATASTRO_GATE` paces and a `HostBreakers` breaker stops a
  broken loop, but neither is what makes the number true; dropping the retries
  is. Do not add a bulk path over it: this project has already blocked itself
  at one free source with its own backfills (the Overpass rule above), and
  there is no paid fallback behind Catastro at all.
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
- **The same rules say what may be recorded as a hospital, and a *centro de
  salud* may not** (owner decision 2026-08-15, narrowing the 2026-08-10 one that
  accepted "a hospital, a health centre or a public outpatient clinic"). Primary
  care has no beds and no emergency department, so recording it overstates
  medical access on a number the scorer reads: measured on the Salamir listing
  (43.568817,-6.211955), the app said "hospital 11 min" — the Centro de Salud in
  Muros de Nalón — against ~27 min to Hospital Universitario San Agustín, the
  assigned hospital. 187 of 396 travel rows held such a place. The patterns went
  on the preset, not into a second filter. Two of them are not obvious and were
  measured: Google indexes a hospital campus **room by room, every room tagged
  `hospital`**, so 13 departments of San Agustín sorted ahead of the hospital
  itself at rank 18 of 20 — `hospital de día` (a day unit) and `unidad de
  hospitalización` (one ward) carry the word and must be refused for the parent
  to win. The old rows are not fixed by the deploy —
  `utils/recalc_property_travel.py --ids …` rewrites them and **spends money**,
  so it needs the owner to ask.
- **The hospital preset is answered from the national register, not from
  Places** (owner decision 2026-08-18, after the invoice read **EUR 190** for
  1-18 August on a project ingesting ~7 listings a day). The whole of that
  bill is enrichment, and it is attributable day by day: 320 travel runs on
  the 16th, 197 on the 15th, 123 on the 10th, against invoice spikes on
  exactly those days. Two beliefs died with the screenshot and neither should
  be rebuilt on: Google's per-SKU free tiers did **not** absorb this volume,
  and the "$0.36 a listing" this file has carried was arithmetic over the
  price list rather than a reading from billing -- read the bill.
  `data/hospitals_cnh.json` is already here, already imported, already read by
  the quality-of-life card: 42 hospitals across the five watched provinces,
  every one with a coordinate, beds and teaching status. So
  `services/reference_places.py` answers the preset from it, the read is free,
  and no request leaves the machine. Measured against 12 random production
  rows: the register names the same hospital as Google for 8, and where it
  differs it is better -- two rows had a Google place literally named
  *"Hospital"* (the register names Covadonga and Jove), and at Ferrol Google
  had a private clinic where the register names the public complex 1.0 km
  away. The rules below are **kept and dormant**: they describe how to survive
  Google's `hospital` type, and one deleted `reference_source` puts that
  search back. A register that cannot answer -- file missing, or a listing
  outside its five provinces -- produces a **refusal and never a fallback to
  the paid search**, because falling through would spend exactly where the
  register is thinnest, which is the opposite of the point. This removes one
  of the seven Places calls per listing; the drive time to the hospital is
  still a Distance Matrix element, so a hospital only becomes free when the
  routing does (`tests/test_hospital_from_the_register.py`).
- **The other five presets are answered from OpenStreetMap** (step 2 of the
  same plan, 2026-08-18). Five of the seven Places calls a listing costs are
  `airport`, `train_station`, `supermarket`, `school` and `police`;
  `services/osm_places.py` resolves them from Overpass, declared on each preset
  as `osm_tag` / `osm_radius_m`. It is not a cost compromise, and the six
  production coordinates it was measured against say why: Google answered
  `police` with **"Traffic radar"** (property 101) and with a private security
  firm (property 67), and `supermarket` with "La luz de mundo" (property 123),
  where `amenity=police` gives the Comisaría and the Cuartel and
  `shop=supermarket` gives Alimerka 0.9 km away. A tag is a claim about what a
  thing *is*.
  **The #171 airport rules work on OSM names verbatim**, which is the finding
  that made this cheap: `aeroway=aerodrome` carries exactly the aeroclubs and
  light-aircraft fields Google's `airport` type does -- at Oviedo the nearest
  is *Aeródromo de La Morgal*, 9.2 km -- and the shipped
  `require_name_patterns` refuse every one while accepting *Aeropuerto de
  Asturias* and *Aeroporto da Coruña*. On all six coordinates that is the
  airport Google named. It also retires the reason `wide_search_query` exists:
  Overpass has no 50 km cap, so Cariño resolves A Coruña at 64.3 km in the same
  query, with no second paid call.
  Three things are load bearing. **One query answers every preset** -- the
  first one to run fetches all declared types and caches the candidates, so
  five presets cost one round trip at the shared 5 s gate rather than five.
  **Candidates are cached, not the nearest**, because the rules walk past what
  they refuse and caching only La Morgal would leave the preset nothing to fall
  back to. And **a refusal never falls through to the paid search**, including
  `wide_search_query`: falling through would spend exactly when the free source
  is down. The transport is `EnrichmentService._overpass_elements`, reached the
  way `services/pool_service.py` already reaches it -- do not grow a second
  Overpass client.
  Two consequences for the suite. `tests/conftest.py` stubs
  `services.osm_places.lookup_candidates` **per test** to "Overpass replied and
  there is nothing here", for the same reason it forces `AUTO_GEOCODING` off:
  six suites written against the Places path mock Google and nothing else, and
  the moment a preset started asking Overpass they reached the live internet
  and `tests/network_guard.py` failed the run. It is reset per test rather than
  once, because a suite that points it at a refusal would otherwise leave it
  there -- that mistake turned three failures into six. And the suites that pin
  the Google machinery now build their preset through a local `_google_path()`
  helper that strips `osm_tag`: what they know cost several tickets, and one
  deleted line puts that path back. `tests/test_osm_places.py` pins the module
  *and the wiring*, because a green unit suite over a dead hook is the defect
  this repository keeps rediscovering (#309).
- **A town crowds the real hospital off the page, so the preset carries
  `wide_search_query` too** (#325). Nearby Search returns **one page of 20**,
  and #323 shipped without the fallback on the strength of one *rural*
  coordinate where the hospital was still on that page. It does not
  generalise: the recalc it authorised left **48 of 187 rows** with no
  hospital, and at 43.3622522,-5.8485461 (Oviedo) all 20 results sit inside
  0.7 km and are private practices — a beauty centre, a driving-licence
  renewal office, several named individuals. HUCA and Monte Naranco are close
  and can never appear. So the refusals were right and the answer was never on
  the page, which is the airport preset's situation exactly (#171/#254), and
  it takes the same cure: Text Search accepts no `radius`, so Nearby's ~50 km
  cap does not apply, and the same preset rules filter the result. Measured
  against the deployed image: Oviedo → "Monte Naranco Hospital" 2.1 km,
  Cudillero → "Hospital Universitario San Agustin" 26.2 km. It fires only
  where Nearby already answered with nothing acceptable, so it bills nothing
  for the rows that resolve. **`wide_search_query` is not part of
  `PlaceRules`**, so adding it leaves the Places cache signature unchanged and
  the already-correct rows keep their cached lookups — which is what let the
  48 be re-run on their own.
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
- **What counts as a comparable listing is decided in one place**
  (`services/property_comparables.py`, #386). Price per m² collapses as a plot
  grows — measured 2026-08-17 over 459 production plots, Spearman −0.842, with
  band medians running €120.5/m² under 800 m² down to €4.4/m² above 6,000 —
  so a peer set that ignores size answers a different question and the answer
  reads like this one's. #378 measured that and #383 fixed the *scorer*; the AI
  prompt built its own pool and was still unbanded, which is how property 351
  (1,300 m², €46/m²) came to be judged `OVERPRICED` against a "local peer
  average" of €26/m² carried by two four-thousand-square-metre parcels, on the
  same page where its own Value component put it below the median at 52.6/100.
  Its listed comparables were worse: `ORDER BY score_total DESC`, and
  `size_score` is a component of that score, so the three shown were always the
  largest and therefore the cheapest per m². Import the ladder, do not copy it,
  and pick comparables by size proximity. `band=False` exists for the size
  component alone, where a window around the listing's own area would be
  circular. A prompt whose pool spans mixed sizes **says so** — a bare average
  is read as "what the neighbours ask", which is #98's defect with a number in
  place of a blank. Two consumers already drifted; the third will too.
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
  `data/.inflight/<module>.<run_id>.json` (a per-run id, not the PID — a
  containerised run is always PID 1, so the PID cannot tell two runs apart,
  #359; the body also records `host`, the container id, because a PID is only
  a name inside one namespace and a marker from another container reads as
  "cannot tell", never as dead or alive) while it runs and reports — on the
  next start — any marker a killed predecessor left behind. `resumable=True` is a
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
  live job and of a corpse alike. `tools/backfill_status.sh` is the question
  "is anything running", there and before a hand build ("Building by hand in
  the shared checkout" above); the marker only answers "and would killing it
  cost anything". It is that script and **not** a bare
  `docker top idealista-app`, which is correct only about one container at one
  instant: it cannot see the respawn a supervisor is a tick away from making,
  and it cannot see a job moved into a `docker compose run` sibling — which is
  where long work goes precisely *because* deploys kill it in the app
  container, so the operator who reacted correctly is the one the bare command
  reports as idle (#338). Deferring is opt-in and bounded
  (`AUTOPILOT_DEFER_ON_INFLIGHT`, `AUTOPILOT_DEFER_BUDGET`) — a deploy that
  never lands is a failure too. See `tools/autopilot/README.md`.
- **Two processes writing `enrichment` lose a measurement, and the #98 guard
  cannot see it happen** (#339, incident 2026-08-16). Two runs of
  `utils.backfill_pool` overlapped on the mini; properties 399 and 400 ended
  the afternoon holding `unavailable` with zero candidates, over measurements
  another run had committed seconds earlier, and three rows were billed to
  Google twice. `enrichment` is one JSON column, so every write is a
  read-modify-write over all of it, and `PoolService.enrich` consults the
  previous status from **the copy its own session loaded** — after `_compute`
  has spent seconds on external calls, which under Overpass 504s and four
  retries ran to about 90 s per row. The rule "a refusal never overwrites
  measured candidates" is therefore a guarantee about one transaction's view,
  not about the row. Do not fix it with ordering or a timestamp: on 399 the
  measurement was written 63 s *after* the refusal and still lost, so any
  comparison of write times would have left that row broken. The primitive
  is already here — `apply_to_property` in `services/sea_view_service.py`
  refreshes with `with_for_update=True` (#196) against this exact hazard, and
  `services/pool_service.py` contains no `with_for_update` at all. It takes
  that lock only when `commit=True`, because with `commit=False` the caller
  owns a transaction whose end this function cannot see, and taking one on
  their behalf for an interval it cannot close is worse than the race — that
  mode makes no concurrency promise at all. **The lock lives inside the one
  writer, on its `commit=True` path, and never at a call site**
  (`services/sea_view_service.py:1294` says so: a lock at a call site protects
  that call site, and `utils/backfill_sea_view.py` "and every future caller
  would otherwise reopen the same hole" — the Enrich button, an endpoint, next
  month's script). So what the tools change is the opposite of a lock of their
  own: `utils/backfill_pool.py` and `utils/recalc_property_travel.py` call
  their services with `commit=False` and commit themselves, and they have to
  give that ownership up and pass `commit=True`. And the boundary is **any** writer of
  `enrichment`, not the paid ones: a one-row free script run by hand through
  `docker exec` clobbers a backfill exactly as thoroughly, cost only setting
  the size of the loss.
- **A liveness check is not a claim about the next minute** (#338). A marker is
  not a lock, and `docker top` is not a reservation. The second run above was
  started after its session ran `docker top idealista-app` and correctly saw
  no `utils` process — because the deploy had killed the first run 57 seconds
  earlier (09:01:02 in the deploy log) and `tools/backfill_supervisor.sh`
  refilled the container at 09:01:59, on its next tick. A kill makes the process list read empty precisely
  when a respawn is imminent, and every deploy manufactures one such window.
  What was missing was anything that could express "nothing is running here,
  **and that is temporary**". `tools/backfill_status.sh` expresses exactly
  that, and it is what to run before starting one: it reads what is running
  now (`docker top`), what is *expected* — the supervisor's lock, taken under
  `noclobber` at startup and released only by its `EXIT` trap, so it spans the
  whole kill→respawn gap and is the one thing in the system that knows the
  future — and what started and never cleaned up (`data/.inflight/`, a report,
  never a lock). It answers in three states, and `unknown` blocks exactly like
  `busy`, because every defect in this family began with a failed probe
  reading as a negative answer. Its judgement about a stale lock is *copied*
  from `acquire_lock()` rather than re-derived — that refuses on any existing
  lock file, live pid or dead (#319) — because a tool calling a state "safe"
  that the supervisor calls "stop" is how two of them come to disagree about
  one file.
  It answers; it does not enforce — the daemon cannot stop you, and that is
  exactly why **`busy` and `unknown` are a stop, not an input to a judgement**
  (owner decision 2026-08-17). Wait, and say you are waiting. Do not weigh
  whether the two jobs touch different `enrichment` keys, whether your own run
  is cheap, or whether the other one looks stuck on a timeout and "is not doing
  anything anyway" — you cannot know when it resumes, and the session running
  it is very likely not in your conversation.

  That sentence exists because the door was walked through the day it was
  written. A session shipped this feature's two backfills over a running
  `backfill_quality_of_life`, twice, each time having first checked the thing
  that made it safe: both writers reach `enrichment` through
  `services/enrichment_write.locked_write` under `FOR UPDATE`, they write
  different keys, the lock spans milliseconds rather than the network calls,
  and the second run made one request and one write. All of that was true, no
  measurement was lost, and none of it is the point. The rule protects the case
  nobody thought to check, and "I read the other job's code and it is fine" is
  the shape of reasoning #339 and #338 were written about. Only an explicit
  owner command to start anyway overrides it.

  Sessions sharing this machine still announce a `utils.backfill_*` /
  `utils.recalc_*` — or any hand-run `docker exec` that writes `enrichment` —
  before starting it, naming the module, the rows and the cost. That is a
  protocol, not a guarantee: it holds while every writer is listening, and the
  next one may not be in the conversation at all.
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
- **A deployer sweeps only what it can prove is dead, and only in its own
  lane.** `tools/autopilot/lib/docker_cleanup.sh` is the second shared contract,
  read by both deployers after a build is *serving* — never on the rollback
  path, where the old image is the thing being restored. Measured 2026-08-17,
  the two machines litter differently and the obvious one-liner fixes neither
  well: the mini held 27 images at 1% reclaimable and no build cache at all
  (with the containerd snapshotter an untagged image is collected as soon as
  nothing holds it, which is why ~21 deploys a day leave no pile), while its
  real leak was three exited `docker compose run` corpses pinning the images
  behind them; the laptop held 20.24 GB of build cache, which
  `docker image prune` does not touch at all. Three things the sweep must never
  become, each a real deletion: **`-a`** removes every image no *container*
  uses, and `${IMAGE}:autopilot-rollback` is exactly that — it is the rollback;
  **`docker system prune`** collects vsdb, virto-property and inbox-zero off the
  shared daemon, which is why the scope is the compose project label, read off
  the running container rather than guessed from a directory name; and
  **`--remove-orphans`** kills a *running* one-off, which is where long
  backfills deliberately live (#338). Only `exited` containers carrying
  `com.docker.compose.oneoff` go, and not for a day — a job the deploy just
  killed leaves its container log as the only record of how far it got, and a
  finish time that will not parse keeps the container rather than deleting it.
  Nothing here may fail a deploy that is already serving.
  `tests/test_docker_cleanup_shared.py` pins the refusals.
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

  **CI now asks the same question and does not rely on you asking it**
  (MUT-001, `tools/ci/mutation_check.py`). On every pull request it removes the
  diff's production hunks in a worktree of its own and re-runs only the tests
  the diff touched: `CAUGHT` when at least one of them goes red, `ESCAPED` and
  red when none do, `NOOP` for a docs- or tests-only diff, `WARN` when
  production changed and no test did, and `TOOLING-ERROR` — exit 2, neither
  pass nor fail — when it could not run. Four to nine seconds on the real PRs
  of 2026-08-19, against the suite's six minutes. An `ESCAPED` that is right —
  a refactor, a revert, a test written for behaviour that already existed —
  is answered with a `Mutation-Waiver: <reason>` trailer on any commit in the
  branch. That friction is the point, the way `tests/skip_guard.py`'s `ALLOWED`
  is — but it is weaker than `ALLOWED`, and the difference is worth knowing: a
  skip exemption is a line in a reviewed file that stays there, while a trailer
  is free text in a commit message, visible in the PR and nowhere afterwards.
  Nothing checks that the reason is a good one.

  **It answers "can these tests fail", never "is what they assert correct."**
  Reverting cannot redden a test for a bug the revert removes, and that is two
  cases wearing one face: the diff *introduced* the defect, so the code without
  it never had one — or the defect already lived in shared code and the diff
  only *brought a new consumer to it*, in which case the revert removes the
  consumer and leaves the bug. The second is the one worth carrying: **a new
  call to an existing shared function is a change to that function**, and has
  to be read as one. Measured on #427 the day the check shipped — an
  independent review of that diff found three real wrong answers (a guard that
  was a no-op whenever the geocoder named no province, a fallback the check was
  blind to, and an alias table that had always been wrong in one direction and
  had simply never been asked), and neither that PR's own six mutations nor
  this check could have seen any of them. Its author: *"I mutated what I wrote,
  not what I missed."* Review is what catches those.

  **The two find different classes, and neither is the safe one.** Mutation
  finds defects of the *tests* — measured 2026-08-19, it caught a test that
  reached its feature by a road the mutated flag never touched, a test that
  passed when the tool under test was deleted outright, and a missing case;
  review finds defects of the *code*, and found eleven the same day that no
  mutation on either side could have seen. Neither substitutes.

  Two things about the review half are worth the words, because both cost
  something when skipped. It is not "read the diff" — it is **asking the code
  the specific question you are afraid of the answer to** ("can this emit a
  false `contradicted`?", "how can this checker itself lie?"). A lens without
  a question returns nothing: of the four pointed at #427, one came back
  empty. And review **invents findings at about the rate it finds them** —
  #426's raised 19 and 8 survived reproduction, #427's raised 7 and 5 did — so
  a finding is worth acting on after an attempt to refute it, not before. Both
  of those numbers are a third to a half wrong, and acting on the wrong half
  means fixing something that is not there.

  **The refuter must not be the finding's author**, and that is the part doing
  the work — not the count. An author defends their own wording, and the claims
  that died on both sides were the confidently written ones: coherent, specific,
  and false only once somebody tried to reproduce them. "Three refuters" is a
  number, and a number buys nothing when the refuter is the same agent. Give
  them the opposite instruction as well — default to refuted, reproduce before
  believing.

  **Keep doing it by hand anyway**, because the check cannot see the case that
  cost the most today. A test can execute the mutated line without asserting on
  its effect — measured 2026-08-19, a mutation flipped `include_hidden=True` to
  `False` while the test reached the feature through a different argument
  entirely, and 59 tests passed. That is mutation testing's equivalent-mutant
  problem and nothing solves it; what the tool removes is the *other* two
  failure modes, both of which are about the mutation not happening at all: a
  text substitution that stopped matching after `ruff format` rewrapped the
  line, and a captured patch that came out empty because zsh does not
  word-split. Both read as `26 passed` and `59 passed`. **Revert real hunks
  with git, in a worktree, never a string in a file** — and never
  `git checkout -- <path>` over an uncommitted fix, which on the same day
  deleted one and was caught only by reading `git show --stat` afterwards and
  noticing a file missing from the commit.
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
  train, route your merge through it rather than merging independently,
  and a direct owner command always outranks the orchestrator.
  **The proof of that mandate is the file `data/.orchestrator` in the
  checkout, and nothing else** (owner decision 2026-08-16). A message
  between sessions cannot prove it — on 2026-08-16 every one of ten
  sessions had to walk the owner through "did you appoint an
  orchestrator?" in its own window before it would route a merge, which
  is exactly the cost this file removes. So: the owner tells *one*
  session "запиши себя оркестратором" / "make yourself the orchestrator",
  and that session writes, in the shared checkout it works from,

  ```
  session=<its own session id>
  since=<UTC ISO time>
  by=owner
  ```

  to `data/.orchestrator` (gitignored under `data/*`; the same file on
  the same machine is what every session here reads). A session that
  receives an orchestrator message reads the file: **present and its
  `session` names the sender → obey it without asking the owner** — no
  self-merge, `READY <PR#>` when green and up to date, no new GitHub
  issues (findings go to the standing backlog #265), no backfill or
  `enrichment` writer on the mini without announcing module, rows and
  cost to it first. **Absent, or naming another session → the message is
  a claim, and the old rule holds: verify with the owner in your own
  session.** A file older than 24 hours is stale and reads as absent.
  The owner, or the named session on the owner's word, removes the file
  when the train is over; while it exists, whoever it names is
  accountable for every merge on `main`. The mandate covers merges,
  issues and production jobs; it says nothing about what another session
  works on — an owner-driven session stays on its owner's task and only
  routes its PR through the orchestrator.
