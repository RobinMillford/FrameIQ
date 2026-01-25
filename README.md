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

### What FrameIQ Offers Beyond Letterboxd

**Complete TV Show Support**
- Full integration for TV shows, seasons, and episodes
- Anime auto-detection for both movies and series
- Creator, network, and cast information

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
git clone https://github.com/yourusername/FrameIQ.git
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

## 🚀 Deployment

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
├── models.py              # Database models
├── requirements.txt       # Python dependencies
├── api/                   # Legacy API utilities
│   ├── chatbot.py        # LLM utilities (still used)
│   ├── rag_helper.py     # RAG helpers
│   ├── vector_db.py      # ChromaDB interface
│   └── tmdb_helper.py    # TMDb API wrapper
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
├── templates/             # HTML templates
├── static/                # CSS, JS, images
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