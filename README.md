# Idealista Watch & Analyze

Universal tracking + AI analysis for Idealista listings — what you see depends on your own Idealista subscription/emails (saved searches).

- Import listings from your Gmail/IMAP folder (Idealista emails)
- Analyze and rank properties with editable scoring (Investment / Lifestyle / Combined)
- List view, card view, and map view with quick links (Google Maps / Idealista / Our Maps)
- Optional enrichment via Google APIs (places, distances, travel times)
- Optional AI analysis via Claude + ChatGPT, plus a side-by-side comparison
- Favorites, filters, CSV export, bilingual UI (EN/ES), light/dark theme

Not affiliated with Idealista.

## Security (important)

This project is safe to use for **personal/private deployments** (localhost, VPN, private network).

If you plan to expose it as a **public web app**, do the hardening items below first — otherwise anyone can trigger expensive actions (Google/Claude/ChatGPT) and you risk XSS/SSRF-style issues.

### Hardening plan (before public hosting)

- Protect write/expensive endpoints with auth (`/api/land/*/enrich`, `/api/ingest/email/run`, AI analysis endpoints).
- Remove `DEV_MODE` admin bypass and require `ADMIN_API_TOKEN` outside tests.
- Eliminate unsafe `innerHTML` injections in templates (sanitize/escape model output).
- Add allowlist validation for outbound URL fetches (listing status checker).
- Move long-running tasks to background jobs + retries/backoff.

## Roadmap (queued)

Next implementation batch is tracked in `PROJECT_PLAN_2025-12-12.md`.

## Quick Start (Docker)

Prerequisite: Docker Desktop.

```bash
git clone https://github.com/sergi039/idealista-land-tracker.git
cd idealista-land-tracker
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:5001`.

## Configuration

Edit `.env` (start from `.env.example`):

**Required**
- `SESSION_SECRET`: Flask session secret
- `IMAP_USER`, `IMAP_PASSWORD`: Gmail + App Password

**Optional**
- `GOOGLE_MAPS_API_KEY`, `GOOGLE_PLACES_API_KEY`: enables enrichment (maps/places/travel times)
- `ANTHROPIC_API_KEY`: enables Claude AI analysis (`ANTHROPIC_MODEL` optional)
- `OPENAI_API_KEY`: enables ChatGPT AI analysis (`OPENAI_MODEL` optional)

Tip: set `IMAP_FOLDER=Idealista` (or any folder/label where you keep the emails).

## Where data is stored

When running via Docker Compose, Postgres data is persisted in the Docker volume `idealista-pgdata`.

## Contributing

Issues and PRs are welcome. Keep changes focused and user-facing.

## License

MIT — see `LICENSE`.
