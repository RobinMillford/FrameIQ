# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run app
python app.py           # starts Flask on 0.0.0.0:5000 (debug=True)

# Install dependencies
pip install -r requirements.txt
uv sync

# Run tests
pytest tests/
pytest tests/test_basic.py   # smoke tests only

# Lint
flake8 .

# Docker
docker build -t frameiq .
docker run -p 5000:8080 -e DATABASE_URL=... frameiq

# Run a migration script (when needed for schema changes)
python migrates/migrate_<name>.py
```

## Architecture

**FrameIQ** is a Letterboxd-style social movie/TV platform with an AI chat assistant.

### Stack
- **Backend**: Flask 3.1.2, SQLAlchemy 2.0, PostgreSQL (Neon serverless in prod)
- **AI**: LangGraph multi-agent system, Groq API (Llama models), OpenAI embeddings, ChromaDB vector DB
- **Auth**: Google OAuth 2.0 (Authlib) + Flask-Login
- **Frontend**: Jinja2 templates, Tailwind CSS, vanilla JS, Chart.js
- **Infra**: Docker, GitHub Actions, Render (prod) / Cloud Run (alternative)

### Key Directories
- `routes/` — 17+ Flask blueprints, one per feature domain (auth, reviews, social, tv_tracking, etc.)
- `src/agents/` — LangGraph multi-agent AI system
- `src/api/` — Agent service + Flask integration blueprint
- `api/` — Legacy utilities: TMDb client, ChromaDB, RAG helpers
- `migrates/` — Manual migration scripts (no Alembic; app uses `db.create_all()` on startup)
- `scripts/` — Data collection, embedding generation, episode sync

### Database
- Models live entirely in `models.py` (39KB)
- `db.create_all()` runs on every app startup — handles new tables automatically
- Schema changes requiring column alterations need a manual migration script in `migrates/`
- Key models: `User`, `MediaItem`, `Review`, `DiaryEntry`, `TVShowProgress`, `TVSeasonProgress`, `TVEpisodeWatch`, `UpcomingEpisode`, `UserFollow`, `ActivityFeed`, `CustomList`, `Tag`, `Like`

### AI / Chat System
LangGraph workflow: `START → supervisor_node → [retriever_node | chat_node | enricher_node] → END`
- **supervisor** (Llama 3.1 8B): routes query via function calling
- **retriever**: ChromaDB semantic search + TMDb API lookup (5,722 movies embedded)
- **chat** (Llama 3.3 70B): deep analysis and recommendations
- **enricher**: fetches posters and metadata
- Responses stream to the frontend via Server-Sent Events (SSE)
- Conversation memory persisted via LangGraph checkpointing

### TV Tracking
Smart completion logic: marks a show "completed" only when TMDb reports `status == "Ended"`. Episode/season records are in `TVEpisodeWatch` and `TVSeasonProgress`; batch season operations supported.

### Environment Variables Required
`DATABASE_URL`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `TMDB_API_KEY`, `CHROMA_API_KEY`, `CHROMA_TENANT`, `CHROMA_DATABASE`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SECRET_KEY`, `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

### CI/CD
- `ci-cd.yml` — runs pytest + flake8 on push to main/develop, auto-deploys to Render
- `sync-upcoming-episodes.yml` — daily cron at 2:00 AM UTC (8 AM Bangladesh time)
- `update_movie_embeddings.yml` — monthly ChromaDB refresh
