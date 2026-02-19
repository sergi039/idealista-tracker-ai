# Universal Properties (WIP)

For the latest “what’s running and how it’s isolated”, see `docs/STATE.md` and `TODO.md`.

This branch introduces a new `Property` model and `/properties` UI as the foundation for making the app work for **any Idealista sale listing type** (housing, land, garage, commercial, etc.) across **all Spain**.

The legacy `Land` model and `/lands` UI remain available during the migration.

## What Exists Now

- New DB model: `Property` (`models.py`)
- Search profiles: `SearchProfile` (`models.py`)
- New UI:
  - `/properties`
  - `/properties/<id>`
- Profiles list (MVP): `/profiles`
- New ingestion service (IMAP → Property): `services/property_imap_service.py`
- Property scoring (category-aware): `services/property_scoring_service.py`
- Property AI analysis (category-aware): `services/property_ai_service.py` + `POST /api/property/<id>/analyze/structured`
- Properties API (list/detail): `GET /api/properties` and `GET /api/properties/<id>`
- Migration helper (legacy Land → Property): `POST /api/migrate/lands-to-properties`
- Settings (DB-backed via `app_settings`):
  - `property_classification_rules` (regex rules, ordered by priority)
  - `travel_targets` (custom user destinations; can be empty)

Search profiles can override the global defaults via `SearchProfile.classification_rules` and `SearchProfile.travel_targets`.

## Running Ingestion

`/api/ingest/email/run` now selects ingestion target based on:

- `INGESTION_TARGET=properties` (default in this branch)
- `INGESTION_TARGET=lands` (legacy)

Incoming emails are assigned to a `SearchProfile` via `SearchProfile.email_matchers` (regex). If nothing matches, the default profile is used (auto-created).

## Next Milestones

1. Update UI to optionally consume `/api/properties` (useful for external clients).
2. Extend scoring/AI registry beyond `housing` + `land` (garage/commercial/building/new development).
3. Make enrichment write into `Property.enrichment` (category-aware).
4. Migration cleanup: retire legacy `/lands` once stable (optional).

## Migration (Land → Property)

This repo contains an optional one-way migration helper to copy legacy `lands` rows into `properties`.

- Endpoint: `POST /api/migrate/lands-to-properties`
- Body (JSON):
  - `dry_run` (bool, default `true`)
  - `limit` (int, optional)
  - `profile_name` (string, optional; default `"Legacy Lands"`)

Example:

```bash
curl -X POST http://localhost:5050/api/migrate/lands-to-properties \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  -d '{"dry_run": false, "profile_name": "Legacy Lands"}'
```

## Notes

Idealista category pages/sitemaps return HTTP 403 in automated requests, so the system is designed to be **config-driven** based on the email subjects/titles and can be refined from real incoming email samples.
