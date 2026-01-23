"""
Clear all data from ChromaDB collection
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

def clear_chromadb():
    """Delete all data from ChromaDB movies collection."""
    try:
        from api.vector_db import get_vector_db
        
        print("🗑️  Clearing ChromaDB...")
        print("-" * 50)
        
        vdb = get_vector_db()
        
        # Check current count
        current_count = vdb.count_movies()
        print(f"Current items in database: {current_count}")
        
        if current_count == 0:
            print("\n✅ Database is already empty!")
            return
        
        print(f"\n⚠️  About to delete {current_count} items...")
        
        # Delete the collection
        vdb.client.delete_collection("movies")
        print("✓ Deleted collection")
        
        # Recreate empty collection
        vdb.collection = vdb.client.get_or_create_collection(
            name="movies",
            metadata={"hnsw:space": "cosine"}
        )
        print("✓ Created new empty collection")
        
        # Verify it's empty
        new_count = vdb.count_movies()
        print(f"\n✅ ChromaDB cleared! Now has {new_count} items")
        
        print("\n" + "-" * 50)
        print("Next steps:")
        print("1. python scripts/collect_media.py")
        print("2. python scripts/generate_embeddings.py")
        print("-" * 50)
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    clear_chromadb()
