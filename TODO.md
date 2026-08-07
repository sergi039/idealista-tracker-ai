# TODO (Universal 5050)

This is the active backlog for the **new universal build** (`http://localhost:5050/`). Legacy (`http://localhost:5001/`) should remain stable and isolated.

## P0 — Decisions & docs

- [x] Decide email isolation mode per environment (default):
  - Production: separate Gmail labels
  - Local dev: shared label + `EXCLUDED_PROPERTY_CATEGORIES=land`
- [x] Document both isolation modes (`README.md` / `docs/STATE.md`).
- [x] Pick the default mode per environment and document it (prod: separate labels; dev: shared label OK).
- [x] Add a short “How to run both builds locally” snippet (commands + ports) to `README.md`.

## P1 — Universalization correctness

- [ ] Expand `DEFAULT_PROPERTY_CLASSIFICATION_RULES` from real sale email samples (Spain):
  - Priority alert types (currently subscribed): `housing` (apartment/house) + `land` (plot).
  - [x] Add common ES synonyms (villa/adosado/parking/promoción, etc.)
  - [x] Document defaults (`docs/PROPERTY_TYPES.md`)
  - [x] Add tests for common titles
  - [x] Add `Unclassified` filter (UI + `/api/properties?category=__none__`)
  - [ ] Confirm coverage for remaining sale types (defer until we have alerts/samples):
    - `garage` (garage / storage)
    - `commercial` (office / industrial / retail)
    - `building`
    - `new_development`
  - [ ] Collect 1–2 real email samples per deferred type and add regression tests
  - [ ] Refine rules from more email samples (avoid overfitting)

## P1 — CI / quality gates

- [ ] Add minimal CI workflow (GitHub Actions): run `pytest tests/` on push/PR. Today there is no `.github/workflows/` at all, so the 216 tests gate nothing. Tracked in [#31](https://github.com/sergi039/idealista-tracker-ai/issues/31). Source: AgentsRoom audit 2026-08-07 (`Skills/docs/agentsroom-projects.md` §6).

## P2 — UX polish

- [x] Remove legacy links from the universal navbar (projects stay isolated).
- [x] Ensure `/properties` list fits without horizontal scroll on desktop.
- [x] Add property-detail parity: Google enrich + AI tabs (Claude/ChatGPT) + comparison block.

## Done (completed in this iteration)

- [x] Keep legacy on `5001` and universal on `5050`.
- [x] Isolate Docker resources (containers/network/volume) and DBs.
- [x] Fix legacy ingestion to respect Gmail label and avoid creating new `Land` rows on price-change emails.
- [x] Add universal ingestion safeguards: `SALE_ONLY` + `EXCLUDED_PROPERTY_CATEGORIES`.
- [x] Add admin UI controls for `SALE_ONLY` and `EXCLUDED_PROPERTY_CATEGORIES` (DB override, env fallback).
- [x] Fix “empty /properties” default by auto-selecting the most recent active profile with data.
- [x] Align `Manual Sync`/`Export CSV` placement with legacy and move `Settings` to end of navbar.
- [x] Add/adjust tests for isolation and ingestion rules.
- [x] Add tests for universal property enrichment + AI endpoints.
