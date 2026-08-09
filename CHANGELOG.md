# Changelog

All notable changes to the Idealista Tracker AI project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### 🐢 Changed: Overpass is paced at 5 s, because 2 s was a guess (2026-08-09, #152 follow-up)
- **What**: `OVERPASS_MIN_INTERVAL_S` goes from 2 s to 5 s, and `429 Too Many
  Requests` joins the three refusal shapes recorded in `docs/STATE.md`.
- **Why**: a dry-run amenity backfill over 20 properties — the first time this
  path was paced and counted against the live instance — spent **39 requests on
  20 answers**: 16 served, 8 refused with `504`, and **15 with `429`**. More
  than half the traffic was the server asking for less of it, and the run took
  ~12 minutes, which extrapolates to about two hours over the whole table.
- **Nothing was recorded wrongly**: `_DEFAULT_RETRY_STATUSES` in `utils/http.py`
  has always carried 429, so every one of those was waited out and answered on
  retry, and the run finished `measured 20, refused 0`. That is also why it
  went unnoticed until someone counted — a backoff hides a bad rate rather
  than reporting it.
- **A backoff is not a rate.** The retry loop makes a too-fast caller correct;
  it does not make it welcome. `tests/test_issue_152_property_osm_amenities.py`
  now pins both halves: a `429` followed by a `200` stores the counts (it fails
  if a caller narrows `retryable_statuses` and files "too many requests" as a
  measured absence), and the interval does not quietly go back down.
- **An interactive Enrich pays nothing** for the wider interval: the gate is
  idle between presses, so a single lookup never waits. Only a run that
  actually issues calls back to back is slowed, which is the run that was
  drawing the 429s.

### 🗺️ Fixed: nearby amenities are measured for the listings that exist (2026-08-09, #152)
- **What**: `PropertyEnrichmentService.enrich_property` — the Enrich button on
  `/properties/<id>` — now runs the OpenStreetMap amenity lookup and writes it
  to `Property.enrichment["infrastructure_extended"]`, in the same
  `osm_amenities` / `osm_amenities_status` shape the page already renders. It
  reuses the existing Overpass client rather than adding a second one: the
  transport, the cache and the three refusal shapes now live in
  `EnrichmentService._fetch_osm_amenities`, with `_enrich_with_osm_data`
  (legacy `Land`) and `enrich_osm_amenities` (universal `Property`) as the two
  thin writers over it. `utils/backfill_osm_amenities.py` backfills stored
  rows; it calls the amenity lookup directly, never `enrich_property`.
- **Why**: the lookup was reachable only from the legacy `Land` endpoints, so
  213 of 352 listings could not show amenities however often Enrich was
  pressed — the 139 that did were legacy rows mirrored in, measured before
  `lands` froze. An absent Extended Infrastructure card reads as "nothing
  nearby" rather than "never asked", which is #98 and #144 one level up: not a
  refusal recorded as a negative, but a question never asked, presented as an
  answer. Overpass is free and keyless, so no billing argument applied.
- **Notes**: a refusal is recorded as `state: "unavailable"` with its reason
  and never as empty counts, and it does not fail the enrichment run — no
  score reads these counts, and `504` is routine on an endpoint that grants
  two slots per IP. That pacing is now one process-wide gate,
  `utils/http.py` `OVERPASS_GATE`, shared with the coastline query in
  `services/sea_view_service.py`; pacing them separately paced neither. A
  property with no usable coordinates is recorded as not asked rather than
  geocoded, because geocoding is a paid Google call — including the listing
  geocoding *failed* to place, where `enrich_property` gives up before
  anything else can run. The gate reserves each caller's slot under its lock
  and sleeps outside it, so a finishing request never blocks for a whole
  interval behind somebody else's wait; it paces when a call may start, which
  on an endpoint granting two concurrent slots is the design rather than a
  hole in it. On a row mirrored from
  `lands` the first write seeds the section from the legacy one, since
  `Property.infrastructure_extended` stops falling back the moment a top-level
  section exists — without that, the first measurement would have hidden every
  Google-derived key the page was showing. Re-running **Google** enrichment
  stays out of scope, and running the backfill is a separate owner decision.

### 🔐 Fixed: a log handler can no longer write an API key in plain text (2026-08-09)
- **What**: `utils/log_redaction.py` adds a logging filter that strips
  credentials out of a record before any handler formats it — `key=`,
  `api_key=`, `token=`, `password=` and friends in a query string, bare
  `AIza…` Google keys wherever they appear, and `Bearer` tokens. `app.py`
  installs it on the root logger's *handlers*, which is where records
  propagating up from `urllib3` are actually emitted; a filter on the root
  logger itself would never see them.
