"""
Vector Database Manager for Movie Recommendations using ChromaDB Cloud
Handles embedding generation and similarity search for recent movies
"""

import chromadb
from openai import OpenAI
import os
from typing import List, Dict, Any, Optional
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MovieVectorDB:
    """
    Manages movie embeddings in ChromaDB Cloud for RAG-based recommendations.
    Uses OpenAI embeddings for generating embeddings from movie descriptions.
    """
    
    def __init__(self):
        """
        Initialize the vector database with Chroma Cloud.
        Requires environment variables:
        - CHROMA_API_KEY
        - CHROMA_TENANT
        - CHROMA_DATABASE
        - OPENAI_API_KEY
        """
        # Get Chroma Cloud credentials from environment
        api_key = os.getenv("CHROMA_API_KEY")
        tenant = os.getenv("CHROMA_TENANT")
        database = os.getenv("CHROMA_DATABASE")
        openai_key = os.getenv("OPENAI_API_KEY")
        
        if not all([api_key, tenant, database]):
            logger.error("Missing Chroma Cloud credentials! Set CHROMA_API_KEY, CHROMA_TENANT, and CHROMA_DATABASE")
            raise ValueError("Chroma Cloud credentials not configured")
        
        if not openai_key:
            logger.error("Missing OpenAI API key! Set OPENAI_API_KEY")
            raise ValueError("OpenAI API key not configured")
        
        # Initialize ChromaDB Cloud client (official method)
        logger.info(f"Connecting to Chroma Cloud (tenant: {tenant}, database: {database})")
        self.client = chromadb.CloudClient(
            api_key=api_key,
            tenant=tenant,
            database=database
        )
        
        # Get or create collection for movies
        self.collection = self.client.get_or_create_collection(
            name="movies",
            metadata={"hnsw:space": "cosine"}  # Use cosine similarity
        )
        
        # Initialize OpenAI client
        self.openai_client = OpenAI(api_key=openai_key)
        logger.info("Vector database initialized successfully with OpenAI embeddings")
    
    def _generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding using OpenAI's text-embedding-3-small model.
        
        Args:
            text: Text to embed
            
        Returns:
            List of floats representing the embedding
        """
        try:
            response = self.openai_client.embeddings.create(
                model="text-embedding-3-small",
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            raise
    
    def add_movie(
        self, 
        movie_id: int, 
        title: str, 
        overview: str, 
        metadata: Dict[str, Any]
    ) -> None:
        """
        Add a movie to the vector database.
        
        Args:
            movie_id: TMDb movie ID
            title: Movie title
            overview: Movie overview/description
            metadata: Additional metadata (genres, cast, director, etc.)
        """
        try:
            # Create rich description for embedding
            description = self._create_description(title, overview, metadata)
            
            # Generate embedding using OpenAI
            embedding = self._generate_embedding(description)
            
            # Clean metadata for ChromaDB (only str, int, float, bool allowed)
            clean_metadata = self._clean_metadata(metadata)
            
            # Store in ChromaDB
            self.collection.add(
                ids=[str(movie_id)],
                embeddings=[embedding],
                documents=[description],
                metadatas=[clean_metadata]
            )
            
            logger.info(f"Added movie: {title} (ID: {movie_id})")
            
        except Exception as e:
            logger.error(f"Error adding movie {title}: {e}")
            raise
    
    def add_movies_batch(self, movies: List[Dict[str, Any]]) -> None:
        """
        Add multiple movies in batch for better performance.
        
        Args:
            movies: List of movie dictionaries with id, title, overview, metadata
                    Optional: 'description' key to bypass automatic description generation
        """
        try:
            ids = []
            embeddings = []
            documents = []
            metadatas = []
            
            for movie in movies:
                movie_id = movie['id']
                title = movie['title']
                overview = movie['overview']
                metadata = movie['metadata']
                
                # Use provided description or create one
                if 'description' in movie:
                    description = movie['description']
                elif 'overview' in movie and 'Title:' in movie['overview'] and 'Overview:' in movie['overview']:
                    # Heuristic: if overview looks like a rich description, use it directly
                    description = movie['overview']
                else:
                    description = self._create_description(title, overview, metadata)
                
                # Generate embedding using OpenAI
                embedding = self._generate_embedding(description)
                
                # Clean metadata for ChromaDB
                clean_metadata = self._clean_metadata(metadata)
                
                ids.append(str(movie_id))
                embeddings.append(embedding)
                documents.append(description)
                metadatas.append(clean_metadata)
            
            # Batch upsert to ChromaDB (add new, update existing)
            self.collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas
            )
            
            logger.info(f"Upserted {len(movies)} movies in batch")
            
        except Exception as e:
            logger.error(f"Error adding movies in batch: {e}")
            raise

    def _create_description(
        self, 
        title: str, 
        overview: str, 
        metadata: Dict[str, Any]
    ) -> str:
        """
        Create a rich text description for embedding generation.
        Combines title, overview, genres, cast, director, and other metadata.
        """
        # Extract metadata fields
        genres = ", ".join(metadata.get('genres', [])) if isinstance(metadata.get('genres'), list) else metadata.get('genres', '')
        cast = ", ".join(metadata.get('cast', [])[:5]) if isinstance(metadata.get('cast'), list) else metadata.get('cast', '')
        director = metadata.get('director', 'Unknown')
        year = metadata.get('release_year', 'Unknown')
        rating = metadata.get('rating', 'N/A')
        
        # Handle TV specific fields
        media_type = metadata.get('media_type', 'movie')
        type_label = "TV Show" if media_type == 'tv' else "Movie"
        
        extra_info = ""
        if media_type == 'tv':
            seasons = metadata.get('number_of_seasons', '')
            if seasons:
                extra_info = f"Seasons: {seasons}\n"
        
        # Create rich description
        description = f"""
