# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run app
python app.py                          # Flask dev server on 0.0.0.0:5000 (debug=True)

# Install dependencies
uv sync                                # preferred
pip install -r requirements.txt        # fallback

# Run tests
pytest tests/                          # full suite
pytest tests/test_basic.py             # smoke tests only
pytest tests/test_models.py            # model unit tests
pytest -k "test_name"                  # single test

# Lint (max-line 127, complexity 10, ignore W293/291/292 — see setup.cfg)
flake8 .

# Docker (local full stack: web + Postgres 15 + nginx)
docker compose up --build
docker build -t frameiq .
docker run -p 5000:8080 -e DATABASE_URL=... frameiq

# Makefile shortcuts (prod ops)
make deploy / make logs / make restart / make clean

# Schema migrations (no Alembic — run manually when column changes needed)
python migrates/migrate_<name>.py
```

## Architecture

**FrameIQ** is a Letterboxd-style social movie/TV platform with an AI chat assistant.

### Stack
- **Backend**: Flask 3.1.2, SQLAlchemy 2.0, PostgreSQL (Neon serverless in prod)
- **AI**: LangGraph multi-agent system, OpenAI (gpt-4.1-mini supervisor/retriever, gpt-5-mini chat)
- **Auth**: Google OAuth 2.0 (Authlib) + Flask-Login + CSRF (Flask-WTF)
- **Frontend**: Jinja2 templates, Tailwind CSS, vanilla JS, Chart.js
- **Infra**: Docker + nginx reverse proxy, GitHub Actions, Render (prod) / Cloud Run (alternative)

### Key Directories
- `routes/` — 28 Flask blueprints, one per feature domain
- `src/agents/` — LangGraph multi-agent AI system
- `src/api/` — Agent service + Flask integration blueprint (`agent_chat`)
- `api/` — TMDb client (`tmdb/` package) + streaming providers
- `migrates/` — Manual migration scripts (no Alembic; `db.create_all()` handles new tables)
- `scripts/` — Data collection, embedding generation, episode sync (not in Docker)
- `utils/` — Shared helpers (currently `email.py`)
- `extensions.py` — Flask extensions singleton (limiter, mail, db)

### Database
- All models in `models.py` (39 KB, single file)
- `db.create_all()` runs on every startup — new tables auto-created
- Column alterations need a manual migration script in `migrates/`
- Key models: `User`, `MediaItem`, `Review`, `DiaryEntry`, `TVShowProgress`, `TVSeasonProgress`, `TVEpisodeWatch`, `UpcomingEpisode`, `UserFollow`, `ActivityFeed`, `CustomList`, `Tag`, `Like`

### AI / Chat System
LangGraph workflow: `START → supervisor_node → [retriever_node | chat_node] → enricher_node → END`
- **supervisor** (gpt-4.1-mini, structured output): routes to `chat`/`retriever` and extracts entities (titles, people, genres, years); heuristic fallback if the LLM fails
- **retriever** (gpt-4.1-mini ReAct agent): 7 cached TMDb tools — discover movies/TV (with keyword vibe search), title lookup, person filmography, trending, similar titles, plus `my_history` (the user's own ratings/diary/watchlist/tracked shows)
- **chat** (gpt-5-mini): general film knowledge and small talk
- **enricher**: extracts titles from the reply (skipped for short messages) and fetches poster cards via TMDb
- Token-level streaming via `astream_events` → SSE from `routes/chat.py` (`/chat_api`, rate-limited 20/min; 100/hour); frontend renders markdown (marked + DOMPurify)
- Conversation memory: SQLite checkpointing at `instance/chat_memory.db` (survives restarts); window trimmed to last 20 messages
- User personalization: profile (ratings, favorite genres, TV tracking, watchlist, streaming progress) built per request in `src/api/agent_service.py`
- Streaming providers for the watch pages: `api/stream_providers.py` (Rive, VidKing, Vidy, 1Embed)

### TV Tracking
Marks a show "completed" only when TMDb reports `status == "Ended"`. Episode/season records in `TVEpisodeWatch` and `TVSeasonProgress`; batch season operations supported.

### Environment Variables
**Strictly required at startup** (app raises `RuntimeError` if missing):
`SECRET_KEY`, `DATABASE_URL`, `TMDB_API_KEY`

**Optional** (features degrade gracefully if missing):
`CLOUDINARY_URL`, `RATELIMIT_STORAGE_URI`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`

**Required for AI features**:
`OPENAI_API_KEY`

**Required for image uploads**:
`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

Copy `.env.example` to `.env` for the full list.

### CI/CD
- `ci-cd.yml` — pytest + flake8 on push to main/develop, auto-deploys to Render
- `sync-upcoming-episodes.yml` — daily cron 2:00 AM UTC via `scripts/sync_upcoming_episodes.py`

### Test Suite
| File | Coverage area |
|------|--------------|
| `test_basic.py` | Smoke — app boots, routes respond |
| `test_auth.py` | Login, register, session |
| `test_models.py` | Model creation and relationships |
| `test_routes.py` | Blueprint endpoint responses |
| `test_social.py` | Follow/unfollow, activity feed |
| `test_feed.py` | Friends activity feed |
| `test_cache.py` | Cache-layer behaviour |
| `test_watch.py` | TV episode tracking |

`tests/conftest.py` sets up SQLite in-memory DB for tests. Note: `connect_timeout` is skipped for SQLite (Postgres-only feature).
