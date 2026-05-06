# SecDigest

A self-hosted daily security newsletter tool. Pulls top stories from Hacker News and subscribed RSS feeds, scores them for security relevance using Claude AI, generates summaries, and serves a password-protected web UI where you can curate, design, and send to a subscriber list.

---

## Features

- **Multi-source fetching** — HN top + new stories and any number of RSS/Atom feeds
- **AI curation** — Claude Haiku scores each article 0–10 for security relevance; only high-relevance articles make it through
- **Article pool** — all scored articles are stored; the top N auto-selected for the newsletter, the rest available in the pool to promote manually
- **Summaries** — Claude fetches the article page and writes a 2–3 sentence technical summary per article
- **Email builder** — live iframe preview with swappable templates (Dark Terminal, Clean Light, Minimal, 2-Column Grid), editable subject line, optional table of contents with anchor links
- **Per-subscriber unsubscribe links** — each email gets a unique token; one-click unsubscribe page
- **Curator** — edit summaries inline, exclude/include articles, drag to reorder, add manual articles or editorial notes
- **Archive** — browse and resend any past newsletter

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/) (Claude Haiku — cheap, ~$0.01–$0.05/day)
- SMTP credentials for sending (optional — the UI is fully usable without sending)

---

## Quick Start

```bash
# 1. Clone and enter
git clone https://github.com/your-org/secdigest
cd secdigest

# 2. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — set at minimum:
#   SECRET_KEY=<random string>
#   ANTHROPIC_API_KEY=sk-ant-...

# 4. Run (.env is auto-loaded from the project root)
python run.py
```

