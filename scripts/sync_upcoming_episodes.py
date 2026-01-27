"""
Sync upcoming episodes for tracked TV shows
Run this periodically (daily) to keep upcoming episodes updated
"""
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from models import TVShowProgress, UpcomingEpisode
from api.tmdb_client import fetch_tv_show_details
import requests

TMDB_API_KEY = os.getenv('TMDB_API_KEY', '42f58fc8daed3752d51fe70c4281c103')
TMDB_BASE_URL = 'https://api.themoviedb.org/3'


def fetch_show_upcoming_episodes(show_id):
    """Fetch all upcoming episodes for a show from TMDb"""
    try:
        # Get show details first
        show = fetch_tv_show_details(show_id)
        if not show:
            print(f"  Could not fetch show {show_id}")
            return []
        
        show_name = show.get('name', '')
        poster_path = show.get('poster_path', '')
        if poster_path:
            poster_path = f"https://image.tmdb.org/t/p/w500{poster_path}"
        
        print(f"  Processing: {show_name}")
        
        upcoming_episodes = []
        today = datetime.now().date()
        sixty_days = today + timedelta(days=60)
        print(f"  Looking for episodes between {today} and {sixty_days}")
        
        # Get all seasons
        seasons = show.get('seasons', [])
        
        for season in seasons:
            season_number = season.get('season_number', 0)
            
            # Skip specials (season 0)
            if season_number == 0:
                continue
            
            # Fetch season details
            try:
                season_response = requests.get(
                    f'{TMDB_BASE_URL}/tv/{show_id}/season/{season_number}',
                    params={'api_key': TMDB_API_KEY}
                )
                
                if season_response.status_code != 200:
                    continue
                
                season_data = season_response.json()
                episodes = season_data.get('episodes', [])
                
                print(f"    Season {season_number}: {len(episodes)} episodes")
                
                for episode in episodes:
                    air_date_str = episode.get('air_date')
                    if not air_date_str:
                        continue
                    
                    try:
                        air_date = datetime.strptime(air_date_str, '%Y-%m-%d').date()
                    except:
                        continue
                    
                    # Only include episodes within next 60 days
                    if today <= air_date <= sixty_days:
                        print(f"      Found upcoming: S{season_number}E{episode.get('episode_number')} on {air_date}")
                        still_path = episode.get('still_path', '')
                        if still_path:
                            still_path = f"https://image.tmdb.org/t/p/w500{still_path}"
                        
                        upcoming_episodes.append({
                            'show_id': show_id,
                            'show_name': show_name,
                            'poster_path': poster_path,
                            'season_number': season_number,
                            'episode_number': episode.get('episode_number', 0),
                            'episode_name': episode.get('name', ''),
                            'episode_overview': episode.get('overview', ''),
                            'air_date': air_date,
                            'runtime': episode.get('runtime'),
                            'still_path': still_path
                        })
            except Exception as e:
                print(f"    Error fetching season {season_number}: {str(e)}")
                continue
        
        return upcoming_episodes
    
    except Exception as e:
        print(f"  Error processing show {show_id}: {str(e)}")
        return []


def sync_upcoming_episodes():
    """Main sync function"""
    print("=" * 60)
    print("SYNCING UPCOMING EPISODES")
    print("=" * 60)
    
    with app.app_context():
        # Get all shows that users are tracking
        tracked_shows = db.session.query(TVShowProgress.show_id).distinct().all()
        show_ids = [show[0] for show in tracked_shows]
        
        print(f"\nFound {len(show_ids)} unique shows being tracked")
        
        if not show_ids:
            print("No shows being tracked yet. Nothing to sync.")
            return
        
        # Clear old upcoming episodes (older than today)
        today = datetime.now().date()
        deleted = UpcomingEpisode.query.filter(UpcomingEpisode.air_date < today).delete()
        db.session.commit()
        print(f"\nDeleted {deleted} old episode entries")
        
        # Fetch and store upcoming episodes for each show
        total_added = 0
        total_updated = 0
        
        for idx, show_id in enumerate(show_ids, 1):
            print(f"\n[{idx}/{len(show_ids)}] Fetching episodes for show ID {show_id}")
            
            episodes = fetch_show_upcoming_episodes(show_id)
            
            for ep_data in episodes:
                # Check if episode already exists
                existing = UpcomingEpisode.query.filter_by(
                    show_id=ep_data['show_id'],
                    season_number=ep_data['season_number'],
                    episode_number=ep_data['episode_number']
                ).first()
                
                if existing:
                    # Update existing
                    existing.show_name = ep_data['show_name']
                    existing.poster_path = ep_data['poster_path']
                    existing.episode_name = ep_data['episode_name']
                    existing.episode_overview = ep_data['episode_overview']
                    existing.air_date = ep_data['air_date']
                    existing.runtime = ep_data['runtime']
                    existing.still_path = ep_data['still_path']
                    existing.updated_at = datetime.now()
                    total_updated += 1
                else:
                    # Create new
                    new_episode = UpcomingEpisode(**ep_data)
                    db.session.add(new_episode)
                    total_added += 1
            
            # Commit after each show to avoid losing progress
            try:
                db.session.commit()
                print(f"  ✓ Processed {len(episodes)} upcoming episodes")
            except Exception as e:
                print(f"  ✗ Error committing: {str(e)}")
                db.session.rollback()
        
        print("\n" + "=" * 60)
        print(f"SYNC COMPLETE")
        print(f"Added: {total_added} new episodes")
        print(f"Updated: {total_updated} existing episodes")
        print(f"Total upcoming episodes in database: {UpcomingEpisode.query.count()}")
        print("=" * 60)


if __name__ == '__main__':
    sync_upcoming_episodes()
