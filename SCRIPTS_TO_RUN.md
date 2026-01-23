# 🚀 3 Scripts to Run (In Order)

## Complete Setup for OpenAI Embeddings

---

### 📝 Step 1: Clear Old Data
```bash
python clear_chromadb.py
```

**What it does:**
- Deletes old sentence-transformer embeddings from ChromaDB
- Creates fresh empty collection
- Prepares for new OpenAI embeddings

**Time:** ~5 seconds

---

### 📝 Step 2: Collect Movie Data
```bash
python scripts/collect_media.py
```

**What it does:**
- Fetches movies/TV shows from TMDb API (2023-2026)
- **Smart deduplication:** Merges with existing data
- Updates changed items, adds new ones
- Saves to `data/movies.json`

**Deduplication Logic:**
```python
# Uses composite key: "{media_type}_{tmdb_id}"
# Examples:
#   - movie_123456
#   - tv_789012
#   - anime_movie_345678
#   - anime_tv_901234

# When fetching:
existing_media = load_existing("data/movies.json")  # Old data
new_media = fetch_from_tmdb()                       # Fresh data
merged = deduplicate(existing_media, new_media)     # No duplicates!

# Result:
#   - New items: Added
#   - Existing items: Updated with latest data
#   - No duplicates: Same ID = same item
```

**Time:** ~15-30 minutes

---

### 📝 Step 3: Generate OpenAI Embeddings
```bash
python scripts/generate_embeddings.py
```

**What it does:**
- Reads `data/movies.json`
- Creates rich descriptions for embeddings
- **Uses OpenAI API** (text-embedding-3-small)
- **Upserts to ChromaDB:** Updates existing, adds new
- No duplicates in vector database

**Time:** ~10-20 minutes

**Cost:** ~$0.02-0.03

---

## 🔄 CI/CD (Automated Monthly Updates)

### GitHub Actions Workflow
**File:** `.github/workflows/update_movie_embeddings.yml`

**Schedule:** 1st of every month at 2 AM UTC

**What it does:**
1. Fetches new movies from TMDb
2. **Deduplicates automatically** (same logic as manual run)
3. Generates OpenAI embeddings
4. Updates ChromaDB Cloud

**Now includes OPENAI_API_KEY!** ✅

---

## 🎯 Duplicate Prevention

### 1. **Local File (data/movies.json)**
- Uses composite ID: `{media_type}_{tmdb_id}`
- Same movie = same ID = no duplicate
- Updates override old data

### 2. **ChromaDB Vector Database**
- Uses upsert operation in `add_movies_batch()`
- Same movie ID = update embedding
- No duplicate embeddings stored

### 3. **Example Flow**

**First Run:**
```
Fetch: Inception (movie_27205)
Save to data/movies.json → 1 entry
Upload to ChromaDB → 1 embedding
```

**Second Run (1 month later):**
```
Fetch: Inception (movie_27205) + new movies
data/movies.json → Still 1 Inception entry (updated)
ChromaDB → Still 1 Inception embedding (updated)
Result: No duplicates! ✅
```

---

## 📊 Summary

| Script | Purpose | Duplicates? | Time |
|--------|---------|-------------|------|
| `clear_chromadb.py` | Clear old embeddings | N/A | 5 sec |
| `collect_media.py` | Fetch TMDb data | ❌ Prevented | 15-30 min |
| `generate_embeddings.py` | Create OpenAI embeddings | ❌ Prevented | 10-20 min |

---

## ⚙️ GitHub Secrets Needed

Add these in GitHub → Settings → Secrets:

```
TMDB_API_KEY         → TMDb API key
OPENAI_API_KEY       → OpenAI API key (NEW!)
CHROMA_API_KEY       → ChromaDB Cloud key
CHROMA_TENANT        → ChromaDB tenant ID
CHROMA_DATABASE      → ChromaDB database name
```

---

## ✅ Ready!

**Run these 3 commands:**
```bash
python clear_chromadb.py
python scripts/collect_media.py
python scripts/generate_embeddings.py
```

**No duplicates. All OpenAI. Fully automated.** 🎉
