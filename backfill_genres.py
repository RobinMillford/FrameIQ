from app import app
from models import db, MediaItem
from api.tmdb_client import fetch_movie_details, fetch_tv_show_details
import time

def backfill_genres():
    with app.app_context():
        items = MediaItem.query.filter(MediaItem.genres == None).all()
        print(f"Found {len(items)} items needing genre backfill.")
        
        for item in items:
            try:
                print(f"Fetching genres for: {item.title} ({item.media_type})")
                if item.media_type == 'movie':
                    details = fetch_movie_details(item.tmdb_id)
                else:
                    details = fetch_tv_show_details(item.tmdb_id)
                
                if details and 'genres' in details:
                    item.genres = ",".join(details['genres'])
                    db.session.commit()
                    print(f"Updated genres for {item.title}: {item.genres}")
                
                # Small delay to respect rate limits
                time.sleep(0.5)
            except Exception as e:
                print(f"Error updating {item.title}: {e}")
                db.session.rollback()

if __name__ == "__main__":
    backfill_genres()
