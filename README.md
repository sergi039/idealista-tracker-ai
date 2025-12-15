# Idealista Watch & Analyze

**Stop losing track of properties. Stop manually comparing listings. Let AI help you find the best deal.**

A self-hosted tool that automatically imports your Idealista saved search emails, ranks properties by investment potential and lifestyle fit, and provides AI-powered analysis to help you make smarter decisions.

> Not affiliated with Idealista. Works with your existing Idealista email alerts.

---

## Why Use This?

**The Problem:** You're searching for property on Idealista. You set up email alerts, but:
- Emails pile up and you lose track of what you've seen
- Comparing properties manually is tedious
- You miss price drops or status changes
- You can't easily see all listings on a map
- Making a decision feels overwhelming

**The Solution:** This tool automatically:
- Imports all your Idealista email alerts into one dashboard
- Scores each property (investment potential + lifestyle fit)
- Shows everything on a map with filters
- Uses AI (Claude/ChatGPT) to analyze properties and compare options
- Tracks which listings are still active vs. sold/removed

---

## Features

| Feature | Description |
|---------|-------------|
| **Auto-import** | Reads your Gmail/IMAP folder and extracts all Idealista listings |
| **Smart scoring** | Configurable scoring for Investment, Lifestyle, and Combined ranking |
| **Multiple views** | List, cards, or interactive map with clustering |
| **AI analysis** | Get detailed property analysis from Claude and/or ChatGPT |
| **Side-by-side comparison** | Compare AI recommendations from both models |
| **Enrichment** | Add travel times, nearby places via Google APIs |
| **Favorites & filters** | Mark favorites, filter by price/size/score/status |
| **CSV export** | Export your data anytime |
| **Bilingual** | English and Spanish UI |
| **Dark mode** | Light and dark themes |

---

## Quick Start

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Gmail account with Idealista email alerts

### Installation (5 minutes)

```bash
# 1. Clone the repository
git clone https://github.com/sergi039/idealista-tracker-ai.git
cd idealista-tracker-ai

# 2. Create your config file
cp .env.example .env

# 3. Edit .env with your settings (see Configuration below)

# 4. Start the app
docker compose up -d --build

# 5. Open in browser
open http://localhost:5001
```

---

## Configuration

Edit the `.env` file. Here's what you need:

### Required Settings

| Setting | What it is | How to get it |
|---------|-----------|---------------|
| `SESSION_SECRET` | Random string for security | Run: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DB_PASSWORD` | Database password | Choose any password (e.g., `mypassword123`) |
| `IMAP_USER` | Your Gmail address | e.g., `yourname@gmail.com` |
| `IMAP_PASSWORD` | Gmail App Password | [Create App Password](https://myaccount.google.com/apppasswords) (NOT your regular password) |

#### How to create a Gmail App Password:
1. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
2. Select "Mail" and your device
3. Click "Generate"
4. Copy the 16-character password (no spaces)
5. Paste it as `IMAP_PASSWORD` in your `.env`

> **Note:** You need 2-Factor Authentication enabled on your Google account to create App Passwords.

### Optional Settings (Enhanced Features)

#### Google APIs (for maps, places, travel times)

| Setting | What it enables |
|---------|----------------|
| `GOOGLE_MAPS_API_KEY` | Interactive maps, travel time calculations |
| `GOOGLE_PLACES_API_KEY` | Nearby places (metro, supermarkets, etc.) |

**How to get:** [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → Create credentials → API Key → Enable Maps JavaScript API, Places API, Distance Matrix API.

#### AI Analysis (Claude and/or ChatGPT)

| Setting | What it enables |
|---------|----------------|
| `ANTHROPIC_API_KEY` | Claude AI property analysis |
| `OPENAI_API_KEY` | ChatGPT property analysis |

**How to get:**
- Anthropic: [console.anthropic.com](https://console.anthropic.com/) → API Keys
- OpenAI: [platform.openai.com](https://platform.openai.com/api-keys) → API Keys

> **Tip:** You can use one or both AI providers. Having both enables side-by-side comparison.

### Example `.env` file

```bash
# Required
SESSION_SECRET=a1b2c3d4e5f6...your-random-string...
DB_PASSWORD=mypassword123
IMAP_USER=yourname@gmail.com
IMAP_PASSWORD=abcd efgh ijkl mnop

# Optional - Google (for maps)
GOOGLE_MAPS_API_KEY=AIza...
GOOGLE_PLACES_API_KEY=AIza...

# Optional - AI (for analysis)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Email folder containing Idealista alerts (default: Idealista)
IMAP_FOLDER=Idealista
```

---

## Usage Tips

1. **Create a Gmail label/folder** called "Idealista" and set up a filter to automatically move Idealista emails there
2. **First sync** may take a few minutes if you have many emails
3. **Scoring weights** can be customized in Settings
4. **AI analysis** costs money per API call — use it on shortlisted properties

---

## Data Storage

Your data is stored locally in a Docker volume (`idealista-pgdata`). Nothing is sent to external servers except:
- Google APIs (if configured) — for maps/places
- AI APIs (if configured) — for property analysis

---

## Security Note

This tool is designed for **personal use** (localhost, home network, VPN).

If you want to expose it publicly, additional security hardening is needed — see [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Can't connect to Gmail | Make sure you're using an App Password, not your regular password |
| No emails importing | Check `IMAP_FOLDER` matches your Gmail label exactly |
| Maps not showing | Verify Google API keys and enable required APIs in Google Console |
| AI analysis not working | Check API key is valid and has credits |

**View logs:**
```bash
docker compose logs -f app
```

---

## Contributing

Issues and PRs welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
