"""
Sync upcoming episodes for tracked TV shows
Run this periodically (daily) to keep upcoming episodes updated

Task F4 — recently AIRED episodes are retained, not purged on the day they
air.

The sync used to delete every row with ``air_date < today``, so the table
could only ever hold ``air_date == today``. That quietly weakened the
canonical aired universe (``api/user_view_state._batched_aired_calendar``
reads ``air_date <= today``): the only calendar contribution to aired
reality was the single day a row spent on the boundary.

Two things changed, and only these two:

  * the TMDb season fetch looks back ``AIRED_RETENTION_DAYS`` as well as
    forward ``UPCOMING_HORIZON_DAYS``, so recently aired episodes are
    written at all;
  * the purge keeps the same look-back window instead of cutting at today.

Why this is safe for every consumer (audited before choosing):

  * ``_batched_aired_calendar`` (canonical aired) reads ``air_date <= today``
    — this is the consumer that WANTS the history;
  * ``/api/tv/upcoming-episodes`` filters ``air_date >= today``;
  * ``/api/tv/calendar`` and ``api/calendar.py`` filter an explicit
    ``start <= air_date <= end`` window, which is correct for a calendar;
  * ``api/notifications.py`` filters ``air_date <= today`` and is already
    idempotent per (user, show, season, episode), so a wider scan cannot
    re-notify;
  * the next-episode resolver only looks up specific ``(season, episode)``
    keys, so extra rows cannot change its answer.

Growth stays bounded: rows exist only for shows someone is tracking, and
only for episodes inside a fixed day window. No schema change, no new
table, no per-request TMDb call — the canonical resolver's historical
reconstruction from cached TMDb season metadata is unchanged and remains the
primary source.
"""
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, db
from models import TVShowProgress, UpcomingEpisode
from api.tmdb_client import fetch_tv_show_details

# Feature 04: notify tracked users about episodes that just aired. Runs BEFORE
# the expiry purge below so the fresh transition is never missed.
from api.notifications import notify_newly_aired_episodes
import requests

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
if not TMDB_API_KEY:
    sys.exit("TMDB_API_KEY environment variable is required")
TMDB_BASE_URL = 'https://api.themoviedb.org/3'

# How far ahead to sync. Unchanged from the original behaviour.
UPCOMING_HORIZON_DAYS = 60

# How far back to KEEP aired episodes (Task F4). Four weeks covers roughly
# four episodes of a weekly show, which is enough to keep the canonical
# aired universe reconstructable through a TMDb outage while staying small:
# rows only exist for tracked shows, and only inside this window.
AIRED_RETENTION_DAYS = 28


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
        window_start = today - timedelta(days=AIRED_RETENTION_DAYS)
        horizon_end = today + timedelta(days=UPCOMING_HORIZON_DAYS)
        print(f"  Looking for episodes between {window_start} and {horizon_end}")
        
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
                    params={'api_key': TMDB_API_KEY},
                    timeout=(3, 10),
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
                    except Exception:
                        continue
                    
                    # Task F4: keep a bounded window of RECENTLY AIRED
                    # episodes as well as the upcoming horizon — the
                    # canonical aired universe reads `air_date <= today`.
                    if window_start <= air_date <= horizon_end:
                        print(f"      Found: S{season_number}E{episode.get('episode_number')} on {air_date}")
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
        
        # Feature 04 — notify tracked users about episodes that just aired.
        # Must run BEFORE the purge below: episodes are purged once their
        # air_date passes, and this window is the transition the notification
        # represents. Idempotent (unique constraint), so re-runs never duplicate.
        try:
            created = notify_newly_aired_episodes()
            print(f"\nCreated {created} new-episode notification(s)")
        except Exception as e:
            # Never let a notification failure abort the sync.
            db.session.rollback()
            print(f"  Notification fan-out failed (sync continues): {e}")

        # Task F4: expire rows that have fallen out of BOTH windows. The cut
        # used to be `air_date < today`, which destroyed the only calendar
        # record of anything that had aired — see the module docstring.
        today = datetime.now().date()
        retention_cutoff = today - timedelta(days=AIRED_RETENTION_DAYS)
        deleted = UpcomingEpisode.query.filter(
            UpcomingEpisode.air_date < retention_cutoff).delete()
        db.session.commit()
        print(f"\nDeleted {deleted} episode entries aired before {retention_cutoff}")
        
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
        print("SYNC COMPLETE")
        print(f"Added: {total_added} new episodes")
        print(f"Updated: {total_updated} existing episodes")
        print(f"Total upcoming episodes in database: {UpcomingEpisode.query.count()}")
        print("=" * 60)


if __name__ == '__main__':
    sync_upcoming_episodes()
