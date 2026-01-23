"""
Generate Rich Embeddings from Comprehensive Movie Data
Creates detailed text descriptions for semantic search and LLM context

🔑 USES OPENAI EMBEDDINGS (text-embedding-3-small)
   - Requires OPENAI_API_KEY in .env
   - Cost: ~$0.02 per 1M tokens (~$0.006 for 1000 movies)
   - No local models needed - runs via API

This script creates embeddings from:
- Title, tagline, and overview
- Genres and keywords
- Cast and crew (director, writers, cinematographer, composer)
- Production details (companies, countries, languages)
- Collection/franchise information
- Review snippets for sentiment and themes
- Similar and recommended movies for relationships

Uploads to ChromaDB Cloud (no local storage)
"""

import json
import sys
import os
from typing import List, Dict, Any

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.vector_db import MovieVectorDB  # Uses OpenAI embeddings automatically


def create_rich_description(movie: Dict[str, Any]) -> str:
    """
    Create an extremely rich text description for embedding generation.
    This gives the LLM maximum context for better responses.
    
    Args:
        movie: Comprehensive movie/TV data dictionary
    
    Returns:
        Rich text description
    """
    parts = []
    
    # Media Type Label - Be very explicit
    media_type = movie.get('media_type', 'movie')
    if media_type == 'anime_tv':
        type_label = "Anime Series (TV Show)"
    elif media_type == 'anime_movie':
        type_label = "Anime Movie"
    elif media_type == 'tv':
        type_label = "TV Show (Series)"
    else:
        type_label = "Movie (Film)"
    parts.append(f"Type: {type_label}")
    
    # Title and basic info
    parts.append(f"Title: {movie['title']}")
    
    if movie.get('original_title') and movie['original_title'] != movie['title']:
        parts.append(f"Original Title: {movie['original_title']}")
    
    if movie.get('tagline'):
        parts.append(f"Tagline: {movie['tagline']}")
    
    # Release info
    release_year = movie['release_date'][:4] if movie.get('release_date') else 'Unknown'
    parts.append(f"Release Year: {release_year}")
    parts.append(f"Status: {movie.get('status', 'Unknown')}")
    
    # TV Specific: Seasons/Episodes
    if media_type == 'tv':
        if movie.get('number_of_seasons'):
            parts.append(f"Seasons: {movie['number_of_seasons']}")
        if movie.get('number_of_episodes'):
            parts.append(f"Episodes: {movie['number_of_episodes']}")
    
    # Collection/Franchise
    if movie.get('collection_name'):
        parts.append(f"Part of Collection: {movie['collection_name']}")
    
    # Genres
    if movie.get('genres'):
        parts.append(f"Genres: {', '.join(movie['genres'])}")
    
    # Keywords (very important for semantic understanding)
    if movie.get('keywords'):
        parts.append(f"Keywords: {', '.join(movie['keywords'][:20])}")  # Top 20 keywords
    
    # TV Specific: Creators
    if media_type == 'tv' and movie.get('created_by'):
        parts.append(f"Created By: {', '.join(movie['created_by'])}")
    
    # Director(s) (mostly for movies)
    if movie.get('director'):
        parts.append(f"Director: {', '.join(movie['director'])}")
    
    # Writers
    if movie.get('writers'):
        parts.append(f"Writers: {', '.join(movie['writers'][:5])}")  # Top 5 writers
    
    # Cinematographer
    if movie.get('cinematographer'):
        parts.append(f"Cinematographer: {', '.join(movie['cinematographer'])}")
    
    # Composer
    if movie.get('composer'):
        parts.append(f"Music Composer: {', '.join(movie['composer'])}")
    
    # Cast (top 10)
    if movie.get('cast'):
        cast_names = [member['name'] for member in movie['cast'][:10]]
        parts.append(f"Main Cast: {', '.join(cast_names)}")
        
        # Character names for better context
        characters = [
            f"{member['name']} as {member['character']}" 
            for member in movie['cast'][:5] 
            if member.get('character')
        ]
        if characters:
            parts.append(f"Characters: {'; '.join(characters)}")
    
    # Production
    if movie.get('production_companies'):
        parts.append(f"Production Companies: {', '.join(movie['production_companies'][:3])}")
    
    if movie.get('production_countries'):
        parts.append(f"Production Countries: {', '.join(movie['production_countries'])}")
    
    # Language
    if movie.get('spoken_languages'):
        parts.append(f"Languages: {', '.join(movie['spoken_languages'])}")
    
    # Runtime and Rating
    if movie.get('runtime'):
        hours = movie['runtime'] // 60
        minutes = movie['runtime'] % 60
        parts.append(f"Runtime: {hours}h {minutes}m")
    
    parts.append(f"Certification: {movie.get('certification', 'NR')}")
    
    # Ratings
    parts.append(f"TMDb Rating: {movie.get('vote_average', 0):.1f}/10 ({movie.get('vote_count', 0)} votes)")
    
    # Overview (main description)
    if movie.get('overview'):
        parts.append(f"\nOverview: {movie['overview']}")
    
    # Review snippets for sentiment and themes
    if movie.get('reviews'):
        review_snippets = []
        for review in movie['reviews'][:2]:  # Top 2 reviews
            snippet = review['content'][:200]  # First 200 chars
            review_snippets.append(f"- {snippet}...")
        if review_snippets:
            parts.append(f"\nReview Highlights:\n" + "\n".join(review_snippets))
    
    # Similar movies (for relationship context)
    if movie.get('similar_movies'):
        similar_titles = [m['title'] for m in movie['similar_movies'][:5]]
        parts.append(f"\nSimilar: {', '.join(similar_titles)}")
    
    # Alternative titles (helps with search)
    if movie.get('alternative_titles'):
        parts.append(f"Also Known As: {', '.join(movie['alternative_titles'])}")
    
    return "\n".join(parts)


