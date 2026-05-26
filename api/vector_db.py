"""
Vector Database Manager — ChromaDB Cloud + OpenAI embeddings.
"""

import chromadb
from openai import OpenAI
import os
import logging
from typing import List, Dict, Any, Optional
from functools import lru_cache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Module-level embedding cache: text → vector
# lru_cache can't decorate instance methods directly, so we cache at
# module level via a plain dict with a small cap.
_EMBED_CACHE: Dict[str, List[float]] = {}
_EMBED_CACHE_MAX = 512


def _cached_embed(client: OpenAI, text: str) -> List[float]:
    """Generate embedding with an in-process LRU-style cache."""
    if text in _EMBED_CACHE:
        return _EMBED_CACHE[text]
    response = client.embeddings.create(
        model="text-embedding-3-small", input=text
    )
    vec = response.data[0].embedding
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        # Evict oldest entry (insertion-order dict in Python 3.7+)
        _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
    _EMBED_CACHE[text] = vec
    return vec


class MovieVectorDB:
    """ChromaDB Cloud wrapper for FrameIQ media embeddings."""

    def __init__(self):
        api_key = os.getenv("CHROMA_API_KEY")
        tenant = os.getenv("CHROMA_TENANT")
        database = os.getenv("CHROMA_DATABASE")
        openai_key = os.getenv("OPENAI_API_KEY")

        if not all([api_key, tenant, database]):
            raise ValueError(
                "Missing Chroma Cloud credentials "
                "(CHROMA_API_KEY / CHROMA_TENANT / CHROMA_DATABASE)"
            )
        if not openai_key:
            raise ValueError("Missing OPENAI_API_KEY")

        logger.info(
            "Connecting to Chroma Cloud (tenant: %s, db: %s)", tenant, database
        )
        self.client = chromadb.CloudClient(
            api_key=api_key, tenant=tenant, database=database
        )
        self.collection = self.client.get_or_create_collection(
            name="movies", metadata={"hnsw:space": "cosine"}
        )
        self.openai_client = OpenAI(api_key=openai_key)
        logger.info("Vector DB ready")

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    def _generate_embedding(self, text: str) -> List[float]:
        try:
            return _cached_embed(self.openai_client, text)
        except Exception as e:
            logger.error("Embedding generation failed: %s", e)
            raise

    # ------------------------------------------------------------------
    # Description builder (single definition)
    # ------------------------------------------------------------------

    def _create_description(
        self, title: str, overview: str, metadata: Dict[str, Any]
    ) -> str:
        """
        Build a natural-language description for embedding.

        Kept as prose rather than key:value so sentence embeddings align
        better with conversational queries.
        """
        genres = metadata.get("genres", "")
        if isinstance(genres, list):
            genres = ", ".join(genres)

        cast = metadata.get("cast", "")
        if isinstance(cast, list):
            cast = ", ".join(cast[:5])

        year = metadata.get("release_year", "")
        rating = metadata.get("rating") or metadata.get("vote_average", "")
        media_type = metadata.get("media_type", "movie")
        director = metadata.get("director", "")
        created_by = metadata.get("created_by", "")

        if media_type in ("tv", "anime_tv"):
            type_phrase = "TV series"
            creator_phrase = (
                f" Created by {created_by}." if created_by else ""
            )
        elif media_type == "anime_movie":
            type_phrase = "anime film"
            creator_phrase = (
                f" Directed by {director}." if director else ""
            )
        else:
            type_phrase = "film"
            creator_phrase = (
                f" Directed by {director}." if director else ""
            )

        rating_phrase = f" Rated {rating}/10." if rating else ""
        seasons = metadata.get("number_of_seasons")
        seasons_phrase = (
            f" It ran for {seasons} season(s)." if seasons else ""
        )

        return (
            f"{title} ({year}) is a {type_phrase} in the genres "
            f"{genres}.{creator_phrase} Starring {cast}.{rating_phrase}"
            f"{seasons_phrase} {overview}"
        ).strip()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_movie(
        self,
        movie_id: int,
        title: str,
        overview: str,
        metadata: Dict[str, Any],
    ) -> None:
        description = self._create_description(title, overview, metadata)
        embedding = self._generate_embedding(description)
        clean_meta = self._clean_metadata(metadata)
        # Store lowercase title for fast exact-match filtering
        clean_meta["title_lower"] = title.lower().strip()
        self.collection.add(
            ids=[str(movie_id)],
            embeddings=[embedding],
            documents=[description],
            metadatas=[clean_meta],
        )
        logger.info("Added: %s (ID %s)", title, movie_id)

    def add_movies_batch(self, movies: List[Dict[str, Any]]) -> None:
        ids, embeddings, documents, metadatas = [], [], [], []
        for movie in movies:
            movie_id = movie["id"]
            title = movie["title"]
            overview = movie["overview"]
            metadata = movie["metadata"]

            if "description" in movie:
                description = movie["description"]
            else:
                description = self._create_description(
                    title, overview, metadata
                )

            embedding = self._generate_embedding(description)
            clean_meta = self._clean_metadata(metadata)
            clean_meta["title_lower"] = title.lower().strip()

            ids.append(str(movie_id))
            embeddings.append(embedding)
            documents.append(description)
            metadatas.append(clean_meta)

        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
        logger.info("Upserted %d items", len(movies))

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Semantic similarity search."""
        try:
            query_embedding = self._generate_embedding(query)
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=filter_metadata,
            )
            logger.info(
                "Search '%s' → %d results", query, len(results["ids"][0])
            )
            return results
        except Exception as e:
            logger.error("Search failed for '%s': %s", query, e)
            return {
                "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]
            }

    def search_by_exact_title(
        self, title: str, year: str = None
    ) -> Optional[Dict]:
        """
        Exact-title lookup using ChromaDB metadata filtering.

        Stores title_lower during ingest; queries with $eq on that field
        instead of loading the full collection. Falls back to a small
        semantic search if the field doesn't exist yet (old embeddings).
        """
        title_lower = title.lower().strip()
        try:
            where = {"title_lower": {"$eq": title_lower}}
            results = self.collection.get(
                where=where,
                include=["documents", "metadatas"],
            )
            if results and results["ids"]:
                for i, meta in enumerate(results["metadatas"]):
                    if year and str(meta.get("release_year", "")) != str(year):
                        continue
                    logger.info(
                        "Exact match: %s (%s)",
                        meta.get("title"), meta.get("release_year"),
                    )
                    return {
                        "id": results["ids"][i],
                        "metadata": meta,
                        "document": (
                            results["documents"][i]
                            if results.get("documents")
                            else None
                        ),
                    }
        except Exception:
            pass  # title_lower field may not exist on old embeddings

        # Fallback: semantic search on title string, check top-5
        try:
            sem = self.search(title, top_k=5)
            if not sem or not sem["ids"][0]:
                return None
            for i, meta in enumerate(sem["metadatas"][0]):
                item_title = meta.get("title", "").lower().strip()
                if item_title != title_lower:
                    base = item_title.split("(")[0].strip()
                    if base != title_lower:
                        continue
                if year and str(meta.get("release_year", "")) != str(year):
                    continue
                return {
                    "id": sem["ids"][0][i],
                    "metadata": meta,
                    "document": sem["documents"][0][i],
                }
        except Exception as e:
            logger.error("Fallback title search failed: %s", e)
        return None

    def get_movie_by_id(self, movie_id: int) -> Optional[Dict[str, Any]]:
        try:
            result = self.collection.get(
                ids=[str(movie_id)], include=["documents", "metadatas"]
            )
            if result["ids"]:
                return {
                    "id": result["ids"][0],
                    "document": result["documents"][0],
                    "metadata": result["metadatas"][0],
                }
            return None
        except Exception as e:
            logger.error("get_movie_by_id(%s) failed: %s", movie_id, e)
            return None

    def get_similar_movies(
        self, movie_id: int, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        movie = self.get_movie_by_id(movie_id)
        if not movie:
            return []
        results = self.search(movie["document"], top_k=top_k + 1)
        return [
            {
                "id": rid,
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i],
            }
            for i, rid in enumerate(results["ids"][0])
            if rid != str(movie_id)
        ][:top_k]

    def delete_movie(self, movie_id: int) -> None:
        try:
            self.collection.delete(ids=[str(movie_id)])
            logger.info("Deleted ID %s", movie_id)
        except Exception as e:
            logger.error("delete_movie(%s) failed: %s", movie_id, e)

    def count_movies(self) -> int:
        try:
            return self.collection.count()
        except Exception as e:
            logger.error("count_movies failed: %s", e)
            return 0

    def clear_database(self) -> None:
        try:
            self.client.delete_collection("movies")
            self.collection = self.client.get_or_create_collection(
                name="movies", metadata={"hnsw:space": "cosine"}
            )
            logger.info("Database cleared")
        except Exception as e:
            logger.error("clear_database failed: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Convert metadata to ChromaDB-safe scalar types."""
        clean = {}
        for key, value in metadata.items():
            if isinstance(value, list):
                clean[key] = ", ".join(str(v) for v in value)
            elif isinstance(value, (str, int, float, bool)):
                clean[key] = value
            elif value is None:
                clean[key] = ""
            else:
                clean[key] = str(value)
        return clean


# Singleton
_vector_db_instance = None


def get_vector_db() -> MovieVectorDB:
    """Return the singleton MovieVectorDB instance."""
    global _vector_db_instance
    if _vector_db_instance is None:
        _vector_db_instance = MovieVectorDB()
    return _vector_db_instance
