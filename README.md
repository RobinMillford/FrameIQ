# 🎬 FrameIQ - Letterboxd-Style Social Platform with AI

A comprehensive movie and TV show social platform inspired by Letterboxd, featuring LangGraph multi-agent AI system, OpenAI embeddings for semantic search, and complete social discovery features.

![FrameIQ Interface](images/FrameIQ-Intelligent-Entertainment-Discovery.jpg)

![FrameIQ Architecture](images/Gemini_Generated_Image_xoiv4uxoiv4uxoiv.png)

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)
![LangGraph](https://img.shields.io/badge/LangGraph-Latest-purple.svg)
![OpenAI](https://img.shields.io/badge/OpenAI-Embeddings-orange.svg)
![License](https://img.shields.io/badge/License-Apache%202.0-yellow.svg)

**Live Demo**: [FrameIQ on Render](https://tv-movie-recommendations-with-ai.onrender.com)

---

## Feature Comparison with Letterboxd

| Feature | Letterboxd | FrameIQ |
|---------|-----------|---------|
| **Reviews & Ratings** | ✅ 0.5-5 stars | ✅ 0.5-5 stars + titles |
| **Film Diary** | ✅ Movies only | ✅ Movies + TV shows |
| **Rewatch Tracking** | ✅ Manual | ✅ Auto-detect + manual |
| **Custom Lists** | ✅ Basic | ✅ With collaborators |
| **Watchlist** | ✅ Basic | ✅ High/Medium/Low priorities |
| **Tags** | ✅ Basic | ✅ Autocomplete + trending |
| **Likes/Hearts** | ✅ Basic | ✅ Animated + real-time |
| **Comments** | ✅ Basic | ✅ Edit/delete + 5000 chars |
| **Following System** | ✅ Yes | ✅ Yes |
| **Activity Feed** | ✅ Single feed | ✅ Multi-tab (Following/Global/Personal) |
| **User Profiles** | ✅ Basic | ✅ Stats dashboard with charts |
| **Trending** | ✅ Basic | ✅ Advanced algorithms |
| **Review Helpful Votes** | ✅ Yes | ✅ Yes |
| **Friends Activity** | ✅ Basic | ✅ Per-movie/show pages |
| **Year in Review** | ✅ Basic | ✅ Interactive Chart.js |
| **Badges/Achievements** | ✅ Patron only | ✅ Free for all |
| **TV Shows** | ❌ | ✅ Full support |
| **Anime Detection** | ❌ | ✅ Auto-categorize |
| **AI Chat Assistant** | ❌ | ✅ Natural language queries |
| **Semantic Search** | ❌ | ✅ Vector database (5,722 movies) |
| **OpenAI Embeddings** | ❌ | ✅ Superior search quality |
| **Multi-Agent System** | ❌ | ✅ LangGraph orchestration |
| **Episode Tracking** | ❌ | ✅ Track progress per episode |
| **Season Progress** | ❌ | ✅ Visual season completion |
| **Episode Calendar** | ❌ | ✅ Upcoming episodes calendar |
| **Batch Mark Episodes** | ❌ | ✅ Mark entire seasons watched |
| **Smart Completion** | ❌ | ✅ Detects returning series vs ended |
| **Auto Episode Refresh** | ❌ | ✅ Updates when new seasons release |

### What FrameIQ Offers Beyond Letterboxd

**Complete TV Show Support**
- Full integration for TV shows, seasons, and episodes
- Anime auto-detection for both movies and series
- Creator, network, and cast information
- Real-time sync with TMDb for latest episode data

**Advanced Episode Tracking System (Trakt.tv-Style)**
- **Episode-by-Episode Tracking**: Mark individual episodes as watched with timestamps
- **Smart Completion Logic**: Automatically detects if shows are "Ended" vs "Returning Series"
  - Shows stay in "watching" status even when all current episodes are watched (if series is returning)
  - Only marks as "completed" when TMDb confirms series has ended
  - Auto-reverts from "completed" to "watching" when new seasons are released
- **Auto-Refresh Episode Counts**: System checks TMDb on every page load for new episodes
  - Updates total episode count automatically when new seasons drop
  - Maintains accurate progress percentages
- **Visual Progress Tracking**: 
  - Per-show progress bars showing completion percentage
  - Per-season completion indicators
  - Episode-level watch status with visual checkmarks
- **Multi-Status System**: 
  - Watching (currently watching)
  - Completed (finished, series ended)
  - Plan to Watch (in watchlist)
  - On Hold (paused)
  - Dropped (discontinued)
- **Batch Operations**: 
  - Mark entire seasons as watched with one click
  - Bulk episode management
- **Upcoming Episodes Calendar**:
  - Syncs upcoming episodes for next 60 days from tracked shows
  - Displays episode stills and air dates
  - Groups episodes by date with countdown timers
  - Shows which episodes are already watched
  - Filters based on show tracking status
- **Interactive TV Dashboard**:
  - Quick stats overview (total shows, episodes watched, completion %)
  - Tabbed interface: Overview, Watching, Completed, Plan to Watch, Upcoming
  - Continue watching section with recent episodes
  - Most watched shows ranking
  - Calendar view with 7-day upcoming preview

**AI-Powered Features**
- Natural language chat interface with LangGraph agents
- Semantic search using ChromaDB and OpenAI embeddings
- Intelligent movie recommendations based on themes and vibes
- Real-time streaming responses with progress updates

**Enhanced Social Features**
- Priority-based watchlist organization (High/Medium/Low)
- Multi-tab activity feeds (Following/Global/Personal)
- Interactive statistics dashboard with Chart.js visualizations
- Advanced trending algorithms based on activity scores
- Per-movie friends activity sections

**All Letterboxd Core Features**
- Star ratings (0.5-5.0 in 0.5 increments)
- Written reviews with optional titles
- Custom lists with public/private options
- Film diary with viewing dates
- Tags system with autocomplete
- Following/followers functionality
- Activity feed with filters
- User profiles with statistics
- Review likes and comments
- Helpful vote system

---

## Architecture

### LangGraph Agent Workflow

```
User Query
    ↓
Supervisor (Llama 3.1 8B) → Routes to appropriate agent
    ↓
    ├─→ Retriever → ChromaDB vector search + TMDb API
    ├─→ Chat Agent → Deep analysis with Llama 3.3 70B
    └─→ Enricher → Fetches posters and metadata
```

### Technology Stack

**Backend**
- Flask 3.0+ - Web framework
- LangGraph - Multi-agent orchestration
- LangChain - LLM integration
- SQLAlchemy + PostgreSQL (Neon) - Data persistence
- ChromaDB Cloud - Vector database for semantic search
- OpenAI API - text-embedding-3-small for embeddings
- Groq API - Fast LLM inference (Llama models)

**Frontend**
- HTML5 + Tailwind CSS - Responsive UI
- Vanilla JavaScript - Interactive features
- Chart.js - Statistics visualizations
- Server-Sent Events (SSE) - Real-time streaming

**APIs & Services**
- TMDb API - Movie/TV data and trending content
- Google OAuth 2.0 - User authentication
- Cloudinary - Avatar image hosting
- News API - Entertainment news

---
Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL database (or Neon serverless)
- Required API keys (see .env setup below)

### Installation

1. Clone the repository
```bash
git clone https://github.com/RobinMillford/FrameIQ.git
cd FrameIQ
```

2. Install dependencies
```bash
pip install -r requirements.txt
```

3. Configure environment variables

Create `.env` file:
```env
# Database
DATABASE_URL=postgresql://user:password@host/database

# AI & Search
GROQ_API_KEY=your_groq_api_key
OPENAI_API_KEY=your_openai_api_key
CHROMA_API_KEY=your_chroma_api_key
CHROMA_TENANT=your_tenant_id
CHROMA_DATABASE=your_database_name

# External APIs
TMDB_API_KEY=your_tmdb_api_key
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
CLOUDINARY_CLOUD_NAME=your_cloudinary_name
CLOUDINARY_API_KEY=your_cloudinary_key
CLOUDINARY_API_SECRET=your_cloudinary_secret

# Flask
SECRET_KEY=your_secret_key
```

4. Run the application
```bash
python app.py
# or
uv rush
python app.py
```

Visit `http://localhost:5000`

---

##  TV Tracking System

### Core Features

**Episode-by-Episode Tracking**
- Mark individual episodes as watched with viewing timestamps
- Track rewatch counts for favorite episodes
- Episode-level notes and ratings
- Visual checkmarks for watched episodes
- Responsive grid layout for all episodes in a season

**Smart Series Management**
- **Intelligent Completion Detection**:
  - Queries TMDb API to check if series is "Ended", "Canceled", or "Returning Series"
  - Only marks show as "completed" if officially ended
  - Keeps status as "watching" for returning series (even with all current episodes watched)
  - Prevents shows from disappearing from upcoming episodes calendar
- **Auto-Refresh System**:
  - Checks TMDb for updated episode counts on every show page load
  - Automatically updates total episodes when new seasons release
  - Auto-reverts "completed" status back to "watching" when new episodes detected
  - Maintains accurate progress percentages dynamically
- **Progress Tracking**:
  - Real-time progress bars (episodes watched / total episodes)
  - Per-season completion indicators
  - Overall show completion percentage
  - Visual season grid with completion status

**Upcoming Episodes Calendar**
- Automated sync via `scripts/sync_upcoming_episodes.py`
- Fetches episodes airing in next 60 days for all tracked shows
- Displays episode stills, descriptions, and air dates
- Groups by date with "Today", "Tomorrow", "X days" labels
- Shows watched status for each upcoming episode
- Only includes shows with status "watching" or "plan_to_watch"
- Auto-cleanup of past episodes

**TV Dashboard** (`/tv/dashboard`)
- **Overview Tab**: Continue watching, recently added, most watched shows
- **Status Tabs**: Separate views for Watching, Completed, Plan to Watch
- **Calendar Tab**: 7-day upcoming preview with episode cards
- **Quick Stats**: Total shows tracked, episodes watched, completion percentage
- **Quick Links**: My Shows, Full Calendar, Browse TV Shows

### Implementation Details

**Database Models**
- `TVShowProgress`: Tracks user's overall progress per show
  - Fields: user_id, show_id, status, watched_episodes, total_episodes, total_seasons
  - Status values: watching, completed, plan_to_watch, on_hold, dropped
- `TVSeasonProgress`: Tracks completion per season
  - Fields: progress_id, season_number, episodes_watched, total_episodes
- `TVEpisodeWatch`: Records individual episode views
  - Fields: progress_id, season_number, episode_number, watched_date, rewatch_count
- `UpcomingEpisode`: Stores upcoming episodes from TMDb
  - Fields: show_id, show_name, season_number, episode_number, air_date, still_path, poster_path, episode_overview

**Key Routes** (`routes/tv_tracking.py`)
- `/tv/dashboard` - Main TV tracking dashboard
- `/tv/my-shows` - All tracked shows list
- `/tv/upcoming` - Full upcoming episodes calendar
- `/tv/<show_id>` - Show detail with tracking controls
- `/tv/<show_id>/season/<season_number>` - Season detail with episode list
- `/api/tv/track` - Start tracking a show
- `/api/tv/mark-episode` - Mark episode as watched
- `/api/tv/mark-season` - Mark entire season as watched
- `/api/tv/upcoming-episodes` - Fetch upcoming episodes for User

**GitHub Actions Setup**:
The project includes automated CI/CD workflows:

1. **Episode Sync** (`.github/workflows/sync-upcoming-episodes.yml`)
   - Runs daily at 8:00 AM Bangladesh Time (2:00 AM UTC)
   - Can be triggered manually from GitHub UI
   - Syncs upcoming episodes for next 60 days
   - Creates GitHub issue on failure
   - Requires secrets: `DATABASE_URL`, `TMDB_API_KEY`, `SECRET_KEY`

2. **CI/CD Pipeline** (`.github/workflows/ci-cd.yml`)
   - Runs on push to main/develop branches
   - Tests with Python 3.11 and 3.12
   - Linting with flake8
   - Code coverage reports
   - Auto-deploys to Render on main branch
   - Triggers episode sync after deployment
   - Security scanning with Trivy

### User Experience Flow

1. **Discover & Track**: User browses TV shows → clicks "Start Tracking"
2. **Set Status**: Choose "Watching", "Plan to Watch", etc.
3. **Mark Progress**: 
   - Click episodes individually to mark as watched
   - Or mark entire seasons with "Mark Season Watched" button
   - Progress bars update in real-time
4. **Smart Completion**: 
   - When all current episodes watched → System checks TMDb status
   - If "Returning Series" → Stays in "watching" status
   - If "Ended" → Marks as "completed"
5. **Auto-Update**: 
   - New season releases → Episode count refreshes on page load
   - Status auto-reverts from "completed" to "watching"
   - New episodes appear in upcoming calendar
6. **Stay Updated**: 
   - Check dashboard's "Upcoming" tab for next 7 days
   - Visit full calendar for 60-day view
   - Episodes show countdown timers ("3 days", "Tomorrow", "Today")

---

##�🚀 Deployment

### Production (Render)

**Deployment Size**: 280 KB compressed (optimized from 7.3 GB)

**Key Optimizations**:
- OpenAI API for embeddings instead of local models
- ChromaDB Cloud for vector database
- Removed sentence-transformers (saved 1.5 GB)
- Enhanced .dockerignore for minimal builds

**Environment Variables** (set in Render dashboard):
```
DATABASE_URL, GROQ_API_KEY, OPENAI_API_KEY, TMDB_API_KEY,
CHROMA_API_KEY, CHROMA_TENANT, CHROMA_DATABASE,
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, SECRET_KEY,
CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
```

**Automated Updates**: GitHub Actions runs monthly to fetch new movies and update embeddings

### Local Development

```bash
python app.py
# or with uv for faster startup
uv run app.py
```

### Model Configuration

| Agent          | Model                   | Purpose                         |
| -------------- | ----------------------- | ------------------------------- |
| **Supervisor** | Llama 3.1 8B Instant    | Fast routing decisions          |
| **Retriever**  | Llama 3.1 8B Instant    | Quick tool execution            |
| **Chat**       | Llama 3.3 70B Versatile | Deep analysis & recommendations |
| **Enricher**   | Llama 3.3 70B Versatile | Accurate title extraction       |

### Streaming Progress

Users see real-time updates:

- 🔍 "Analyzing your query..."
- 📊 "Searching vector database (8,945 movies)..."
- 🎬 "Querying TMDb API..."
- 🤖 "Generating response..."
- 🎨 "Fetching movie posters..."

### API Endpoints

| Endpoint          | Method | Description                  |
| ----------------- | ------ | ---------------------------- |
| `/chat_api`       | POST   | Streaming chat with progress |
| `/agent_chat_api` | POST   | Alternative agent endpoint   |
| `/agent_metrics`  | GET    | Performance metrics          |
| `/agent_health`   | GET    | System health check          |

---AI Agent System

### Model Configuration

| Agent | Model | Purpose |
|-------|-------|---------|
| Supervisor | Llama 3.1 8B | Fast query routing |
| Retriever | Llama 3.1 8B | Tool execution |
| Chat | Llama 3.3 70B | Analysis & recommendations |
| Enricher | Llama 3.3 70B | Title extraction |

### Streaming Updates

Real-time progress indicators:
- "Analyzing your query..."
- "Searching vector database (5,722 movies)..."
- "Querying TMDb API..."
- "Generating response..."
- "Fetching movie posters..."

### Key Endpoints

- `/chat_api` - Streaming chat with progress updates
- `/agent_metrics` - Performance metrics
- `/agent_health` - System health check
## 🧪 Testing

Test the agent system independently:
Performance Metrics

**Response Times**
- Average: 2-3 seconds end-to-end
- Supervisor routing: 0.3-0.5 seconds
- Vector search: 0.5-1 second
- Success rate: 98%+

**Database**
- Vector database: 5,722 movies with OpenAI embeddings
- Embedding dimension: 1536 (text-embedding-3-small)
- Deployment size: 280 KB (99.96% reduction)

**Cost**
- Embeddings: ~$0.006 per 1000 movies
- Smart model selection: 30-40% savings on inference
---

## 📁 Project Structure

```
FrameIQ/
├── app.py                 # Flask application entry point
├── models.py              # Database models (TVShowProgress, TVEpisodeWatch, UpcomingEpisode)
├── requirements.txt       # Python dependencies
├── scripts/               # Utility scripts
│   ├── sync_upcoming_episodes.py  # Syncs upcoming TV episodes from TMDb
│   ├── collect_media.py   # Collects movie data
│   └── generate_embeddings.py     # Creates vector embeddings
├── api/                   # Legacy API utilities
│   ├── chatbot.py        # LLM utilities (still used)
│   ├── rag_helper.py     # RAG helpers
│   ├── vector_db.py      # ChromaDB interface
│   └── tmdb_client.py    # TMDb API wrapper (TV show details, episode data)
├── src/                   # LangGraph agent system
│   ├── agents/
│   │   ├── state.py      # GraphState schema
│   │   ├── tools.py      # LangChain tools
│   │   ├── nodes.py      # Agent nodes
│   │   ├── graph.py      # StateGraph workflow
│   │   ├── error_handling.py  # Retry logic
│   │   ├── memory.py     # Conversation persistence
│   │   ├── monitoring.py # Performance tracking
│   │   └── rate_limiter.py    # Request throttling
│   └── api/
│       ├── agent_service.py   # Main service
│       └── flask_integration.py  # Flask routes
├── routes/                # Flask blueprints
│   ├── tv_tracking.py    # TV show tracking routes & APIs
│   ├── main.py           # Core routes
│   ├── auth.py           # Authentication
│   ├── social.py         # Social features
│   └── reviews.py        # Review system
├── templates/             # HTML templates
│   ├── tv_dashboard.html # TV tracking dashboard
│   ├── tv_detail.html    # Show detail with tracking
│   ├── tv_season_detail.html  # Episode list view
│   ├── tv_upcoming.html  # Upcoming episodes calendar
│   └── ...               # Other templates
├── static/                # CSS, JS, images
│   └── js/
│       └── tv-seasons.js # Episode tracking JavaScript
└── test_agent.py          # Agent testing utility
```

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

Example queries:
- "Suggest movies like Inception"
- "Recent sci-fi movies from 2024"
- "Tell me about film noir"
- "What's trending right now?"uilt with ❤️ using LangGraph and Flask**
Contributing

Contributions are welcome! Areas for improvement:
- UI/UX enhancements
- Additional AI agent capabilities
- New analytics and visualizations
- Mobile responsive improvements
- Test coverage

---

## License

GNU Affero General Public License v3.0 - see [LICENSE](LICENSE) file

---

## Acknowledgments

- Letterboxd - Inspiration for social features
- LangGraph & LangChain - Multi-agent framework
- Groq - Fast LLM inference
- OpenAI - Superior embedding quality
- TMDb - Comprehensive movie/TV data
- ChromaDB - Efficient vector database

---

Built with LangGraph, OpenAI,