- **Why**: the Google clients take their API key as a **query parameter**, so
  it is part of every request URL — and `urllib3` logs the full URL at DEBUG.
  Running the ingest by hand on 2026-08-09 printed the owner's live key in
  full. Nothing in this codebase printed it: the HTTP library printed the URL,
  which amounts to the same thing.
- **Why not just keep DEBUG off**: that holds only until the next time someone
  turns it on. `DEV_MODE=true`, a library that configures logging itself, or a
  one-off `python -c` that never calls `basicConfig` all reach the same place.
  The level is a preference; the filter is a property.
- **Proven, not assumed**: `tests/test_log_redaction.py` opens with a baseline
  test showing the key *is* written without the filter, then pins that the same
  call with it installed is clean. The format it exercises is copied verbatim
  from `urllib3.connectionpool` — the same `'%s://%s:%s "%s %s %s" %s %s'` and
  the same eight arguments — because the secret arrives as an argument, not in
  the format string, and a filter reading only `record.msg` would pass it
  straight through.
- **A filter alone was not enough, and review caught it.** The first version
  redacted only in a `logging.Filter`. That covers the message and nothing
  else: `logging` renders `exc_info` inside `Formatter.format`, and a handler
  filters *before* it formats, so every raised exception carrying a credential
  went out untouched — confirmed against a live logger. `RedactingFormatter`
  wraps whatever formatter a handler already has and redacts the rendered
  result, including the cached `exc_text` so a second handler cannot re-emit
  the original from the cache.
- **The test that missed it is gone.** It pre-populated `record.exc_text` and
  called the filter directly, which passes against code that leaks. The
  replacement raises a real exception and reads what the handler actually
  wrote.
- **Values are matched as "up to the delimiter", not as an allow-list of
  characters** — the second thing review caught. `Bearer` under
  `[A-Za-z0-9._-]+` stopped at the first character the list forgot:
  `Bearer abcdefgh+TOPSECRET` became `Bearer REDACTED+TOPSECRET`, a leak that
  looks handled. base64 carries `+`, `/` and `=`; JWTs carry `.`.
- **`key` is a credential in a query string and a preset name in a dict.**
  `?key=` is Google's API-key parameter, but this codebase stores
  `{"key": "airport"}` in travel presets and `{"key": "police"}` in enrichment.
  The structured-output pattern therefore covers `api_key`, `token`,
  `password`, `secret` and friends but deliberately not a bare `key`, so
  redaction does not blind the diagnostics it exists to keep readable.
- **A record that cannot be rendered leaks more, not less** — the third review
  finding. `logging` does not drop a record whose `%` substitution fails; it
  calls `Handler.handleError`, which prints the format string *and the raw
  arguments* to stderr. `logger.error("token=%s %s", "TOPSECRET")` wrote
  `Arguments: ('TOPSECRET',)` in the clear.
- **There the arguments are withheld, not pattern-matched.** Patterns work on a
  *marked* secret — `?key=`, `Bearer …`, an `AIza…` key. A bare argument has no
  marker: in that example the only thing identifying `TOPSECRET` as a
  credential is the format string it never got substituted into. Guessing
  cannot be made reliable, so each argument is replaced by its type name —
  `('<str>', '<str>')` still answers the question a broken-formatting
  diagnostic exists to answer, and a type name is not a credential.
- **Not covered**: handlers added *after* `create_app()` runs. Redaction is a
  net under accidents, not a licence to log secrets deliberately.

### 🛟 Fixed: the two saved-search identity races are observable and locked (2026-08-09, #116)
- **What**: an alert email that carries no saved-search URL now says so.
  `SearchProfileService.resolve_profile()` logs one warning per such email,
  naming the label it matched on and describing what actually happened — a
  keyless profile (the half a *concurrent* URL-carrying alert can split; a
  later sequential one adopts it instead), a profile that already carries a
  search key (matched on a label alone, nothing verified), or no single profile
  at all. And `merge_duplicate_profiles()` takes **one** `FOR UPDATE` over
  every row it will look at, in ascending id order, before any group's decision
  or mutation — through the same `lock_profiles_statement()` the repair service
  uses, now defined once in `services/search_profile_service.py` and carrying
  the `ORDER BY id` that actually fixes acquisition order — then rebuilds the
  groups from the names those locked rows carry *now*, discarding the snapshot's
  grouping, and keeps the rows to the end of the transaction. With
  `commit=True` it always ends that transaction before returning, from the
  first query onwards: the conflicts-only and no-op runs that write nothing,
  and any failure in between, all release the locks. `commit=False` still
  leaves the transaction to the caller.
