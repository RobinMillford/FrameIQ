# FrameIQ

**A social movie & TV platform with an AI chat assistant — inspired by Letterboxd, powered by LangGraph.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.1-black?logo=flask)](https://flask.palletsprojects.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-blueviolet)](https://langchain-ai.github.io/langgraph/)
[![LLM](https://img.shields.io/badge/LLM-compatible-412991)](https://github.com/RobinMillford/FrameIQ)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)

**Live →** [frameiq.studio](https://frameiq.studio)

---

## What is FrameIQ?

FrameIQ lets you track, rate, and review everything you watch — movies, TV shows, anime — and share it with a social community. On top of the standard Letterboxd-style features, it ships a multi-agent AI assistant (CineBot) that answers natural-language questions, recommends titles by vibe, and streams its thinking in real time.

---

## Features

### Tracking & Library
- **Watchlist** with High / Medium / Low priority labels
- **Diary** — chronological log of everything you've watched with dates
- **Viewed** and **Wishlist** libraries
- **Star ratings** (0.5 – 5.0 in half-star increments) and written reviews
- **Custom lists** — public or private, shareable
- **Tags** with autocomplete and trending suggestions

### TV Show Tracking
- Episode-by-episode progress with timestamps
- Season-level batch marking ("Mark entire season watched")
- Smart completion: a show only moves to *Completed* when TMDb confirms the series has ended — returning shows stay in *Watching* even when you're caught up
- Auto-refresh episode counts when new seasons drop
- **Upcoming Episodes calendar** — 60-day view, grouped by date, synced daily via GitHub Actions

### Social
- Follow / unfollow users
- Activity feed — Following, Global, and Personal tabs
- Friends' activity on individual movie and show pages
- Popular With Friends ranking
- Review likes, comments, and helpful votes
- User discovery with suggested follows

### Watch
- Stream movies and TV episodes directly in the browser — no redirects
- Resume playback from where you left off (progress saved per title)
- Auto-logs a diary entry once you've watched ≥ 85 % of a title
- Continue Watching row on your dashboard
- Full watch history with timestamps

### Discovery & Search
- Genre, year, language, and rating filters via TMDb Discover
- Trending movies and shows
- Entertainment news feed (NewsAPI)
- Semantic similarity search — "movies that feel like a rainy Sunday"

### AI Chat — CineBot
- Multi-agent LangGraph pipeline: **Supervisor → Retriever / Chat → Enricher**
- Zero-LLM heuristic supervisor (saves 2–3 API calls per request)
- **7 tools:** vector DB search, TMDb title lookup, person filmography, movie discover, TV discover, similar movies, trending
- Streaming responses via SSE — user sees each tool call as it happens
- Poster and metadata cards injected into replies
- Conversation memory persisted across turns (LangGraph checkpointing)

### Stats & Analytics
- Personal dashboard: watch time, genre breakdown, top directors, yearly review
- Interactive Chart.js visualisations

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Flask 3.1, SQLAlchemy 2.0, PostgreSQL |
| **AI / Agents** | LangGraph 0.2, LangChain, configurable LLM provider |
| **Vector search** | ChromaDB Cloud, OpenAI-compatible text embeddings (5 700+ movies) |
| **Auth** | Google OAuth 2.0 (Authlib) + Flask-Login |
| **Frontend** | Jinja2, Tailwind CSS, Vanilla JS, Chart.js |
| **Media APIs** | TMDb, Cloudinary (avatars), NewsAPI |
| **Infra** | Docker, Nginx, GitHub Actions, VPS |

---

## Architecture

```
User message
     │
     ▼
 Supervisor          ← zero-LLM heuristic router
  │        │
  ▼        ▼
Retriever  Chat      ← Retriever uses 7 TMDb / vector tools
  │        │           Chat uses a capable LLM for open questions
  └───┬────┘
      ▼
  Enricher           ← concurrent TMDb poster & metadata fetch
      │
      ▼
 SSE stream → browser
```

| Agent | Role | Recommended capability |
|---|---|---|
| Retriever | Structured tool calling, TMDb queries | Fast model with reliable function-calling |
| Chat | Open-ended film knowledge & recommendations | High-quality reasoning model |
| Enricher | Title extraction from AI reply | Lightweight / cheap model |

> The specific models used in production are not disclosed. Any provider compatible with the OpenAI Chat Completions API (OpenAI, Azure OpenAI, Groq, Together, Ollama, etc.) will work — set your chosen model names and `OPENAI_API_KEY`-equivalent in `.env`.

---

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL (local or [Neon](https://neon.tech) serverless)
- API keys — see [Environment Variables](#environment-variables)

### Local setup

```bash
git clone https://github.com/RobinMillford/FrameIQ.git
cd FrameIQ

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in your keys
python app.py          # http://localhost:5000
```

### Run tests

```bash
pytest tests/
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in every value.

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask session secret |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `TMDB_API_KEY` | ✅ | [themoviedb.org](https://www.themoviedb.org/settings/api) |
| `OPENAI_API_KEY` | ✅ | API key for your chosen LLM provider (must be OpenAI-API-compatible) |
| `CHROMA_API_KEY` | ✅ | ChromaDB Cloud |
| `CHROMA_TENANT` | ✅ | ChromaDB Cloud tenant |
| `CHROMA_DATABASE` | ✅ | ChromaDB Cloud database |
| `GOOGLE_CLIENT_ID` | ✅ | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | ✅ | Google OAuth |
| `CLOUDINARY_CLOUD_NAME` | ✅ | Avatar uploads |
| `CLOUDINARY_API_KEY` | ✅ | Cloudinary |
| `CLOUDINARY_API_SECRET` | ✅ | Cloudinary |
| `NEWS_API_KEY` | optional | Entertainment news feed |
| `MAIL_SERVER` etc. | optional | Password reset emails |

---

## Docker Deployment (VPS)

```bash
cp .env.example .env   # fill in production values
make deploy            # clean build + start (no cache)
```

Three containers start: **web** (Gunicorn), **db** (Postgres 15), **nginx** (ports 80 / 443).

```bash
make logs      # tail web container logs
make restart   # stop + start without rebuild
make ps        # show running containers
make clean     # wipe everything including the DB volume ⚠️
```

### SSL — Let's Encrypt

```bash
ufw allow 80 && ufw allow 443
docker compose stop nginx
certbot certonly --standalone -d frameiq.studio -d www.frameiq.studio
cp /etc/letsencrypt/live/frameiq.studio/fullchain.pem nginx/ssl/
cp /etc/letsencrypt/live/frameiq.studio/privkey.pem   nginx/ssl/
# uncomment the HTTPS server block in nginx/nginx.conf, then:
docker compose up -d nginx
```

---

## Project Structure

```
FrameIQ/
├── app.py                      # Application factory (create_app)
├── models.py                   # All SQLAlchemy models
├── extensions.py               # Flask extensions (limiter, mail)
├── requirements.txt
│
├── routes/                     # Flask blueprints — one per feature domain
│   ├── auth.py                 # Login, register, password reset
│   ├── main.py                 # Home, search, news
│   ├── details.py              # Movie / TV detail pages
│   ├── reviews.py              # Reviews & ratings
│   ├── diary.py                # Watch diary
│   ├── lists.py / lists_advanced.py
│   ├── tv_tracking.py          # Episode & season tracking
│   ├── chat.py                 # SSE streaming chat
│   └── ...                     # social, stats, trending, analytics, etc.
│
├── src/
│   ├── agents/                 # LangGraph multi-agent system
│   │   ├── graph.py            # StateGraph definition
│   │   ├── nodes.py            # Supervisor, Retriever, Chat, Enricher
│   │   ├── tools.py            # 7 LangChain tools (TMDb + ChromaDB)
│   │   └── state.py            # GraphState schema
│   └── api/
│       ├── agent_service.py
│       └── flask_integration.py
│
├── api/                        # Shared utilities
│   ├── tmdb_client.py          # TMDb API wrapper
│   ├── vector_db.py            # ChromaDB interface
│   └── chatbot.py              # LLM helpers
│
├── templates/                  # Jinja2 templates
├── static/                     # CSS, JS, images
│
├── scripts/                    # Ops scripts (excluded from Docker image)
│   ├── sync_upcoming_episodes.py
│   ├── collect_media.py
│   └── generate_embeddings.py
│
├── .github/workflows/          # CI/CD, episode sync, embedding refresh
├── nginx/nginx.conf            # Reverse proxy config
├── docker-compose.yml
├── Dockerfile
└── Makefile
```

---

## Contributing

Contributions are welcome. Please read [CLA.md](CLA.md) before submitting a pull request — by opening a PR you agree to the terms of the Contributor License Agreement, which lets FrameIQ offer a commercial licence alongside AGPL v3.

**Good first issues:**
- Add more languages/regions to the TMDb discover tool
- Write missing unit tests in `tests/`
- Improve accessibility (ARIA labels, keyboard navigation)
- Add more chart types to the stats dashboard

```bash
# fork → clone → branch
git checkout -b feat/your-feature

# make changes
pytest tests/ && flake8 .

git push origin feat/your-feature
# open a pull request against main
```

---

## License

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).

> Commercial use, SaaS hosting, or white-labelling requires a separate licence.  
> Contact: robinmill4d@gmail.com

---

## Acknowledgements

[TMDb](https://www.themoviedb.org) · [LangGraph](https://langchain-ai.github.io/langgraph/) · [ChromaDB](https://www.trychroma.com) · [Letterboxd](https://letterboxd.com) (inspiration)