def prepare_metadata(movie: Dict[str, Any]) -> Dict[str, Any]:
    """
    Prepare metadata for ChromaDB storage.
    Convert all lists to comma-separated strings.
    
    Args:
        movie: Movie data dictionary
    
    Returns:
        Clean metadata dictionary
    """
    metadata = {
        'media_type': movie.get('media_type', 'movie'),
        'title': movie['title'],
        'original_title': movie.get('original_title', movie['title']),
        'release_date': movie.get('release_date', ''),
        'release_year': movie['release_date'][:4] if movie.get('release_date') else 'Unknown',
        'status': movie.get('status', 'Released'),
        'runtime': movie.get('runtime', 0),
        'vote_average': movie.get('vote_average', 0),
        'vote_count': movie.get('vote_count', 0),
        'popularity': movie.get('popularity', 0),
        'certification': movie.get('certification', 'NR'),
        'budget': movie.get('budget', 0),
        'revenue': movie.get('revenue', 0),
        'original_language': movie.get('original_language', ''),
    }
    
    # TV Specific
    if movie.get('number_of_seasons'):
        metadata['number_of_seasons'] = movie['number_of_seasons']
    if movie.get('number_of_episodes'):
        metadata['number_of_episodes'] = movie['number_of_episodes']
    
    # Convert lists to strings
    if movie.get('genres'):
        metadata['genres'] = ', '.join(movie['genres'])
    
    if movie.get('keywords'):
        metadata['keywords'] = ', '.join(movie['keywords'][:20])
    
    if movie.get('director'):
        metadata['director'] = ', '.join(movie['director'])
        
    if movie.get('created_by'):
        metadata['created_by'] = ', '.join(movie['created_by'])
    
    if movie.get('cast'):
        cast_names = [member['name'] for member in movie['cast'][:10]]
        metadata['cast'] = ', '.join(cast_names)
    
    if movie.get('production_companies'):
        metadata['production_companies'] = ', '.join(movie['production_companies'][:3])
    
    if movie.get('collection_name'):
        metadata['collection_name'] = movie['collection_name']
        metadata['belongs_to_collection'] = movie.get('belongs_to_collection', 0)
    
    if movie.get('writers'):
        metadata['writers'] = ', '.join(movie['writers'][:5])
    
    if movie.get('cinematographer'):
        metadata['cinematographer'] = ', '.join(movie['cinematographer'])
    
    if movie.get('composer'):
        metadata['composer'] = ', '.join(movie['composer'])
    
    return metadata