- **Why**: both races are from #110. The first splits one subscription across
  two profiles when a URL-carrying and a URL-less alert for the same new label
  arrive together; it cannot be constrained away, because `UNIQUE (name) WHERE
  source_search_key IS NULL` deliberately ignores keyed rows so that two
  genuinely different subscriptions may share a label. Nothing marked the
  URL-less path, so the precondition was invisible until the damage showed up
  in the listing counts. The second decided from an unlocked snapshot: a
  `_claim_keyless_profile()` landing in between turned a safe-looking group
  into one holding two keys, and the merge deleted a row that had just
  acquired an identity — unrecoverably, since nothing records which saved
  search a stored listing came from.
- **Notes**: neither unique index was touched, and no constraint was widened.
  The lock is Postgres semantics; SQLite ignores `FOR UPDATE`, so the suite
  pins that the lock is *requested* — one statement, full sorted id set, before
  any mutation — and that the decision is taken from a re-read, but not that a
  second connection blocks. Two things that look like ordering and are not:
  sorting the `IN` list orders nothing without `ORDER BY` in the SQL, and
  per-statement `ORDER BY` orders nothing across a run that locks group by
  group — groups `[1, 100]` then `[2]` climb back down and can deadlock against
  a repair holding `[2, 100]`. One seam stays open by construction: the lock set
  can only be derived from the snapshot, since a row must be known before it can
  be locked, so a profile *inserted* under one of these labels mid-run is
  neither locked nor seen. The regroup closes the rename seam, not that one.

### 🌊 Added: sea view is computed again, as four states instead of a flag (2026-08-09)
- **What**: `services/sea_view_service.py` estimates a sea view from two
  independent signals and stores the verdict at
  `enrichment.environment.sea_view`. Geometry: nearest OpenStreetMap coastline
  (`natural=coastline`) plus a terrain profile sampled from Copernicus EU-DEM
  25 m, with earth curvature and standard refraction, deciding whether the line
  of sight to the water clears the ground between. Text: the listing wording,
  with the subscription bridge asked to separate "vistas al mar" from "cerca
  del mar" — its only job is to demote the false positives that made the old
  flag untrustworthy. The states are `yes` (both agree), `likely` (one source
  unopposed), `no` (computed and negative) and `unknown` (not computable). The
  `/properties` control is a working select again — *Any* / *Confirmed* /
  *Confirmed or likely* — carried through every in-page link, the map and the
  CSV export, which gains `Sea View` and `Sea View Source` columns. The
  property page shows the verdict with its provenance, and its Environment card
  now renders even when there is no data yet, which is what makes the hand-set
  override reachable; a hand-set verdict outranks both models and survives
  recalculation. `python -m utils.backfill_sea_view` fills it.
