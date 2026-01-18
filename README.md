# 🎬 FrameIQ - AI-Powered Movie Recommendation Platform

A sophisticated movie and TV show recommendation platform powered by **LangGraph multi-agent AI system**, featuring intelligent RAG (Retrieval-Augmented Generation), real-time streaming responses, and comprehensive media discovery.

![FrameIQ Interface](images/FrameIQ-Intelligent-Entertainment-Discovery.jpg)

![FrameIQ Architecture](images/Gemini_Generated_Image_xoiv4uxoiv4uxoiv.png)

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)
![LangGraph](https://img.shields.io/badge/LangGraph-Latest-purple.svg)
![License](https://img.shields.io/badge/License-Apache%202.0-yellow.svg)

## 🌐 Live Demo

Check out the live demo on Google Cloud Run: [FrameIQ](https://frameiq-344233295407.asia-south1.run.app/)

---

## ✨ Key Features

### 🤖 **LangGraph Multi-Agent AI System**

- **Intelligent Supervisor**: Routes queries to specialized agents
- **Smart Retriever**: Dynamically selects between ChromaDB vector search and TMDb API
- **Conversational Chat**: Handles general questions with deep movie knowledge
- **Media Enricher**: Automatically fetches posters and metadata
- **Real-time Streaming**: Live progress updates during AI processing

### 🎯 **Advanced Capabilities**

- **Semantic Search**: 8,945+ movies in ChromaDB vector database
- **TMDb Integration**: Real-time data from The Movie Database API
- **Personalized Recommendations**: User watchlists, wishlists, and viewing history
- **Multi-turn Conversations**: Stateful chat with 24-hour memory
- **Rate Limiting**: 20 requests/min per user, 100/min global
- **Performance Monitoring**: Built-in metrics and logging

### 🎨 **User Experience**

- Modern, responsive UI with glassmorphism design
- Google OAuth authentication
- Profile management with avatar support
- Advanced search with autocomplete
- Trending content and personalized feeds

---

## 🏗️ Architecture

### LangGraph Agent Workflow

```
User Query
    ↓
Supervisor (Llama 3.1 8B) → Fast routing (0.3-0.5s)
    ↓
    ├─→ Retriever (Llama 3.1 8B) → Vector DB + TMDb (0.5-1s)
    ├─→ Chat (Llama 3.3 70B) → Deep analysis (1-2s)
    └─→ Enricher (Llama 3.3 70B) → Fetch posters (0.5-1s)
```

### Tech Stack

**Backend**:

- Flask 3.0+ (Web framework)
- LangGraph (Multi-agent orchestration)
- LangChain (LLM integration)
- ChromaDB Cloud (Vector database)
- PostgreSQL (User data)
- Groq API (LLM provider)

**Frontend**:

- HTML5 + Tailwind CSS
- Vanilla JavaScript
- Server-Sent Events (SSE) for streaming

**APIs**:

- TMDb API (Movie/TV data)
- Google OAuth (Authentication)

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL database
- API Keys:
  - Groq API key
  - TMDb API key
  - ChromaDB Cloud credentials
  - Google OAuth credentials

### Installation

1. **Clone the repository**

```bash
git clone https://github.com/yourusername/FrameIQ.git
cd FrameIQ
```

2. **Install dependencies**

```bash
pip install -r requirements.txt
```

3. **Set up environment variables**

Create a `.env` file:

```env
# Database
DATABASE_URL=postgresql://user:password@host/database

# API Keys
GROQ_API_KEY=your_groq_api_key
TMDB_API_KEY=your_tmdb_api_key

# ChromaDB Cloud
CHROMA_API_KEY=your_chroma_api_key
CHROMA_TENANT=your_tenant_id
CHROMA_DATABASE=your_database_name

# Google OAuth
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret

# Flask
SECRET_KEY=your_secret_key
```

4. **Run the application**

```bash
python app.py
```

Visit `http://localhost:5000`

---

## 🤖 AI Agent System

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

---

## 📊 Features

### User Management

- Google OAuth authentication
- Profile customization with avatars
- Watchlist, wishlist, and viewing history
- Personalized recommendations

### Content Discovery

- Trending movies and TV shows
- Now playing and upcoming releases
- Genre-based browsing
- Advanced search with autocomplete
- Actor/director profiles

### AI Chat

- Natural language movie queries
- Semantic similarity search
- Multi-turn conversations
- Context-aware recommendations
- Real-time streaming responses

---

## 🧪 Testing

Test the agent system independently:

```bash
python test_agent.py
```

Example queries:

- "Suggest movies like Inception"
- "What are some recent sci-fi movies from 2024?"
- "Tell me about film noir"
- "What's trending right now?"

---

## 📈 Performance

- **Average Response Time**: 2-3 seconds
- **Throughput**: 100 requests/min
- **Success Rate**: 98%+
- **Vector Database**: 8,945 movies indexed
- **Cost Optimization**: 30-40% savings with smart model selection

---

## 🔧 Configuration

### Rate Limits

Edit `src/agents/rate_limiter.py`:

```python
_user_rate_limiter = RateLimiter(max_requests=20, time_window=60)
_global_rate_limiter = RateLimiter(max_requests=100, time_window=60)
```

### Conversation Memory

Edit `src/agents/memory.py`:

```python
_cache_ttl = timedelta(hours=24)  # Session expiration
```

### Recursion Limit

Edit `src/api/agent_service.py`:

```python
config = {"recursion_limit": 15}  # Max agent iterations
```

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

---

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **LangGraph** - Multi-agent orchestration framework
- **LangChain** - LLM integration toolkit
- **Groq** - Fast LLM inference
- **TMDb** - Movie and TV show data
- **ChromaDB** - Vector database

---

## 📞 Support

For issues or questions, please open an issue on GitHub.

---

**Built with ❤️ using LangGraph and Flask**
