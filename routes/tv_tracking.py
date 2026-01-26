"""TV show tracking routes - episode progress, season tracking, and calendar"""
from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from models import db, TVShowProgress, TVEpisodeWatch, UpcomingEpisode
from api.tmdb_client import fetch_tv_show_details
from datetime import datetime, timedelta
from sqlalchemy import and_, func
import requests
import os

tv_tracking = Blueprint('tv_tracking', __name__)

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = 'https://api.themoviedb.org/3'


@tv_tracking.route('/tv/dashboard')
@login_required
def tv_dashboard():
    """TV Tracking Dashboard - main page for TV tracking"""
    return render_template('tv_dashboard.html', api_key=TMDB_API_KEY)


@tv_tracking.route('/tv/upcoming')
@login_required
def tv_upcoming():
    """Upcoming episodes page with filters"""
    return render_template('tv_upcoming.html', api_key=TMDB_API_KEY)


@tv_tracking.route('/api/tv/<int:show_id>/start-tracking', methods=['POST'])
@login_required
def start_tracking_show(show_id):
    """Start tracking a TV show"""
    try:
        # Check if already tracking
        existing = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if existing:
            return jsonify({'error': 'Already tracking this show'}), 400
        
        # Fetch show details from TMDb
        show = fetch_tv_show_details(show_id)
        
        # Create progress entry
        progress = TVShowProgress(
            user_id=current_user.id,
            show_id=show_id,
            total_seasons=show.get('number_of_seasons', 0),
            total_episodes=show.get('number_of_episodes', 0),
            watched_seasons=0,
            watched_episodes=0,
            status='watching'
        )
        
        db.session.add(progress)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Started tracking show',
            'progress': progress.to_dict()
        }), 201
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/progress', methods=['GET'])
@login_required
def get_show_progress(show_id):
    """Get user's progress for a TV show"""
    try:
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'progress': None}), 200
        
        # Get watched episodes
        watched_episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).order_by(
            TVEpisodeWatch.season_number,
            TVEpisodeWatch.episode_number
        ).all()
        
        return jsonify({
            'progress': progress.to_dict(),
            'watched_episodes': [ep.to_dict() for ep in watched_episodes]
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season>/<int:episode>/mark-watched', methods=['POST'])
@login_required
def mark_episode_watched(show_id, season, episode):
    """Mark an episode as watched"""
    try:
        print(f"\n=== MARK EPISODE WATCHED: Show {show_id}, S{season}E{episode}, User {current_user.id} ===")
        
        data = request.get_json() or {}
        
        # Get or create progress entry
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            print("Creating new progress entry...")
            # Auto-create progress if not exists
            show = fetch_tv_show_details(show_id)
            progress = TVShowProgress(
                user_id=current_user.id,
                show_id=show_id,
                total_seasons=show.get('number_of_seasons', 0),
                total_episodes=show.get('number_of_episodes', 0),
                watched_seasons=0,
                watched_episodes=0,
                status='watching'
            )
            db.session.add(progress)
            db.session.flush()
            print(f"Progress created with ID: {progress.id}")
        else:
            print(f"Using existing progress ID: {progress.id}")
        
        # Check if episode already marked
        existing = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season,
            episode_number=episode
        ).first()
        
        if existing:
            print(f"Episode already watched, updating...")
            # Update existing watch
            existing.watched_date = datetime.strptime(data.get('watched_date', datetime.now().strftime('%Y-%m-%d')), '%Y-%m-%d').date()
            existing.rating = data.get('rating')
            existing.notes = data.get('notes')
            existing.is_rewatch = data.get('is_rewatch', False)
            episode_watch = existing
        else:
            print(f"Adding new episode watch...")
            # Create new watch entry
            episode_watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                progress_id=progress.id,
                season_number=season,
                episode_number=episode,
                episode_name=data.get('episode_name'),
                watched_date=datetime.strptime(data.get('watched_date', datetime.now().strftime('%Y-%m-%d')), '%Y-%m-%d').date(),
                rating=data.get('rating'),
                notes=data.get('notes'),
                is_rewatch=data.get('is_rewatch', False)
            )
            db.session.add(episode_watch)
            
            # Update progress counts
            if not episode_watch.is_rewatch:
                progress.watched_episodes += 1
                print(f"Progress updated: {progress.watched_episodes}/{progress.total_episodes}")
        
        # Update last watched time
        progress.last_watched = datetime.utcnow()
        
        # Check if season completed
        update_season_progress(progress, show_id)
        
        # Check if show completed
        if progress.watched_episodes >= progress.total_episodes and progress.total_episodes > 0:
            progress.status = 'completed'
            progress.completed_at = datetime.utcnow()
            print("Show marked as COMPLETED!")
        
        db.session.commit()
        print("✓ Database commit successful!")
        
        return jsonify({
            'success': True,
            'message': 'Episode marked as watched',
            'progress': progress.to_dict()
        }), 200
        
    except Exception as e:
        print(f"ERROR in mark_episode_watched: {str(e)}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season>/mark-watched', methods=['POST'])
@login_required
def mark_season_watched(show_id, season):
    """Mark entire season as watched"""
    try:
        print(f"\n=== MARK SEASON WATCHED: Show {show_id}, Season {season}, User {current_user.id} ===")
        
        data = request.get_json() or {}
        watched_date = data.get('watched_date', datetime.now().strftime('%Y-%m-%d'))
        
        # Fetch season details from TMDb
        response = requests.get(
            f'{TMDB_BASE_URL}/tv/{show_id}/season/{season}',
            params={'api_key': TMDB_API_KEY}
        )
        
        if response.status_code != 200:
            print(f"ERROR: Failed to fetch season details from TMDb: {response.status_code}")
            return jsonify({'error': 'Failed to fetch season details'}), 400
        
        season_data = response.json()
        episodes = season_data.get('episodes', [])
        print(f"Found {len(episodes)} episodes in season {season}")
        
        # Get or create progress
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            print("Creating new progress entry...")
            show = fetch_tv_show_details(show_id)
            progress = TVShowProgress(
                user_id=current_user.id,
                show_id=show_id,
                total_seasons=show.get('number_of_seasons', 0),
                total_episodes=show.get('number_of_episodes', 0),
                watched_seasons=0,
                watched_episodes=0,
                status='watching'
            )
            db.session.add(progress)
            db.session.flush()
            print(f"Progress created with ID: {progress.id}")
        else:
            print(f"Using existing progress ID: {progress.id}, Current watched: {progress.watched_episodes}/{progress.total_episodes}")
        
        # IMPORTANT: Delete existing episodes for this season first to ensure clean state
        existing_count = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season
        ).count()
        
        if existing_count > 0:
            print(f"Found {existing_count} existing episodes for season {season}, deleting them first...")
            TVEpisodeWatch.query.filter_by(
                user_id=current_user.id,
                show_id=show_id,
                season_number=season
            ).delete()
            db.session.flush()
            print(f"Deleted {existing_count} existing episodes")
            # Update progress count
            progress.watched_episodes -= existing_count
        
        # Mark all episodes in season
        marked_count = 0
        for ep in episodes:
            ep_num = ep['episode_number']
            
            episode_watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                progress_id=progress.id,
                season_number=season,
                episode_number=ep_num,
                episode_name=ep.get('name'),
                watched_date=datetime.strptime(watched_date, '%Y-%m-%d').date()
            )
            db.session.add(episode_watch)
            marked_count += 1
        
        print(f"Added {marked_count} episodes as watched")
        
        # Update progress
        old_watched = progress.watched_episodes
        progress.watched_episodes += marked_count
        progress.last_watched = datetime.utcnow()
        update_season_progress(progress, show_id)
        
        print(f"Progress updated: {old_watched} -> {progress.watched_episodes}")
        
        # Check completion
        if progress.watched_episodes >= progress.total_episodes and progress.total_episodes > 0:
            progress.status = 'completed'
            progress.completed_at = datetime.utcnow()
            print("Show marked as COMPLETED!")
        
        # Commit to database
        db.session.commit()
        print("✓ Database commit successful!")
        
        # Verify data was saved
        saved_episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season
        ).count()
        print(f"Verification: {saved_episodes} episodes saved in database for season {season}")
        
        return jsonify({
            'success': True,
            'message': f'Season {season} marked as watched',
            'marked_episodes': marked_count,
            'progress': progress.to_dict()
        }), 200
        
    except Exception as e:
        print(f"ERROR in mark_season_watched: {str(e)}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/my-shows', methods=['GET'])
@login_required
def get_my_tracked_shows():
    """Get all shows user is tracking"""
    try:
        status_filter = request.args.get('status')  # 'watching', 'completed', 'plan_to_watch', 'dropped'
        
        query = TVShowProgress.query.filter_by(user_id=current_user.id)
        
        if status_filter:
            query = query.filter_by(status=status_filter)
        
        shows = query.order_by(TVShowProgress.last_watched.desc()).all()
        
        return jsonify({
            'shows': [show.to_dict() for show in shows],
            'total': len(shows)
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/update-status', methods=['POST'])
@login_required
def update_show_status(show_id):
    """Update show tracking status"""
    try:
        data = request.get_json()
        new_status = data.get('status')
        
        if new_status not in ['watching', 'completed', 'plan_to_watch', 'dropped']:
            return jsonify({'error': 'Invalid status'}), 400
        
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if not progress:
            return jsonify({'error': 'Show not being tracked'}), 404
        
        progress.status = new_status
        
        if new_status == 'completed':
            progress.completed_at = datetime.utcnow()
        elif progress.completed_at:
            progress.completed_at = None
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'progress': progress.to_dict()
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/upcoming-episodes', methods=['GET'])
@login_required
def get_upcoming_episodes():
    """Get upcoming episodes for shows user is tracking"""
    try:
        # Get shows user is currently watching or planning to watch
        watching_shows = TVShowProgress.query.filter_by(
            user_id=current_user.id
        ).filter(TVShowProgress.status.in_(['watching', 'plan_to_watch'])).all()
        
        show_ids = [show.show_id for show in watching_shows]
        
        # Get upcoming episodes for these shows
        today = datetime.utcnow().date()
        week_from_now = today + timedelta(days=7)
        
        upcoming = UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(show_ids),
            UpcomingEpisode.air_date >= today,
            UpcomingEpisode.air_date <= week_from_now
        ).order_by(UpcomingEpisode.air_date).all()
        
        episodes_list = []
        for ep in upcoming:
            # Check if already watched
            watched = TVEpisodeWatch.query.filter_by(
                user_id=current_user.id,
                show_id=ep.show_id,
                season_number=ep.season_number,
                episode_number=ep.episode_number
            ).first()
            
            if not watched:  # Only include unwatched episodes
                days_until = (ep.air_date - today).days
                ep_dict = ep.to_dict()
                ep_dict['days_until_air'] = days_until
                episodes_list.append(ep_dict)
        
        return jsonify({
            'success': True,
            'episodes': episodes_list
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/api/tv/calendar', methods=['GET'])
@login_required
def get_episode_calendar():
    """Get calendar view of upcoming episodes"""
    try:
        # Get date range
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        if start_date:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        else:
            start_date = datetime.utcnow().date()
        
        if end_date:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            end_date = start_date + timedelta(days=30)
        
        # Get shows user is tracking
        watching_shows = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            status='watching'
        ).all()
        
        show_ids = [show.show_id for show in watching_shows]
        
        # Get upcoming episodes in date range
        episodes = UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(show_ids),
            UpcomingEpisode.air_date >= start_date,
            UpcomingEpisode.air_date <= end_date
        ).order_by(UpcomingEpisode.air_date).all()
        
        # Group by date
        calendar = {}
        for episode in episodes:
            date_key = episode.air_date.isoformat()
            if date_key not in calendar:
                calendar[date_key] = []
            calendar[date_key].append(episode.to_dict())
        
        return jsonify({
            'calendar': calendar,
            'start_date': start_date.isoformat(),
            'end_date': end_date.isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@tv_tracking.route('/tv/calendar')
@login_required
def tv_calendar_page():
    """Render TV calendar page"""
    return render_template('tv_calendar.html', api_key=TMDB_API_KEY)


@tv_tracking.route('/tv/my-shows')
@login_required
def my_shows_page():
    """Render my shows tracking page"""
    return render_template('tv_my_shows.html', api_key=TMDB_API_KEY)


# Helper function
def update_season_progress(progress, show_id):
    """Update watched seasons count based on completed seasons"""
    try:
        # Fetch show details to get season info
        response = requests.get(
            f'{TMDB_BASE_URL}/tv/{show_id}',
            params={'api_key': TMDB_API_KEY}
        )
        
        if response.status_code != 200:
            return
        
        show_data = response.json()
        seasons = show_data.get('seasons', [])
        
        # Count completed seasons
        completed_seasons = 0
        for season in seasons:
            if season['season_number'] == 0:  # Skip specials
                continue
            
            season_num = season['season_number']
            episode_count = season['episode_count']
            
            # Count watched episodes in this season
            watched_in_season = TVEpisodeWatch.query.filter_by(
                user_id=progress.user_id,
                show_id=show_id,
                season_number=season_num
            ).filter(TVEpisodeWatch.is_rewatch == False).count()
            
            if watched_in_season >= episode_count:
                completed_seasons += 1
        
        progress.watched_seasons = completed_seasons
        
    except Exception as e:
        print(f"Error updating season progress: {e}")


# ===== NEW ROUTES FOR SEASON/EPISODE PAGES =====

@tv_tracking.route('/tv/<int:show_id>/season/<int:season_number>')
@login_required
def season_detail(show_id, season_number):
    """Season detail page with episode list"""
    # Get show name from TMDb
    response = requests.get(
        f'{TMDB_BASE_URL}/tv/{show_id}',
        params={'api_key': TMDB_API_KEY}
    )
    show_data = response.json()
    show_name = show_data.get('name', 'Unknown Show')
    
    return render_template(
        'tv_season_detail.html',
        show_id=show_id,
        show_name=show_name,
        season_number=season_number,
        api_key=TMDB_API_KEY
    )


@tv_tracking.route('/tv/<int:show_id>/season/<int:season_number>/episode/<int:episode_number>')
@login_required
def episode_detail(show_id, season_number, episode_number):
    """Episode detail page with watch controls"""
    # Get show name from TMDb
    response = requests.get(
        f'{TMDB_BASE_URL}/tv/{show_id}',
        params={'api_key': TMDB_API_KEY}
    )
    show_data = response.json()
    show_name = show_data.get('name', 'Unknown Show')
    
    return render_template(
        'tv_episode_detail.html',
        show_id=show_id,
        show_name=show_name,
        season_number=season_number,
        episode_number=episode_number,
        api_key=TMDB_API_KEY
    )


@tv_tracking.route('/api/tv/<int:show_id>/watched-episodes')
@login_required
def get_watched_episodes(show_id):
    """Get all watched episodes for a show"""
    try:
        print(f"\n=== GET WATCHED EPISODES: Show {show_id}, User {current_user.id} ===")
        
        episodes = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).all()
        
        print(f"Found {len(episodes)} watched episodes in database")
        
        # Group by season for debugging
        season_counts = {}
        for ep in episodes:
            season_counts[ep.season_number] = season_counts.get(ep.season_number, 0) + 1
        
        print(f"Episodes by season: {season_counts}")
        
        return jsonify({
            'success': True,
            'episodes': [{
                'season_number': ep.season_number,
                'episode_number': ep.episode_number,
                'watched_date': ep.watched_date.isoformat() if ep.watched_date else None,
                'rating': ep.rating,
                'notes': ep.notes,
                'is_rewatch': ep.is_rewatch
            } for ep in episodes]
        })
    except Exception as e:
        print(f"ERROR in get_watched_episodes: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/season/<int:season_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_season_watched(show_id, season_number):
    """Unmark all episodes in a season as unwatched"""
    try:
        # Delete all episode watches for this season
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number
        ).delete()
        
        db.session.commit()
        
        # Update show progress
        update_show_progress(show_id)
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/unmark-watched', methods=['POST'])
@login_required
def unmark_single_episode(show_id, season_number, episode_number):
    """Unmark a single episode as unwatched (new version)"""
    try:
        # Delete episode watch
        TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).delete()
        
        db.session.commit()
        
        # Update show progress
        update_show_progress(show_id)
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/mark-all-watched', methods=['POST'])
@login_required
def mark_all_watched(show_id):
    """Mark all episodes in all seasons as watched (complete series)"""
    try:
        # Fetch show details
        response = requests.get(
            f'{TMDB_BASE_URL}/tv/{show_id}',
            params={'api_key': TMDB_API_KEY}
        )
        show_data = response.json()
        
        # Mark each episode in each season
        for season in show_data.get('seasons', []):
            if season['season_number'] == 0:  # Skip specials
                continue
            
            # Fetch season details
            season_response = requests.get(
                f'{TMDB_BASE_URL}/tv/{show_id}/season/{season["season_number"]}',
                params={'api_key': TMDB_API_KEY}
            )
            season_data = season_response.json()
            
            # Mark each episode
            for episode in season_data.get('episodes', []):
                # Check if already watched
                existing = TVEpisodeWatch.query.filter_by(
                    user_id=current_user.id,
                    show_id=show_id,
                    season_number=season['season_number'],
                    episode_number=episode['episode_number']
                ).first()
                
                if not existing:
                    watch = TVEpisodeWatch(
                        user_id=current_user.id,
                        show_id=show_id,
                        season_number=season['season_number'],
                        episode_number=episode['episode_number'],
                        watched_at=datetime.utcnow()
                    )
                    db.session.add(watch)
        
        # Update show progress to completed
        progress = TVShowProgress.query.filter_by(
            user_id=current_user.id,
            show_id=show_id
        ).first()
        
        if progress:
            progress.status = 'completed'
            progress.completed_at = datetime.utcnow()
        
        db.session.commit()
        
        # Update progress stats
        update_show_progress(show_id)
        
        return jsonify({'success': True, 'message': 'Series completed!'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@tv_tracking.route('/api/tv/<int:show_id>/episode/<int:season_number>/<int:episode_number>/update-watch', methods=['POST'])
@login_required
def update_episode_watch(show_id, season_number, episode_number):
    """Update episode watch with rating and notes"""
    try:
        data = request.get_json()
        
        # Find or create episode watch
        watch = TVEpisodeWatch.query.filter_by(
            user_id=current_user.id,
            show_id=show_id,
            season_number=season_number,
            episode_number=episode_number
        ).first()
        
        if not watch:
            watch = TVEpisodeWatch(
                user_id=current_user.id,
                show_id=show_id,
                season_number=season_number,
                episode_number=episode_number,
                watched_at=datetime.utcnow()
            )
            db.session.add(watch)
        
        # Update fields
        watch.rating = data.get('rating')
        watch.notes = data.get('notes')
        watch.is_rewatch = data.get('is_rewatch', False)
        
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
