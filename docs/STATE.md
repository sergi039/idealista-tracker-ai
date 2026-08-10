# State Snapshot — 2025-12-21

This repo is a **separate universal build** that must run side-by-side with the legacy land tracker without mixing code, Docker resources, or data.

## Repositories (local)

- Legacy (lands): `/Users/ss/IdealistaRank` (`main`)
  - URL: `http://localhost:5001/`
- Universal (properties): `/Users/ss/IdealistaRank-properties-universal` (`feature/properties-universal`)
  - URL: `http://localhost:5050/`

## Hard Rules (do not change)

- **Ports**
  - Legacy app stays on: `5001`
  - Universal app stays on: `5050`
- **Docker resources are isolated**
  - Universal containers: `idealista-universal-app`, `idealista-universal-db`
  - Universal network: `idealista-universal-network`
  - Universal volume: `idealista-universal-pgdata`
- **Databases are isolated**
  - Legacy DB name: `idealista` (debug port `5433`)
  - Universal DB name: `idealista_universal` (debug port `5434`)
- **Browser session is isolated**
  - Universal cookie name: `SESSION_COOKIE_NAME=idealista_universal_session`

## Current State (implemented)

### Universal (5050)

- **Ingestion target**: `INGESTION_TARGET=properties`
- **Sale-only**: configurable in `Settings → Property settings` (DB override, fallback to `SALE_ONLY=true`)
- **Category skip**: configurable in `Settings → Property settings` (DB override, fallback to `EXCLUDED_PROPERTY_CATEGORIES`)
- **Gmail label routing**
  - For Gmail (`imap.gmail.com`) the service searches in `All Mail` using **X-GM-RAW**:
    - `from:noresponder@idealista.com label:<IMAP_FOLDER>`
  - This makes `IMAP_FOLDER` effectively the Gmail label name.
- **Full Sync (ops)**
  - Available in `Settings → Property settings` (admin required).
  - Resets the ingestion cursor (last-seen UID) to 0 and reprocesses the entire Gmail label.
- **Search Profiles**
  - `SearchProfile` is auto-created from the **saved-search name** extracted from subject/body.
  - `/properties` auto-selects the most recently active profile with properties if `Default` is empty (prevents “0 properties found” on first load).
- `/map` auto-selects the profile with the most properties that have coordinates (fallback: most recent active profile).
- **AI analysis (properties)**
  - Claude is stored in `Property.ai_analysis` (legacy parity).
  - Provider variants are stored in `property_ai_analysis_variants` (`provider=claude|openai`).
  - Comparison endpoint: `GET /api/property/<id>/analysis/compare` (baseline is placeholder until a universal market model exists).
- **UI parity**
  - `/properties`: `Export CSV` is in the navbar; `Manual Sync` is in the page header.
  - `/properties`: `Category/Subtype/Municipality` choices come from the subscriptions currently on screen (subtypes narrow again to the chosen category); `Unclassified` is offered only when such rows exist there.
  - Navbar order: `Properties → Profiles → Map → Scoring Criteria → Settings`.
  - `/properties/<id>`: has `Enrich with Google APIs` + AI tabs (Claude/ChatGPT) + comparison block.
- **DB sanity (as of this snapshot)**
  - Universal DB contains properties and **no land** rows (land is skipped via `EXCLUDED_PROPERTY_CATEGORIES=land`).

### Legacy (5001)

- **Gmail ingestion now respects `IMAP_FOLDER`** label via X-GM-RAW (prevents accidental non-land ingestion).
- **Price-change emails do not create new `Land` rows** (only update existing ones).
- If accidental non-land items appear in legacy DB, remove them directly from the legacy database (data fix).

### Tests (as of this snapshot)

- Universal: `python3 -m pytest -q` → `155 passed`

## Email Isolation Modes (choose per environment)

Default recommendation:

- Production: **Option 1**
- Local dev: **Option 2** is OK

### Option 1 (recommended): separate Gmail labels

- Legacy `.env`: `IMAP_FOLDER=IdealistaLand` (example)
- Universal `.env`: `IMAP_FOLDER=IdealistaProperties` (example)
- No need for `EXCLUDED_PROPERTY_CATEGORIES`.

### Option 2 (current / acceptable): shared Gmail label + category filtering

