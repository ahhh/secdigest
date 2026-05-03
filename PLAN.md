# SecDigest — Design Plan

## What It Does
Daily security newsletter that:
1. Pulls HN top stories, filters by score threshold
2. Claude scores each for security relevance using user-defined prompts
3. Generates 1-paragraph summaries for top articles
4. Presents in a password-protected web UI for editing/organizing
5. Sends to a subscriber list (manual or auto, daily schedule)

## Wiki References
- `agents.md` — harness outside LLM: Claude only sees text input, never touches state
- `audit-and-accountability.md` — every LLM call logged to llm_audit_log
- `least-privilege.md` — Claude has no tools; structured JSON output only
- `spec-driven-development.md` — user-defined prompts are structural inputs, not behavioral overrides

## Architecture

```
HN API → fetcher.py → Claude (curation scoring)
                    ↓
              db: articles
                    ↓
         summarizer.py → Claude (summaries)
                    ↓
              db: newsletters
                    ↓
           FastAPI web UI ← user edits
                    ↓
              mailer.py → SMTP → subscribers
```

## Key Design Decisions
- SQLite + WAL mode (consistent with project patterns)
- Prompts stored in DB, editable via UI — never hardcoded
- All LLM calls logged: model, tokens, cached tokens, result
- No LLM tools — Claude receives text, returns structured JSON
- Prompt caching on system prompts (cost reduction for daily runs)
- Single-password session auth (bcrypt hash in config_kv table)

## DB Tables
- `newsletters` — one per date, status: draft|published|sent
- `articles` — HN items linked to newsletter, with relevance + summary
- `prompts` — user-defined curation and summary prompts
- `subscribers` — email send list
- `llm_audit_log` — every Claude API call with tokens
- `config_kv` — runtime config (SMTP, schedule, password hash, etc.)

## File Layout
```
/opt/secdigest/
├── config.py       — env + DB config loading
├── db.py           — all SQLite operations
├── fetcher.py      — HN API + Claude curation
├── summarizer.py   — Claude per-article summaries
├── mailer.py       — SMTP HTML email sending
├── scheduler.py    — APScheduler daily job
├── main.py         — FastAPI app + all routes
├── templates/      — Jinja2 HTML templates
└── static/         — CSS + minimal JS (HTMX via CDN)
```