def generate_embeddings(
    input_file: str = "data/movies.json"
) -> None:
    """
    Generate embeddings for all movies and store in Chroma Cloud.
    
    Args:
        input_file: Path to movies JSON file
    """
    print("=" * 70)
    print("GENERATING RICH EMBEDDINGS FOR MOVIES")
    print("=" * 70)
    
    # Load movies
    print(f"\n[Step 1/3] Loading movies from {input_file}...")
    try:
        # Try compressed version first
        import gzip
        gz_file = input_file + '.gz' if not input_file.endswith('.gz') else input_file
        
        if os.path.exists(gz_file):
            with gzip.open(gz_file, 'rt', encoding='utf-8') as f:
                movies = json.load(f)
            print(f"✓ Loaded {len(movies)} movies from compressed file")
        elif os.path.exists(input_file):
            with open(input_file, 'r', encoding='utf-8') as f:
                movies = json.load(f)
            print(f"✓ Loaded {len(movies)} movies")
        else:
            raise FileNotFoundError(f"{input_file} or {gz_file} not found")
    except FileNotFoundError as e:
        print(f"✗ Error: {e}")
        print("Please run 'python scripts/collect_movies.py' first")
        return
    
    # Initialize vector database (Chroma Cloud with OpenAI embeddings)
    print(f"\n[Step 2/3] Connecting to Chroma Cloud...")
    print("Using OpenAI text-embedding-3-small model")
    vector_db = MovieVectorDB()
    print("✓ Connected to Chroma Cloud with OpenAI embeddings")
    
    # Generate embeddings
    print(f"\n[Step 3/3] Generating embeddings for {len(movies)} movies...")
    print("Using upsert - new movies added, existing movies updated")
    print("This may take several minutes...")
    
    batch_size = 50
    total_batches = (len(movies) + batch_size - 1) // batch_size
    
    for batch_num in range(total_batches):
        start_idx = batch_num * batch_size
        end_idx = min((batch_num + 1) * batch_size, len(movies))
        batch_movies = movies[start_idx:end_idx]
        
        # Prepare batch data
        batch_data = []
        for movie in batch_movies:
            # Create rich description
            description = create_rich_description(movie)
            
            # Prepare metadata
            metadata = prepare_metadata(movie)
            
            batch_data.append({
                'id': movie['id'],
                'title': movie['title'],
                'overview': description,  # Use rich description instead of just overview
                'metadata': metadata
            })
        
        # Add batch to vector database
        try:
            vector_db.add_movies_batch(batch_data)
            print(f"  Batch {batch_num + 1}/{total_batches}: Added {len(batch_data)} movies ({end_idx}/{len(movies)})")
        except Exception as e:
            print(f"  ✗ Error in batch {batch_num + 1}: {e}")
            # Continue with next batch
    
    # Final statistics
    total_count = vector_db.count_movies()
    
    print("\n" + "=" * 70)
    print("EMBEDDING GENERATION COMPLETE")
    print("=" * 70)
    print(f"Total movies in database: {total_count}")
    print("Database location: Chroma Cloud")
    
    # Test search
    print("\n" + "=" * 70)
    print("TESTING SEMANTIC SEARCH")
    print("=" * 70)
    
    test_queries = [
        "epic science fiction movie about desert planet",
        "superhero movie with dark atmosphere",
        "animated family movie",
        "historical drama about war"
    ]
    
    for query in test_queries:
        print(f"\nQuery: '{query}'")
        results = vector_db.search(query, top_k=3)
        
        if results['ids'][0]:
            for i, movie_id in enumerate(results['ids'][0]):
                title = results['metadatas'][0][i]['title']
                year = results['metadatas'][0][i]['release_year']
                similarity = 1 - results['distances'][0][i]
                print(f"  → {title} ({year}) - Similarity: {similarity:.3f}")
        else:
            print("  → No results found")
    
    print("\n✅ Embedding generation complete!")
    print("Next step: Integrate with chat route in Week 3")


if __name__ == "__main__":
    generate_embeddings(
        input_file="data/movies.json"
    )