- Both apps read the same Gmail label (same `IMAP_FOLDER`).
- Universal `.env` must keep: `EXCLUDED_PROPERTY_CATEGORIES=land`
- Risk: correctness depends on classification staying accurate; keep tests + rules updated.

## Known Limitations

- Idealista web pages often return HTTP 403 for automated requests; ingestion is designed to be **email-driven**.
- Classification is regex-based and should be refined as new email samples appear.
- **overpass-api.de refuses more than it admits.** All four behaviours below
  were measured against the live instance on 2026-08-09, not inferred, and the
  first three silently produced "no amenities nearby" until #144 fixed it.
  This entry documents shipped behaviour: the handling lives in
  `EnrichmentService._fetch_osm_amenities` and `_osm_refusal`
  (`services/enrichment_service.py`) plus `fetch_coastline_points`
  (`services/sea_view_service.py`), and is pinned by
  `tests/test_overpass_user_agent_and_refusal.py` — `TestOutgoingRequest`
  for the User-Agent and the backoff, `TestRefusalIsNotAnEmptyResult` for
  all four refusal shapes including the `remark` case — and, for the
  property path, by `tests/test_issue_152_property_osm_amenities.py`.
  - It answers `406 Not Acceptable` to the default `python-requests`
    User-Agent, **and** to any User-Agent carrying a parenthetical comment.
    Only a bare product token is served — `utils/http.py` `HTTP_USER_AGENT`.
  - It grants **two query slots per IP** and answers `504` while both are
    busy. That is a queue, not a broken request: a slot frees up in roughly a
    minute, so a retry backoff has to be measured in tens of seconds. The
    half-second default in `utils/http.py` gives up long before the server
    would have answered.
  - It reports its own failures **inside a `200`**: a query that times out or
    runs out of memory returns `{"elements": [], "remark": "runtime error:
    Query timed out ..."}`. Reading `elements` off such a body records a
    computed negative for a query that never ran.
  - It answers `429 Too Many Requests` when the *rate* is too high, which is a
    different complaint from the `504` above: the slots are not busy, the
    server is unwilling to open one this soon. It is retried rather than
    recorded — `_DEFAULT_RETRY_STATUSES` in `utils/http.py` has always carried
    429 — so it never became a computed negative, which is why it went unnoticed
    until a run was actually paced and counted.
  - Every Overpass caller in the process shares one pacer,
    `utils/http.py` `OVERPASS_GATE`, because the two slots above are per IP
    rather than per caller (#152). Pacing the coastline query and the amenity
    query separately paced neither.
  - **The pacer belongs to the transport, not the caller.** `gate=` is passed
    to `request_with_retries`, which takes it before *every* attempt. A caller
    that took the gate itself and then handed the retry loop a free hand paced
    its lookups and left the retries unpaced — and a retry storm is exactly
    when the endpoint is asking for less traffic. The backoff does not replace
    it: the backoff is what this server just asked for, the gate is what the
    process allows itself across every caller, and waiting for the gate after
    the backoff yields the longer of the two at no extra cost.
  - **The interval is 5 s, and it is measured rather than chosen.** A dry-run
    amenity backfill over 20 properties at the previous 2 s spent 39 requests
    on 20 answers: 16 served, 8 refused with `504`, 15 with `429`. More than
    half the traffic was the server asking for less of it, and the run took
    ~12 minutes — about two hours had it covered the whole table. Nothing was
    lost, because both statuses are retried, but a backoff is not a substitute
    for a rate. An interactive Enrich pays nothing for the wider interval: the
    gate is idle between presses, so a single lookup never waits.
- Enrichment writes to `Land.infrastructure_extended`, a plain
  `db.Column(JSON)` with **no `MutableDict`**. Mutating the loaded dict and
  assigning the same object back never marks the attribute dirty, so the flush
  emits no UPDATE and the write is lost on commit while still looking correct
  in memory. Copy before merging — `EnrichmentService._write_infrastructure_extended`
  is the one place that does it. A test that does not reload cannot see this.
  `Property.enrichment` is the same kind of column one level deeper, so its
  writer (`_write_property_infrastructure_extended`) copies the blob *and* the
  section and calls `flag_modified`, as `services/sea_distance_service.py` does.

## Next

See `TODO.md`.
