# SecDigest

Daily security newsletter. Pulls the top stories from Hacker News, scores them for
security relevance using Claude, generates one-paragraph summaries, and serves them
in a password-protected web UI where you can edit, reorder, and send to a subscriber list.

---

## Requirements

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/) (Claude Haiku — cheap, ~$0.01/day)
- SMTP credentials for email sending (optional — you can use the UI without sending)

---

## Quick Start

```bash
# 1. Clone and enter
git clone https://github.com/brandy-savage/secdigest
cd secdigest

# 2. Create virtualenv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env and set at minimum:
#   SECRET_KEY=<random string>
#   ANTHROPIC_API_KEY=sk-ant-...

# 4. Run
source .env
python run.py
```

Open http://localhost:8080 — default password is **`secdigest`** (change it in Settings).

---

## Configuration

All settings can be set via `.env` at startup or changed live on the `/settings` page
(stored in SQLite). The `.env` values only apply on first run for DB-backed keys.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me` | Session signing key — set a random string in prod |
| `ANTHROPIC_API_KEY` | — | Required for fetch + summarize |
| `PASSWORD_HASH` | auto | bcrypt hash of the login password. Generated automatically from `secdigest` on first run. Override: `python -c "from passlib.context import CryptContext; print(CryptContext(['bcrypt']).hash('yourpassword'))"` |
| `SMTP_HOST` | — | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | — | SMTP login username |
| `SMTP_PASS` | — | SMTP password or app password |
| `SMTP_FROM` | — | e.g. `SecDigest <you@example.com>` |
| `FETCH_TIME` | `07:00` | 24h local time for the daily auto-fetch |
| `HN_MIN_SCORE` | `50` | Minimum HN points for a story to be considered |
| `MAX_ARTICLES` | `15` | Max articles to include per newsletter |

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
User=cumman
WorkingDirectory=/opt/secdigest
EnvironmentFile=/opt/secdigest/.env
ExecStart=/opt/secdigest/.venv/bin/uvicorn secdigest.web.app:app --host 0.0.0.0 --port 8080
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
    }
}
```

---

## Project Structure

```
secdigest/
├── run.py                      # Dev entry point (python run.py)
├── requirements.txt
├── .env.example
├── docs/
│   └── PLAN.md                 # Architecture decisions and design notes
├── tests/                      # Add tests here
├── secdigest/                  # Main Python package
│   ├── config.py               # Env config and defaults
│   ├── db.py                   # All SQLite operations
│   ├── fetcher.py              # HN API + Claude curation scoring
│   ├── summarizer.py           # Claude per-article summaries
│   ├── mailer.py               # SMTP HTML email sending
│   ├── scheduler.py            # APScheduler daily job
│   └── web/
│       ├── app.py              # FastAPI factory + auth routes
│       ├── auth.py             # Password helpers + session check
│       ├── routes/
│       │   ├── newsletter.py   # /, /archive, /day/{date}, article actions
│       │   ├── prompts.py      # /prompts — curation prompt CRUD
│       │   ├── subscribers.py  # /subscribers — email list management
│       │   └── settings.py     # /settings — config + LLM audit log
│       ├── templates/          # Jinja2 HTML templates
│       └── static/             # CSS + JS
```

---

## How It Works

1. **Daily fetch** (auto at configured time, or manual via UI):
   - Pulls top 200 HN stories, filters by minimum score
   - Sends batches of 25 to Claude Haiku with user-defined curation prompts
   - Scores each 0–10 for security relevance; keeps articles scoring ≥ 5
   - Stores top `MAX_ARTICLES` ranked by `relevance × √hn_score`

2. **Summarization** (triggered after fetch, or manually per article):
   - Claude Haiku generates a 2–3 sentence technical summary per article
   - System prompt is cache-controlled (prompt caching) for cost efficiency

3. **Web UI**:
   - Edit summaries inline, exclude/include articles, drag to reorder
   - Archive view for browsing any past day
   - Prompts page to tune curation and summary behavior

4. **Email** (manual send button, or auto-send after daily fetch):
   - HTML + plaintext multipart email to all active subscribers
   - Configure SMTP on the Settings page

---

## Contributing

1. Fork and clone
2. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY`
4. `python run.py` — hot reload is on by default in dev mode
5. Add tests in `tests/` (pytest)
6. Open a PR against `main`

Key files to know:
- **`secdigest/db.py`** — all data access; if you need a new query, add it here
- **`secdigest/fetcher.py`** — HN fetch + Claude curation; tweak scoring logic here
- **`secdigest/web/routes/`** — one file per feature area; add new routes here
- **`secdigest/web/templates/`** — Jinja2 templates; base layout in `base.html`
