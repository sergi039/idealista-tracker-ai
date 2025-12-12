# Idealista Land Watch & Rank

Track and rank land listings from Idealista in one simple dashboard.

- Import listings from your Gmail/IMAP folder (Idealista emails)
- Score each property (Investment / Lifestyle / Combined) with editable criteria
- List view, card view, and a map view with quick links (Google Maps / Idealista / Our Maps)
- Optional enrichment via Google APIs (places, distances, travel times)
- Optional AI analysis via Anthropic Claude
- Favorites, filters, CSV export, bilingual UI (EN/ES), light/dark theme

Not affiliated with Idealista.

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
- `GOOGLE_API_KEY`: enables enrichment (maps/places/travel times)
- `ANTHROPIC_API_KEY`: enables AI analysis

Tip: set `IMAP_FOLDER=Idealista` (or any folder/label where you keep the emails).

## Where data is stored

When running via Docker Compose, Postgres data is persisted in the Docker volume `idealista-pgdata`.

## Contributing

Issues and PRs are welcome. Keep changes focused and user-facing.

## License

MIT — see `LICENSE`.
