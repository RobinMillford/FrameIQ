<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/RobinMillford/FrameIQ/main/static/images/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/RobinMillford/FrameIQ/main/static/images/logo-light.svg">
    <img alt="FrameIQ" width="180" src="https://raw.githubusercontent.com/RobinMillford/FrameIQ/main/static/images/logo-dark.svg">
  </picture>
</p>

<h1 align="center">FrameIQ</h1>

<p align="center">
  <strong>A cinematic social movie & TV platform with a multi-agent AI assistant.</strong><br>
  Track, discover, and discuss what you watch — powered by LangGraph, Flask, and TMDb.
</p>

<p align="center">
  <a href="https://frameiq.studio"><img src="https://img.shields.io/badge/Live%20Demo-frameiq.studio-000?style=for-the-badge&logo=vercel&logoColor=F6B73C" alt="Live Demo"></a>
  <a href="https://github.com/RobinMillford/FrameIQ/actions/workflows/ci-cd.yml"><img src="https://img.shields.io/github/actions/workflow/status/RobinMillford/FrameIQ/ci-cd.yml?branch=main&style=for-the-badge&logo=githubactions&logoColor=white" alt="CI/CD"></a>
  <a href="https://github.com/RobinMillford/FrameIQ/blob/main/LICENSE"><img src="https://img.shields.io/github/license/RobinMillford/FrameIQ?style=for-the-badge&color=blue" alt="License"></a>
  <a href="https://python.org/downloads/release/python-3120/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://flask.palletsprojects.com/"><img src="https://img.shields.io/badge/Flask-3.1-000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask"></a>
  <a href="https://langchain-ai.github.io/langgraph/"><img src="https://img.shields.io/badge/LangGraph-multi--agent-412991?style=for-the-badge" alt="LangGraph"></a>
  <a href="https://github.com/RobinMillford/FrameIQ/stargazers"><img src="https://img.shields.io/github/stars/RobinMillford/FrameIQ?style=for-the-badge&logo=github&logoColor=white" alt="Stars"></a>
</p>

---

## 🎬 What is FrameIQ?

FrameIQ is a **Letterboxd-inspired** social platform for film and TV enthusiasts, elevated with a **production-grade multi-agent AI assistant (CineBot)** that understands natural language, recommends by vibe, and streams its reasoning in real-time.

Built for people who care about what they watch — and want an intelligent companion to help them discover more.

| | | |
|:---:|:---:|:---:|
| **Track** everything you watch | **Discover** by mood, genre, era | **Discuss** with friends & community |
| **Watch** inline with resume | **Analyze** your taste DNA | **Chat** with an AI film expert |

---

## ✨ Features

### 📚 Library & Tracking
- **Watchlist** with priority tiers (High / Medium / Low)
- **Diary** — chronological log with dates, ratings, notes
- **Viewed** & **Wishlist** libraries with rich filtering
- **Star ratings** (½–5★) + written reviews with markdown
- **Custom lists** — public, private, collaborative
- **Tags** with autocomplete & trending suggestions

### 📺 TV Show Tracking (First-Class)
- Episode-by-episode progress with timestamps
- **Batch season operations** — mark entire seasons watched
- **Smart completion** — only marks *Completed* when TMDb confirms series ended
- **Upcoming Episodes calendar** — 60-day view, auto-synced daily
- Continue Watching row with resume points

### 👥 Social Layer
- Follow / unfollow with activity feeds (Following / Global / Personal)
- Friends' activity on every title page
- **Popular With Friends** algorithmic ranking
- Review likes, comments, helpful votes
- Suggested follows based on taste overlap

### ▶️ Inline Watching
- Stream movies & episodes directly — no redirects
- **Resume playback** with per-title progress persistence
- Auto-logs diary entry at 85% completion
- Full watch history with timestamps

### 🔍 Discovery & Search
- TMDb-powered filters: genre, year, language, rating, provider
- Trending movies & shows (daily/weekly)
- Entertainment news feed (NewsAPI)
- **Semantic similarity** — "movies that feel like a rainy Sunday"

### 🤖 CineBot — Multi-Agent AI Assistant
<details>
<summary><strong>Architecture: LangGraph pipeline</strong></summary>

```
User message
     │
     ▼
┌─────────────┐
│  Supervisor │ ← zero-LLM heuristic router (saves 2–3 API calls)
└──────┬──────┘
       │
   ┌───┴───┐
   ▼       ▼
┌───────┐ ┌─────┐
│Retriever│ │ Chat│ ← Retriever: 7 TMDb/vector tools
│        │ │     │    Chat: open-ended film knowledge
└───┬───┘ └──┬──┘
    └────┬────┘
         ▼
    ┌──────────┐
    │ Enricher │ ← concurrent TMDb poster & metadata fetch
    └────┬─────┘
         ▼
    SSE stream → browser (markdown + poster cards)
```