- **Why**: the control was rendered dead because `Property` had no field behind
  it. But the underlying question needs no paid API — Google billing (#98) is
  irrelevant to it — and the old `Land` boolean it replaced was `true` on
  exactly one of 168 rows, built by matching keywords against the
  ~300-character fragment an alert email carries. Four states exist so that
  "we could not compute this" stops being written as "no", which is the same
  confusion #98 documented for travel times. Geometry alone never reaches
  `yes`: EU-DEM is bare earth, so it cannot see trees or buildings. The
  mirrored `Land` boolean reads as `likely`, never `yes`, for the same reason.
- **Notes**: overpass-api.de refuses the default `python-requests` User-Agent
  with a 406 (and any UA carrying a parenthetical comment), and grants two
  query slots per IP. Coastline is therefore fetched once per 0.1° grid cell —
  67 queries for 351 rows instead of one per property — behind a bare product
  token. The "to beach" sort stays unavailable: that one really does need
  Google Distance Matrix.
### 🔎 Added: the unassigned listings are counted where the page claims a total (2026-08-09, #111)
- **What**: `/properties` discloses, next to its total, how many listings with
  `search_profile_id IS NULL` the current view leaves out — with a link that
  shows exactly those — or says the total already includes them. `/profiles`
  grew a "No subscription" row, so its per-profile counts stop summing to less
  than the table.
- **Why**: #102 stopped guessing a subscription for an email that names several
  saved searches or whose lookup failed, on the grounds that "visible and
  unassigned beats silently wrong". #104/#112 made those rows reachable through
  the dropdown; the page's total still left them out without saying so —
  `profile_id=all` means every *active profile*, never "no filter".
- **A third way a listing loses its subscription**: deleting a profile.
  `properties.search_profile_id` is `ON DELETE SET NULL` (models.py), so the
  listings are detached rather than removed.
- **The count is computed under the filters currently applied**, off the same
  query as the page's own result, with the subscription filter applied last. A
  global count would advertise a number the "show them" link does not land on
  the moment any other filter is set. It is now the page's *only* unassigned
  count: the "No subscription" entry in the subscription dropdown (#104/#112)
  was counting the whole table, so one screen carried two different numbers
  under one label. That entry reads the scoped count too, which also means it
  drops out of the dropdown when the current filters leave nothing for it to
  show — the rule already applied to a subscription that would only ever
  return an empty page.
- **No control to file one by hand**, although issue #111 asked for that too.
  The version of this change written before #130 landed reinstated `POST
  /properties/<id>/profile` and wrote
  `enrichment.profile_assignment.manual_override` — precisely the stale pin
  migration `014` had just cleared, and one nothing can clear again now that
  the editor is gone, while `search_profile_repair_service` refuses to move a
  row carrying it. Ingestion stays the only writer of a listing's
  subscription: the route stays deleted and the guard tests in
  `tests/test_subscription_assignment_is_automatic.py` stay untouched. A row
  lands in this bucket because its email named no single saved search, so the
  fix belongs in ingestion rather than in a dropdown.
- **Not in scope**: `/api/properties` still defaults to the default profile and
  offers no unassigned selection.

### 🧹 Fixed: the type filters now describe the subscription on screen (2026-08-09)
- **What**: `/properties` builds its `Type`, `Subtype` and `Municipality`
  choices from the subscriptions currently selected instead of from the whole
  `properties` table, and the subtypes narrow again to the chosen type. The
  hard-coded `Unclassified` option is now offered only when the selection
  really holds a row with no category/subtype (or the filter is already on
  it) — the same rule the "No subscription" checkbox follows. An applied
  filter stays listed even when the new selection has none of it, so the
  control can never read "All types" over a filtered page.
- **Why**: a saved search for land offered `apartment`, `house` and
  `developed` — values owned by other, mostly retired subscriptions, which
  can only ever return an empty page there — plus `Unclassified`, which
  matched **zero** of the 351 stored listings because every one of them is
  classified.

### 🧹 Fixed: Overpass was refusing every OSM enrichment call (2026-08-09)
- **What**: `EnrichmentService._enrich_with_osm_data` sends
  `User-Agent: IdealistaRank/1.0` (a bare product token — `utils/http.py`
  `HTTP_USER_AGENT`), and a refused call is now recorded as
  `infrastructure_extended["osm_amenities_status"] = {"state": "unavailable",
  "reason": …, "http_status": …}` instead of leaving no amenity list behind.
  The refusal is logged at ERROR, never cached, and returned to `enrich_land`,
  which now names the source that refused rather than always saying "Google".
  An answered-but-empty run stays a success (`state: "ok"` with an empty
  `osm_amenities`), so "nothing within 2km" and "we never got to look" are
  finally distinguishable. Retries widened to 8/16/32s because
  overpass-api.de answers 504 while both of its two per-IP query slots are
  busy, and the old half-second backoff gave up long before a slot freed.
- **Why**: the call sent no `User-Agent`, so `requests` sent its default
  `python-requests/x.y.z` — measured against the live instance, that is
  answered with `406 Not Acceptable`, as is any UA carrying a parenthetical
  comment. Every OSM enrichment had therefore been failing, and since the only
  check was `if response.status_code == 200`, the 406 never even reached the
  `except` block: no log line, no stored marker, just a property that looked
  like it had no amenities nearby. Same class of defect as #98 — a refused API
  written out as a computed negative. The same live coordinates now return
  `{'fuel': 12, 'restaurant': 232, 'cafe': 221, 'school': 67, 'hospital': 7}`.
  Overpass is a fallback source that the score does not read, so its refusal is
  reported and stamped but does not by itself fail an enrichment run.

### 🧹 Fixed: an address no longer decides a listing's type (2026-08-09)
- **What**: classification reads the head of a title (the part before ` in ` /
  ` en `) before falling back to the full text, so the type comes from what
  the listing *is*, not from where it is.
- **Why**: the `casa` rule outranks the land rule and matched the *address*:
  five plots — "Land plot in Caserio **Casa** de Anes", "Land in Camín de
  **Casa** Pelayo" and three more — were stored as `housing/house`, one of
  them inside the owner's land subscription. Re-checked against all 351
  production titles: the fix changes those five rows and nothing else.

### 🧭 Changed: one listing surface, with the saved searches on it (2026-08-09, #125)
- **What**: `/lands` redirects to `/properties`; `templates/lands.html` and its
  archive banner are gone. `/properties` is rebuilt in the layout the owner
  uses (row count + last sync, Filters & Search, the scoring-mode toggle, and
  the Score / ★ / Title / Price / Area / Coords / Travel / Inv. Metr. / Type /
  Added / Actions table). A visible switcher lists every live subscription by
  name with its listing count, retired ones sit under *Archive*, and a bare
  `/properties` now shows **all** live subscriptions instead of one
  auto-selected profile. Rows carry a subscription badge when more than one is
  on screen; travel columns survive the union (presets shared, custom targets
  withheld).
- **Why**: the owner has two saved searches on idealista.com and was looking at
  two differently-styled pages, one of which opened on a single subscription
  chosen for them and hid the other behind a closed dropdown.

### 🧹 Removed: three enrichment buttons and two unasked-for editors (2026-08-09, #131)
- **What**: the property page has one `Enrich` in its header instead of
  "Enrich with Google APIs" + "Recalculate travel" + "Recalculate scoring", and
  the "Profile assignment" and "Classification" editors are gone. All four
  now-unreachable POST endpoints were removed with them.
- **Why**: `enrich_property()` already geocodes, recalculates travel and
  rescores in one pass, so the two extra buttons re-billed Google for the same
  answer. The editors arrived with the original universal-tracker import and
  duplicate what ingestion and the profile rules decide. A state-changing route
  with no UI is a hole on an app with no authentication, not a leftover.

### 🛟 Fixed: AI sections that had data reported themselves empty (2026-08-09, #132)
- **What**: Investment Potential falls back to `forecast` (and lists
  `key_drivers`); `renderSimilarProperties()` accepts the
  `{comparison_summary, recommended_alternatives}` object as well as an array.
- **Why**: both providers write those shapes, and the renderer looked for a
  `summary` key and an array — so a filled-in analysis showed "No investment
  potential analysis available" and "No similar properties found". ChatGPT's
  N/A rental metrics are *not* part of this: those are real nulls, the model
  declined to put numbers on a single-comparable market.
### 🧹 Removed: the geo heuristic that overwrote a listing's subscription (2026-08-09)
- **What**: `services/profile_assignment_service.py`, the `AUTO_PROFILE_ASSIGNMENT`
  flag and the enrichment call behind them are gone, and migration `014` clears
  the `enrichment.profile_assignment` metadata they left on existing rows.
  `Property.search_profile_id` now has exactly one writer: ingestion, which reads
  the saved-search URL out of the alert email
  (`services/property_imap_service.py`). This finishes the removal the same-day
  UI change (#131) started by dropping the "Profile assignment" editor.
- **Why**: the email knows which saved search a listing arrived through.
  `assign_nearest_profile` then silently refiled it under whichever *active*
  profile had the geographically nearest custom target — so a listing from
  "houses norte" could end up filed under a coastal profile, and nothing in the
  UI said so. The manual editor's real function was to set `manual_override`,
  which made the heuristic skip the row: the only defence against the automation
  was to override it by hand, which is backwards. Owner decision, 2026-08-09.
- **Why the metadata had to go too**: `search_profile_repair_service` refuses to
  move a row carrying `manual_override`, and with the editor deleted nothing can
  clear that flag any more. A stale pin would freeze a listing in a fragmented
  profile permanently. The clause itself stays, as a guard should any future
  writer set the key again.

### 🛟 Fixed: listing pagination has a deterministic order (2026-08-09, #113)
- **What**: `/properties`, `/lands`, and their CSV exports now use the model ID
  as the final sort key after every nullable user-selected ordering.
- **Why**: rows tied on price, area, score, date, title, or a NULL run could
  move between query executions, causing pagination to repeat or omit rows and
  making the page disagree with its CSV export.

### 🔑 Changed: a saved search is identified by its URL, not its label (2026-08-08, #102)
- **What**: alert emails link to the saved-search page, and that link encodes
  the subscription's filters. `services/search_subscription_identity.py`
  canonicalizes it and fingerprints it as `idealista:v1:<sha256>`, stored in
  the new `search_profiles.source_search_key` (unique) with the raw link kept
  in `source_search_url` for diagnostics. `resolve_profile()` now resolves by
  search key first, then adopts a same-named profile that has no key yet, then
  creates one; emails with no recognizable search URL keep the old
  name/matcher/default path. Migration `013`.
- **Why**: the name parsed out of the subject is a *label*. It gets folded by
  the mail server (#101), reworded by Idealista, or renamed by the owner, and
  each variant used to create another `SearchProfile`.
- **What counts as a search URL**: only `/areas/` links that carry a non-empty
  `shape`. The polygon is what tells two custom-area subscriptions apart, so a
  link that lost its query — wrapped mid-line in a text/plain part, or
  truncated — yields no identity rather than a key minted from the path.
- **Consequence to keep in mind**: `search_profiles.name` is no longer UNIQUE —
  two subscriptions may legitimately share a label with a different `shape`, so
  `merge_duplicate_profiles()` now refuses to merge a group holding different
  search keys (and carries the single key onto the survivor when it merges one
  that has it). Only labels the ingester invented (`is_auto_created`) are ever
  rewritten. Keys are **not** backfilled: existing profiles stay NULL until the
  next email for that subscription arrives. Identity conflicts (URL points at
  one profile, label at another) are logged and left alone, never merged
  automatically. An email linking to **several different** searches resolves to
  no profile at all — the listing is stored unassigned rather than guessed into
  a same-named subscription, and so does an email whose search URL *was* read
  but could not be resolved (contested retries exhausted, or the insert
  failed). Unassigned listings are surfaced in the UI since #111.
- **The catch-all stays a catch-all**, enforced by the schema: `013` adds
  `CHECK (source_search_key IS NULL OR is_default IS NOT TRUE)`, so a profile
  tied to one saved search cannot be the fallback for everything else — through
  the profile editor, the create form, the merge, or any route written later.
  On top of that, `get_default_profile()` only considers profiles with no search
  key, the merge refuses any group pairing the default with an identified
  search, a label claimed by two different subscriptions resolves to neither,
  and the "Make default" checkbox is disabled for an identified profile.
- **Every by-name profile lookup** now goes through
  `SearchProfileService.find_unidentified_by_name()`, which ignores rows that
  hold a search key. That includes the legacy-land migration, which would
  otherwise have poured the 168-row archive into a live subscription labelled
  "Legacy Lands". The only by-name scans that still see identified profiles are
  the deliberate conflict detectors.
- **Concurrency**: the dropped UNIQUE was also what protected check-then-insert
  in `get_or_create_profile_by_name()` / `get_default_profile()` from two
  overlapping ingestions, so `013` adds a partial unique index on `name` for
  rows with no search key — the old invariant, minus the case it wrongly
  blocked. Binding a key to an existing profile is a conditional UPDATE that
  fails and retries rather than re-pointing a row another ingestion just
  claimed.
- **CI**: the `pytest` job now runs a PostgreSQL service, because the migration
  SQL is PostgreSQL-only and cannot be executed by SQLite —
  `tests/test_postgres_migrations.py` applies the real files to a real server.
  That test caught a percent-sign collision in `013` that would otherwise have
  failed at deploy time, after the container had already been replaced.

### 🛟 Fixed: folded MIME headers are unfolded before anything parses them (2026-08-08, #101)
- **What**: `utils/email_headers.py` owns unfolding plus RFC 2047 decoding, and
  both IMAP services use it — for `Subject`, and now for `From`, which was not
  decoded at all.
- **Why**: a header longer than the line limit arrives folded (RFC 5322 2.2.3),
  and `extract_search_name()` matches with a `[^\r\n]` class, so the
  saved-search name was cut at the fold. Where the fold lands depends on the
  subject prefix — "New detached house" folds elsewhere than "Price reduction" —
  so one saved search produced four `SearchProfile` rows holding 41 listings
  between them. The same CR also reached the skip list, price-change detection,
  deal-type inference, classification, and the stored `Property.email_subject`.
- **Also fixed here**: `decode_header()` returns chunks, and joining them with a
  space breaks RFC 2047, where whitespace *between* two encoded-words is a
  separator. `=?utf-8?Q?John?= =?iso-8859-1?Q?Doe?=` decoded to `John Doe  `
  instead of `JohnDoe`. It predates this change and only ever touched `Subject`,
  but `From` is a new call site, so `make_header()` does the joining now.

### 🛟 Fixed: the four profiles of one saved search are repaired (2026-08-08, #103)
- **What**: `services/search_profile_repair_service.py` — an idempotent
  operator command, dry-run by default. On the owner's database it moved 38
  listings onto profile #8 (41 total) and deleted profiles 7, 9 and 10; a second
  run reports `clean`.
- **Why**: #101 stops new fragments; the existing ones were still split.
- **How a fragment is proven**: the repair replays the pre-#101 extraction over
  the stored subject and requires it to return precisely the name that profile
  carries. Not a heuristic and not a name shape — the bug itself, re-run. A
  prefix rule was tried and dropped, because a fold landing on punctuation
  (`Homes in Ciudad Quesada,\r\n Alicante`) leaves a name that is not a
  word-boundary prefix, and the owner has such a subscription. It follows that
  the repair can only ever act on rows written before #101.
- **What it will never do**: move a hand-reassigned listing; rename a profile
  that is not a proven fragment or that holds a second saved search; resolve a
  label two subscriptions share; delete a profile that still holds a listing,
  carries a search key, or is the default; create a profile. Every refusal is
  reported as `BLOCKED:` with its reason.
- **Concurrency**: one transaction, with the plan re-verified against the
  database before anything is written and the deletes flushed *inside* it, so a
  listing arriving mid-run aborts the repair rather than being orphaned by
  `ON DELETE SET NULL`. Exit codes never overstate what is known: `1` means
  nothing was committed, `2` committed but the after-report failed, `3` the
  COMMIT outcome is unknowable. Run it with ingestion stopped — that buys
  success, not safety.

### 🔑 Changed: `/properties` is the working page, `/lands` the archive (2026-08-08, #105)
- **What**: `/` and the navbar point at `/properties`. The cards/list toggle and
  the combined/investment/lifestyle modes moved over with it. `/lands` still
  works, labelled an archive.
- **Why**: `/lands` reads the `lands` table — 168 rows, newest 2026-02-18. Every
  fresh listing goes to `properties`, and of the 182 non-legacy rows there, 105
  are plots and 77 are homes. The mail stream is no longer land-only, and the
  `Land` model cannot represent a house. This reverses the 2026-08-07 decision.
- **No faked parity**: `Sea View` and the "to beach" sort are rendered
  unavailable with a reason, not wired to empty data — `Property` has no
  `travel_time_nearest_beach` column and no beach travel target, and per #98 not
  one of the 350 listings has a travel time. They come back when #98 is fixed.

### ✨ Added: several subscriptions can be picked at once (2026-08-08, #104)
- **What**: the profile control on `/properties` is a checkbox dropdown reading
  `All profiles` or `N selected`. Selection state is modelled explicitly as
  `auto | all | selected(ids)` in `services/profile_selection.py` and travels
  through every link — column sorting, both pagination directions, the CSV
  export and `/map`. Old single-`profile_id` bookmarks keep working.
- **Why**: the owner asked to see everything at once or tick several searches,
  without growing the filter bar. It stays one control.
- **`No subscription`** sits below a divider as a peer of the profiles, never
  part of `all`. A listing with no `search_profile_id` is otherwise unreachable
  in the UI — it has no id to type — and #102 made that a legal state.
- **With more than one profile selected**, profile-specific travel targets and
  the recalculate buttons are hidden with an explanation, on the page and on the
  map: custom target ids belong to different profiles, so a union would mislead.

### 🛟 Fixed: a refused Google API is no longer stored as a search result (2026-08-08, #98)
- **What**: `PropertyTravelService.calculate_for_property()` wrote
  `status: "not_found"` and returned `True` whether Google had answered "there
  is no train station nearby" or refused the request outright
  (`REQUEST_DENIED`, missing key, HTTP or network error). Refusals now get their
  own `status: "unavailable"` with a reason code and the stage that failed, a
  run where every target was refused returns `False`, and the run logs one ERROR
  carrying the code Google returned. `Property.travel["api_status"]` records the
  verdict (`ok` / `degraded` / `unavailable`) and per-reason counts.
  `EnrichmentService` (legacy lands) got the same treatment: a refused Places
  search no longer becomes `<amenity>_available: False`, and `enrich_land()`
  returns `False` when Google refused.
- **Why**: on 2026-08-08 three independent outages (billing off, Distance Matrix
  not enabled, a key from the wrong project) were all invisible — 0 of 350
  properties had ever received a travel time while every run reported success.
- **Consequence to keep in mind**: refused answers are no longer cached, and the
  distance cache namespace moved to `property_travel_v2` so entries poisoned by
  the outage are not served for another week. A run that Google refused keeps
  the previously stored travel times instead of overwriting them with an empty
  structure. Haversine fallbacks (no Maps key) are now labelled
  `status: "estimated"` rather than passing as measured times. Removed
  `_create_fallback_amenities_data()`, which invented amenity distances
  (800 m to a supermarket, and so on) whenever the Places call raised.

### 🛟 Fixed: last_seen_uid no longer runs ahead of the database (2026-08-08, #24)
- **What**: both IMAP services persisted `max(uids)` inside
  `get_idealista_emails()`, before `run_ingestion()` had written anything. A
  crash, restart or DB failure between fetch and commit lost those listings,
  price changes and removal notices permanently and silently. The cursor now
  advances per email, only after that email's rows are committed (or it was
  deliberately filtered out), via the new `utils/uid_cursor.py`.
- **Why**: silent, permanent data loss with no signal — `SyncHistory` could not
  be reconciled against the cursor.
- **Consequence to keep in mind**: the cursor file is now written atomically,
  and an unreadable/corrupt cursor **raises** instead of silently resetting to 0
  and reprocessing the whole mailbox. An email that keeps failing to commit
  holds the cursor back (logged as `Holding last_seen_uid at …`), so a stuck
  ingestion is now visible in the logs instead of losing mail.

### 🔓 Removed: admin authentication (2026-08-08)
- **Login removed**: deleted `utils/auth.py`, the `/login` and `/logout` routes,
  `templates/login.html`, all 42 `@admin_required` decorators and every inline
  `check_admin_auth()` gate. `ADMIN_API_TOKEN` is no longer read anywhere.
- **Why**: single-owner local tool, published only on `127.0.0.1:5001`; the
  owner wanted direct access rather than re-entering a token each month.
- **Consequence to keep in mind**: nothing gates the API any more, and the JSON
  blueprints stay CSRF-exempt — the loopback binding in `docker-compose.yml` is
  now the only thing standing between the database (and the paid AI/enrichment
  endpoints) and the network. Put authentication back before exposing the app.

## [2.0.0] - 2025-09-11

### 🚀 Major Features Added
- **Dual Score System**: Implemented separate Investment Score (32%) and Lifestyle Score (68%) for targeted property analysis
- **Three Analysis Modes**: Investment-focused, Lifestyle-focused, and Balanced approaches for different user needs
- **Complete Bilingual Support**: Full English/Spanish localization with session-based language switching
- **AI-Powered Analysis**: Claude Sonnet 4 integration for detailed investment insights and market predictions

### ⚡ Performance Improvements
- **Database Optimization**: Added 7 strategic indexes resulting in 3-5x faster query performance
- **Memory Efficiency**: Implemented deferred JSONB column loading, reducing memory usage by 60%
- **Caching Layer**: Added Flask-Caching with Redis support for API responses and enrichment data
- **Page Load Speed**: Achieved 40% reduction in page load times through query optimization

### 🔒 Security Enhancements
- **Fail-Closed Authentication**: Implemented secure admin authentication with token-based access
- **Rate Limiting**: Added configurable rate limits for different endpoint types
- **Input Validation**: Comprehensive SQLAlchemy constraints and form validation
- **Secrets Management**: Centralized SecurityValidator with startup validation

### 🐛 Critical Bug Fixes
- **Scheduler Reliability**: Added file-based locking to prevent duplicate scheduler instances
- **Download Endpoints**: Fixed filename handling for property data exports
- **Email Backend**: Simplified to stable IMAP implementation, removed deprecated Gmail API service
- **UI Text Overflow**: Implemented comprehensive text truncation with tooltips for Spanish content

### 🧹 Code Quality Improvements
- **Dead Code Removal**: Cleaned up unused gmail_service.py and related test files
- **Architecture Refactoring**: Improved separation of concerns with service layer pattern
- **Error Handling**: Enhanced error handling and logging throughout the application
- **Testing**: Expanded test coverage for critical functionality

## [1.5.0] - 2025-09-10

### Added
- **Manual Sync Button**: User-friendly one-click property synchronization
- **Enhanced UI Components**: Improved property cards and table layouts
- **Export Functionality**: CSV export with filtered data support

### Fixed
- **Language Switching**: Improved language toggle reliability
- **Mobile Responsiveness**: Better mobile device support
- **Data Validation**: Enhanced property data validation

## [1.4.0] - 2025-09-09

### Added
- **Advanced Filtering**: Multi-criteria filtering by price, location, scores
- **Sorting Options**: Sortable columns in property tables
- **Responsive Design**: Enhanced mobile and tablet support

### Changed
- **UI Theme**: Refined dark theme for better accessibility
- **Navigation**: Improved navigation structure and user flow

## [1.3.0] - 2025-09-08

### Added
- **MCDM Scoring**: Multi-Criteria Decision Making methodology implementation
- **Weight Management**: Dynamic scoring weight adjustment interface
- **Location Intelligence**: Enhanced geocoding and distance calculations

### Fixed
- **API Rate Limiting**: Improved handling of external API rate limits
- **Data Enrichment**: More reliable enrichment process

## [1.2.0] - 2025-09-07

### Added
- **Email Integration**: Automated Idealista email processing
- **Property Enrichment**: External API integration for location data
- **Scoring System**: Initial implementation of property scoring

### Security
- **Environment Variables**: Moved all secrets to environment configuration
- **Input Sanitization**: Added protection against common web vulnerabilities

## [1.1.0] - 2025-09-06

### Added
- **Database Models**: Core property and scoring models
- **Web Interface**: Basic Flask web application
- **Property Display**: Initial property listing functionality

## [1.0.0] - 2025-09-05

### Added
- **Initial Release**: Basic Flask application structure
- **PostgreSQL Integration**: Database setup and configuration
- **Project Foundation**: Core architecture and development environment

---

## Legend

- 🚀 **Major Features**: Significant new functionality
- ⚡ **Performance**: Speed and efficiency improvements
- 🔒 **Security**: Security enhancements and fixes
- 🐛 **Bug Fixes**: Bug fixes and stability improvements
- 🧹 **Code Quality**: Refactoring and code improvements
- 📝 **Documentation**: Documentation updates
- 🎨 **UI/UX**: User interface and experience improvements

## Upcoming Features

### Planned for v2.1.0
- **Background Processing**: Async enrichment jobs for better performance
- **Advanced Analytics**: Enhanced investment metrics and market analysis
- **Export Enhancements**: Additional export formats and scheduling
- **API Documentation**: Comprehensive API documentation with examples

### Under Consideration
- **Mobile App**: Native mobile application
- **Multi-Region Support**: Support for other Spanish regions
- **Advanced Mapping**: Interactive maps with property overlays
- **Machine Learning**: Predictive pricing models