Open [http://localhost:8080](http://localhost:8080) — default password is **`secdigest`** (change it immediately in Settings).

---

## Configuration

All settings can be set via `.env` at startup, or changed live on the `/settings` page (stored in SQLite). `.env` values only apply on first run for DB-backed keys.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me` | Session signing key — use a long random string in production |
| `ANTHROPIC_API_KEY` | — | Required for curation scoring and article summarisation |
| `PASSWORD_HASH` | auto | bcrypt hash of the login password. Auto-generated from `secdigest` on first run. |
| `SMTP_HOST` | — | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` | `587` for STARTTLS, `465` for SSL |
| `SMTP_USER` | — | SMTP login username |
| `SMTP_PASS` | — | SMTP password or app password (spaces preserved as-is) |
| `SMTP_FROM` | — | e.g. `SecDigest <you@example.com>` |
| `BASE_URL` | `http://localhost:8000` | Public URL — used in unsubscribe links |
| `FETCH_TIME` | `00:00` | 24h local time for the daily auto-fetch |
| `HN_MIN_SCORE` | `50` | Minimum HN upvote score for a story to be considered |
| `MAX_ARTICLES` | `15` | Total articles to store per day (the pool size) |
| `MAX_CURATOR_ARTICLES` | `10` | How many of the top articles are auto-included in the newsletter |

**Gmail:** use `smtp.gmail.com`, port `587` or `465`, and a [16-character App Password](https://myaccount.google.com/apppasswords) — regular passwords are rejected by Google. Enable IMAP in Gmail settings first.

---

## Running in Production

### With systemd

```bash
sudo nano /etc/systemd/system/secdigest.service
```

```ini
[Unit]
Description=SecDigest
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.web.app:app --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now secdigest
sudo systemctl status secdigest
```

### Behind nginx (recommended)

```nginx
server {
    listen 80;
    server_name secdigest.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Add TLS with `certbot --nginx -d secdigest.yourdomain.com`.

---

## Project Structure

```
secdigest/
├── run.py                       # Dev entry point (python run.py)
├── requirements.txt
├── .env.example
├── secdigest/                   # Main Python package
│   ├── config.py                # Env config and defaults
│   ├── db.py                    # All SQLite operations
│   ├── fetcher.py               # HN API fetch + Claude curation scoring
│   ├── rss.py                   # RSS/Atom feed fetching and parsing
│   ├── summarizer.py            # Claude per-article summaries (fetches article text)
│   ├── mailer.py                # SMTP HTML email rendering and sending
│   ├── scheduler.py             # APScheduler daily job
│   └── web/
│       ├── app.py               # FastAPI app factory + auth routes
│       ├── auth.py              # Password helpers + session check
│       ├── routes/
│       │   ├── newsletter.py    # Day view, curator, pool, email builder, send
│       │   ├── feeds.py         # RSS feed management
│       │   ├── prompts.py       # Curation and summary prompt CRUD
│       │   ├── subscribers.py   # Subscriber list management
│       │   ├── settings.py      # Settings + SMTP test + LLM audit log
│       │   ├── email_templates_route.py  # Email template CRUD
│       │   └── unsubscribe.py   # Public one-click unsubscribe
│       ├── templates/           # Jinja2 HTML templates
│       └── static/              # CSS, JS, favicon
```

---

## How It Works

### 1. Daily Fetch

Triggered automatically at the configured time, or manually via the Fetch button:

- Pulls HN top 200 stories and newest 100 stories; filters by minimum score
- Fetches all active RSS/Atom feeds (configurable per-feed article limit)
- Deduplicates across both sources and against all historical article URLs
- Scores each article individually via Claude Haiku (0–10 security relevance)
- Falls back to keyword matching if Claude is unavailable
- Stores all articles scoring ≥ 5.0 (up to `MAX_ARTICLES`); top `MAX_CURATOR_ARTICLES` are auto-included, the rest go into the pool

### 2. Article Pool

The `/pool` page for any day shows all scored articles sorted by relevance. You can:
- Individually promote or demote articles
- Click **Auto-select Top N** to re-rank and batch-promote the best articles

### 3. Summarisation

After fetch (or on demand per article):
- Fetches the article's full page content
- Claude Haiku writes a 2–3 sentence summary tailored to the content type (vulnerability, opinion, tool, etc.)
- System prompt is cache-controlled for cost efficiency — bulk summarisation reuses the cached prompt

### 4. Curator

- Edit summaries inline or regenerate with one click (live spinner + auto-refresh)
- Exclude/include articles; drag to reorder
- Add articles manually by URL, or add editorial notes (no URL required)

### 5. Email Builder

- Live iframe preview of the rendered email
- Switch between built-in templates: Dark Terminal, Clean Light, Minimal, 2-Column Grid
- Edit template HTML directly in the UI or create custom templates
- Optional table of contents with anchor links to each article
- Per-subscriber unsubscribe links injected automatically on send

### 6. Sending

- Multipart HTML + plain-text email via SMTP
- Each subscriber gets a personalised email with their unique unsubscribe token
- Use the **Test Connection** button in Settings to diagnose SMTP issues before sending

---

## RSS Feeds

Go to **Feeds** in the navigation to add RSS or Atom feeds. Each feed has:
- A display name
- A per-feed article limit (how many articles to pull per fetch)
- Active/paused toggle

RSS articles are scored and deduplicated alongside HN articles. They appear in the pool and curator with an `RSS` source badge.

---

## Email Templates

Four built-in templates are included. You can edit any template or create new ones from **Templates** in the navigation. Templates support these placeholders:

| Placeholder | Description |
|---|---|
| `{articles}` | Rendered article list (single column) |
| `{articles_2col}` | Rendered article list (2-column grid) |
| `{date}` | Newsletter date |
| `{unsubscribe_url}` | Per-subscriber unsubscribe link |

Article HTML placeholders: `{number}`, `{title}`, `{url}`, `{hn_url}`, `{summary}`, `{hn_score}`, `{hn_comments}`

---

## Contributing

1. Fork and clone
2. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY`
4. `python run.py` — hot reload is on by default in dev mode
5. Add tests in `tests/`
6. Open a PR against `main`

Key files:
- **`secdigest/db.py`** — all data access; add new queries here
- **`secdigest/fetcher.py`** — HN fetch + Claude curation scoring
- **`secdigest/rss.py`** — RSS/Atom parsing
- **`secdigest/mailer.py`** — email rendering and sending
- **`secdigest/web/routes/`** — one file per feature area
- **`secdigest/web/templates/`** — Jinja2 templates; base layout in `base.html`
