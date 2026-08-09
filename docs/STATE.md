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

## Next

See `TODO.md`.
