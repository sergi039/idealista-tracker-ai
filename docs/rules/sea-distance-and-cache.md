# Sea distance and the shared cache

Moved verbatim from `CLAUDE.md` (lines 730–784 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

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