| Agent | Role | Model |
|-------|------|-------|
| **Supervisor** | Route + extract entities (titles, people, genres, years) | gpt-4.1-mini (structured) |
| **Retriever** | ReAct agent with 7 cached tools | gpt-4.1-mini |
| **Chat** | General film knowledge, recommendations | gpt-5-mini |
| **Enricher** | Extract titles from reply → fetch posters/meta | gpt-4.1-mini |

</details>

- **Streaming responses** via SSE — see each tool call as it happens
- **Poster & metadata cards** injected inline in replies
- **Conversation memory** persisted (SQLite checkpointing, survives restarts)
- **User personalization** — ratings, favorite genres, TV progress, watchlist injected per request
- **Command palette** (⌘K) + **Slide-over panel** (⌘J) for keyboard-first UX

### 📊 Personal Analytics
- Watch time, genre breakdown, top directors/actors
- Year-in-review with interactive Chart.js visualizations
- Taste DNA — genre affinity bars with gradient fills
- Taste Match badges on every title page

---

## 🏗 Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Flask 3.1, SQLAlchemy 2.0, PostgreSQL 16 |
| **AI / Agents** | LangGraph, LangChain, OpenAI (gpt-4.1-mini / gpt-5-mini) |
| **Auth** | Google OAuth 2.0 (Authlib) + Flask-Login + Flask-WTF CSRF |
| **Frontend** | Jinja2, Tailwind CSS, Vanilla JS (ESM), Chart.js, Lucide icons |
| **Media APIs** | TMDb, Cloudinary (avatars), NewsAPI |
| **Streaming** | Rive, VidKing, Vidy, 1Embed providers |
| **Infra** | Docker Compose, systemd-nginx (reverse proxy), GitHub Actions |
| **Observability** | Langfuse-ready, structured logging |

---

## 🎨 Design System