Type: {type_label}
Title: {title}
Year: {year}
{extra_info}Genres: {genres}
Director: {director}
Cast: {cast}
Rating: {rating}/10
Overview: {overview}
        """.strip()
        
        return description
    
    def search(
        self, 
        query: str, 
        top_k: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Search for similar movies using semantic similarity.
        
        Args:
            query: Search query (natural language)
            top_k: Number of results to return
            filter_metadata: Optional metadata filters
        
        Returns:
            Dictionary with ids, documents, metadatas, and distances
        """
        try:
            # Generate query embedding using OpenAI
            query_embedding = self._generate_embedding(query)
            
            # Search in ChromaDB
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                where=filter_metadata
            )
            
            logger.info(f"Search query: '{query}' - Found {len(results['ids'][0])} results")
            return results
            
        except Exception as e:
            logger.error(f"Error searching for query '{query}': {e}")
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    
    def search_by_exact_title(self, title: str, year: str = None) -> Optional[Dict]:
        """
        Search for exact title match in metadata.
        
        Args:
            title: Exact title to search for (case-insensitive)
            year: Optional year filter
        
        Returns:
            Dictionary with id, metadata, document or None
        """
        try:
            # Get all items
            results = self.collection.get(
                include=["documents", "metadatas"]
            )
            
            if not results or not results['ids']:
                return None
            
            # Search for exact title match (case-insensitive)
            title_lower = title.lower().strip()
            
            for i, metadata in enumerate(results['metadatas']):
                item_title = metadata.get('title', '').lower().strip()
                item_original_title = metadata.get('original_title', '').lower().strip()
                
                # Check title OR original_title match
                if item_title != title_lower and item_original_title != title_lower:
                    # Also try matching just the first part (before any special characters)
                    item_title_base = item_title.split('(')[0].strip() if '(' in item_title else item_title
                    if item_title_base != title_lower:
                        continue
                
                # Check year if provided
                if year:
                    item_year = str(metadata.get('release_year', ''))
                    if item_year != str(year):
                        continue
                
                # Found exact match!
                logger.info(f"Exact match found: {metadata.get('title')} ({metadata.get('release_year')})")
                return {
                    'id': results['ids'][i],
                    'metadata': metadata,
                    'document': results['documents'][i] if results.get('documents') else None
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Error in exact title search: {e}")
            return None
    
    def get_movie_by_id(self, movie_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific movie by its TMDb ID.
        
        Args:
            movie_id: TMDb movie ID
        
        Returns:
            Movie data if found, None otherwise
        """
        try:
            result = self.collection.get(
                ids=[str(movie_id)],
                include=["documents", "metadatas"]
            )
            
            if result['ids']:
                return {
                    'id': result['ids'][0],
                    'document': result['documents'][0],
                    'metadata': result['metadatas'][0]
                }
            return None
            
        except Exception as e:
            logger.error(f"Error getting movie {movie_id}: {e}")
            return None
    
    def delete_movie(self, movie_id: int) -> None:
        """
        Delete a movie from the vector database.
        
        Args:
            movie_id: TMDb movie ID
        """
        try:
            self.collection.delete(ids=[str(movie_id)])
            logger.info(f"Deleted movie ID: {movie_id}")
        except Exception as e:
            logger.error(f"Error deleting movie {movie_id}: {e}")
    
    def count_movies(self) -> int:
        """
        Get the total number of movies in the database.
        
        Returns:
            Number of movies
        """
        try:
            return self.collection.count()
        except Exception as e:
            logger.error(f"Error counting movies: {e}")
            return 0
    
    def clear_database(self) -> None:
        """
        Clear all movies from the database.
        WARNING: This is irreversible!
        """
        try:
            self.client.delete_collection("movies")
            self.collection = self.client.get_or_create_collection(
                name="movies",
                metadata={"hnsw:space": "cosine"}
            )
            logger.info("Database cleared successfully")
        except Exception as e:
            logger.error(f"Error clearing database: {e}")
    
    
    def _clean_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Clean metadata to ensure ChromaDB compatibility.
        ChromaDB only accepts str, int, float, or bool values.
        Converts lists to comma-separated strings.
        
        Args:
            metadata: Raw metadata dictionary
        
        Returns:
            Cleaned metadata dictionary
        """
        clean_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, list):
                # Convert list to comma-separated string
                clean_metadata[key] = ", ".join(str(v) for v in value)
            elif isinstance(value, (str, int, float, bool)):
                clean_metadata[key] = value
            elif value is None:
                clean_metadata[key] = "Unknown"
            else:
                # Convert other types to string
                clean_metadata[key] = str(value)
        return clean_metadata
    
    def _create_description(
        self, 
        title: str, 
        overview: str, 
        metadata: Dict[str, Any]
    ) -> str:
        """
        Create a rich text description for embedding generation.
        Combines title, overview, genres, cast, director, and other metadata.
        
        Args:
            title: Movie title
            overview: Movie overview
            metadata: Additional metadata
        
        Returns:
            Rich text description
        """
        # Extract metadata fields
        genres = ", ".join(metadata.get('genres', []))
        cast = ", ".join(metadata.get('cast', [])[:5])  # Top 5 cast members
        director = metadata.get('director', 'Unknown')
        year = metadata.get('release_year', 'Unknown')
        rating = metadata.get('rating', 'N/A')
        
        # Create rich description
        description = f"""
Title: {title}
Year: {year}
Genres: {genres}
Director: {director}
Cast: {cast}
Rating: {rating}/10
Overview: {overview}
        """.strip()
        
        return description
    
    def get_similar_movies(
        self, 
        movie_id: int, 
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Find movies similar to a given movie.
        
        Args:
            movie_id: TMDb movie ID
            top_k: Number of similar movies to return
        
        Returns:
            List of similar movies with metadata
        """
        try:
            # Get the movie
            movie = self.get_movie_by_id(movie_id)
            if not movie:
                logger.warning(f"Movie {movie_id} not found in database")
                return []
            
            # Search using the movie's document
            results = self.search(movie['document'], top_k=top_k + 1)
            
            # Filter out the original movie and format results
            similar_movies = []
            for i, movie_id_result in enumerate(results['ids'][0]):
                if movie_id_result != str(movie_id):
                    similar_movies.append({
                        'id': movie_id_result,
                        'metadata': results['metadatas'][0][i],
                        'distance': results['distances'][0][i]
                    })
            
            return similar_movies[:top_k]
            
        except Exception as e:
            logger.error(f"Error finding similar movies for {movie_id}: {e}")
            return []


# Singleton instance
_vector_db_instance = None


def get_vector_db() -> MovieVectorDB:
    """
    Get or create the singleton vector database instance.
    
    Returns:
        MovieVectorDB instance connected to Chroma Cloud
    """
    global _vector_db_instance
    if _vector_db_instance is None:
        _vector_db_instance = MovieVectorDB()
    return _vector_db_instance