**Monochrome Marquee** — true black (#000), white type, single amber accent (#F6B73C)

- **Display:** Archivo Expanded (variable width)
- **Body:** Inter
- **Metadata:** JetBrains Mono
- **Motion:** cubic-bezier(0.16, 1, 0.3, 1) — 180ms/320ms
- **Radii:** 4 / 6 / 10px — sharp, broadcast-grade
- **Film grain** overlay + **projector beam/dust** atmosphere

All 49 templates unified on a single design token system. Zero Poppins, zero indigo/violet, zero unused dependencies.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- PostgreSQL 16 (local or Docker)
- API keys (see [Environment Variables](#-environment-variables))

### Local Development

```bash
# 1. Clone & enter
git clone https://github.com/RobinMillford/FrameIQ.git
cd FrameIQ

# 2. Virtual environment
python -m venv .venv && source .venv/bin/activate

# 3. Dependencies (uv recommended)
uv sync
# or: pip install -r requirements.txt

# 4. Environment
cp .env.example .env
# Edit .env with your keys

# 5. Run
python app.py          # http://localhost:5000
```

### Run Tests

```bash
pytest tests/ -v        # 104 tests, ~30s
uv run flake8 .         # lint (max-line 127, complexity 10)
```

### Docker (Production Stack)

```bash
cp .env.example .env    # production values
make deploy             # clean build + start
```

Stack: **web** (Gunicorn 4 workers) + **db** (Postgres 16) + **system nginx** (80/443)

```bash
make logs      # tail web logs
make restart   # zero-downtime restart
make ps        # container status
make clean     # wipe everything including DB ⚠️
```

---

## ⚙️ Environment Variables

> Copy `.env.example` → `.env` and fill in.

| Variable | Required | Description |
|----------|:--------:|-------------|
| `SECRET_KEY` | ✅ | Flask session secret (32+ chars) |
| `DATABASE_URL` | ✅ | `postgresql://user:pass@host:5432/db` |
| `TMDB_API_KEY` | ✅ | [TMDb API](https://www.themoviedb.org/settings/api) |
| `OPENAI_API_KEY` | ✅ | OpenAI API key (chat + embeddings) |
| `GOOGLE_CLIENT_ID` | ✅ | Google OAuth 2.0 |
| `GOOGLE_CLIENT_SECRET` | ✅ | Google OAuth 2.0 |
| `CLOUDINARY_CLOUD_NAME` | ✅ | Avatar uploads |
| `CLOUDINARY_API_KEY` | ✅ | Cloudinary |
| `CLOUDINARY_API_SECRET` | ✅ | Cloudinary |
| `NEWS_API_KEY` | ⭕ | Entertainment news feed |
| `MAIL_SERVER` / `MAIL_*` | ⭕ | Password reset emails |
| `RATELIMIT_STORAGE_URI` | ⭕ | Redis for rate limiting (defaults to memory) |

---

## 📁 Project Structure

```
FrameIQ/
├── app.py                      # Application factory (create_app)
├── models.py                   # 39 KB — all SQLAlchemy models
├── extensions.py               # Flask extensions (limiter, mail, db)
├── requirements.txt
│
├── routes/                     # 28 Flask blueprints (one per domain)
│   ├── auth.py                 # Login, register, password reset
│   ├── main.py                 # Home, search, news, tonights-pick
│   ├── details.py              # Movie / TV detail pages
│   ├── reviews.py              # Reviews & ratings
│   ├── diary.py                # Watch diary
│   ├── lists.py                # Custom lists (basic + advanced)
│   ├── tv_tracking.py          # Episode & season tracking
│   ├── chat.py                 # SSE streaming chat
│   ├── social.py               # Follow, activity feed
│   ├── stats.py                # Dashboard, year-in-review
│   └── ...
│
├── src/
│   ├── agents/                 # LangGraph multi-agent system
│   │   ├── graph.py            # StateGraph definition
│   │   ├── nodes.py            # Supervisor, Retriever, Chat, Enricher
│   │   ├── tools.py            # 7 LangChain tools (cached TMDb + history)
│   │   └── state.py            # GraphState schema
│   └── api/
│       ├── agent_service.py    # User context building, SSE streaming
│       └── flask_integration.py
│
├── api/                        # Shared utilities
│   ├── tmdb_client.py          # TMDb wrapper (compat shim)
│   ├── tmdb/                   # TMDb package: cache, movies, tv, people, search
│   ├── stream_providers.py     # Embed providers (Rive, VidKing, Vidy, 1Embed)
│   └── chatbot.py              # LLM helpers
│
├── templates/                  # 49 Jinja2 templates (unified on base.html)
├── static/
│   ├── css/                    # tokens.css, chrome.css, detail.css, projector.css
│   ├── js/                     # chat-page.js, chrome.js, projector-dust.js
│   └── images/
│
├── scripts/                    # Ops (excluded from Docker)
│   ├── sync_upcoming_episodes.py
│   └── collect_media.py
│
├── migrates/                   # Manual schema migrations
├── .github/workflows/          # ci-cd.yml, deploy.yml, sync-upcoming-episodes.yml
├── nginx/nginx.conf            # Reverse proxy + SSL
├── docker-compose.yml
├── Dockerfile
└── Makefile
```

---

## 🔄 CI/CD Pipeline

| Workflow | Trigger | Actions |
|----------|---------|---------|
| **ci-cd.yml** | Push to `main`/`develop` | pytest (104) + flake8 |
| **deploy.yml** | Push to `main` (after CI pass) | SSH → VPS → `make deploy` |
| **sync-upcoming-episodes.yml** | Daily 02:00 UTC | Sync TMDb → PostgreSQL |

**VPS:** Self-hosted PostgreSQL 16, Docker Compose, system nginx, nightly `pg_dump` backups, certbot SSL.

---

## 🧪 Testing & Quality

```bash
# Full suite (104 tests)
pytest tests/

# Smoke only
pytest tests/test_basic.py

# Models
pytest tests/test_models.py

# Single test
pytest -k "test_name"
```

**Quality gates:** 104 tests passing • flake8 clean • vulture clean • 0 secrets • 0 dead code

---

## 🤝 Contributing

We welcome contributions! Please read [CLA.md](CLA.md) before submitting a PR.

**Good first issues:**
- 🌍 Add more languages/regions to TMDb discover tools
- 🧪 Write missing unit tests in `tests/`
- ♿ Improve accessibility (ARIA, keyboard nav)
- 📈 Add chart types to stats dashboard
- 🎨 Extend design token system

```bash
# 1. Fork & clone
git clone https://github.com/YOUR-USERNAME/FrameIQ.git

# 2. Branch
git checkout -b feat/your-feature

# 3. Develop
# ... make changes ...

# 4. Verify
pytest tests/ && uv run flake8 .

# 5. Push & PR
git push origin feat/your-feature
# Open PR against main
```

---

## 📄 License

Licensed under **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).

> **Commercial use, SaaS hosting, or white-labelling requires a separate licence.**  
> Contact: `robinmill4d@gmail.com`

---

## 🙏 Acknowledgements

- **[TMDb](https://www.themoviedb.org)** — The movie database powering our data
- **[LangGraph](https://langchain-ai.github.io/langgraph/)** — Multi-agent orchestration
- **[Letterboxd](https://letterboxd.com)** — Product inspiration
- **[Archivo](https://github.com/Omnibus-Type/Archivo)** • **[Inter](https://github.com/rsms/inter)** • **[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono)** — Typefaces
- **[Tailwind CSS](https://tailwindcss.com)** • **[Chart.js](https://www.chartjs.org/)** • **[Lucide](https://lucide.dev/)** — Frontend toolkit

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/RobinMillford">Robin Millford</a> and contributors.</sub><br>
  <sub>Star ⭐ the repo if you find it useful — it helps more people discover FrameIQ.</sub>
</p>