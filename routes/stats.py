"""
Film Stats & Analytics Routes
Week 3 Implementation: Personal stats, viewing patterns, Year in Review
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import func, extract, desc, and_, or_, select
from datetime import datetime, timedelta
from collections import defaultdict, Counter
import calendar

from models import db, Review, User, DiaryEntry, MediaItem, user_watchlist, user_viewed

stats_bp = Blueprint('stats', __name__)


@stats_bp.route('/api/stats/overview', methods=['GET'])
@login_required
def get_stats_overview():
    """Get comprehensive user statistics overview"""
    try:
        user_id = current_user.id
        
        # Total counts
        total_reviews = Review.query.filter_by(
            user_id=user_id,
            is_deleted=False
        ).count()
        
        # Total watched (from diary entries)
        total_watched = DiaryEntry.query.filter_by(
            user_id=user_id
        ).count()
        
        # Total watchlist
        total_watchlist = db.session.execute(
            select(func.count()).select_from(user_watchlist).where(
                user_watchlist.c.user_id == user_id
            )
        ).scalar()
        
        # Average rating
        avg_rating = db.session.query(
            func.avg(Review.rating)
        ).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).scalar()
        
        # Ratings distribution
        ratings_dist = db.session.query(
            Review.rating,
            func.count(Review.id)
        ).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).group_by(Review.rating).all()
        
        ratings_distribution = {
            str(rating): count for rating, count in ratings_dist
        }
        
        # This year stats
        current_year = datetime.now().year
        this_year_count = DiaryEntry.query.filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == current_year
        ).count()
        
        return jsonify({
            'success': True,
            'stats': {
                'total_reviews': total_reviews,
                'total_watched': total_watched,
                'total_watchlist': total_watchlist or 0,
                'average_rating': round(float(avg_rating), 2) if avg_rating else 0,
                'ratings_distribution': ratings_distribution,
                'this_year_count': this_year_count
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/by-year', methods=['GET'])
@login_required
def get_stats_by_year():
    """Get viewing statistics grouped by year"""
    try:
        user_id = current_user.id
        
        # Group watched items by year from diary entries
        watched_by_year = db.session.query(
            extract('year', DiaryEntry.watched_date).label('year'),
            func.count(DiaryEntry.id).label('count')
        ).filter(
            DiaryEntry.user_id == user_id,
            DiaryEntry.watched_date.isnot(None)
        ).group_by('year').order_by('year').all()
        
        # Calculate average rating by year
        ratings_by_year = db.session.query(
            extract('year', Review.created_at).label('year'),
            func.avg(Review.rating).label('avg_rating'),
            func.count(Review.id).label('count')
        ).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).group_by('year').order_by('year').all()
        
        year_stats = []
        for watched in watched_by_year:
            year = int(watched.year) if watched.year else 0
            rating_info = next(
                (r for r in ratings_by_year if int(r.year) == year),
                None
            )
            
            year_stats.append({
                'year': year,
                'watched_count': watched.count,
                'average_rating': round(float(rating_info.avg_rating), 2) if rating_info else 0,
                'reviews_count': rating_info.count if rating_info else 0
            })
        
        return jsonify({
            'success': True,
            'stats_by_year': year_stats
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/by-month', methods=['GET'])
@login_required
def get_stats_by_month():
    """Get viewing statistics grouped by month"""
    try:
        user_id = current_user.id
        year = request.args.get('year', datetime.now().year, type=int)
        
        # Group watched items by month for the specified year
        watched_by_month = db.session.query(
            extract('month', DiaryEntry.watched_date).label('month'),
            func.count(DiaryEntry.id).label('count')
        ).filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == year,
            DiaryEntry.watched_date.isnot(None)
        ).group_by('month').all()
        
        # Create a full 12-month array
        month_stats = []
        watched_dict = {int(m.month): m.count for m in watched_by_month}
        
        for month_num in range(1, 13):
            month_stats.append({
                'month': month_num,
                'month_name': calendar.month_name[month_num],
                'count': watched_dict.get(month_num, 0)
            })
        
        return jsonify({
            'success': True,
            'year': year,
            'stats_by_month': month_stats
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/genres', methods=['GET'])
@login_required
def get_genre_stats():
    """Get statistics by genre"""
    try:
        user_id = current_user.id
        
        # Get all diary entries with their media items
        diary_entries = db.session.query(DiaryEntry, MediaItem).join(
            MediaItem, DiaryEntry.media_id == MediaItem.id
        ).filter(
            DiaryEntry.user_id == user_id
        ).all()
        
        # Count genres
        genre_counts = Counter()
        for entry, media in diary_entries:
            if media.genres:
                genres = media.genres if isinstance(media.genres, list) else media.genres.split(',')
                for genre in genres:
                    genre = genre.strip()
                    if genre:
                        genre_counts[genre] += 1
        
        # Get top genres
        top_genres = [
            {'genre': genre, 'count': count}
            for genre, count in genre_counts.most_common(10)
        ]
        
        return jsonify({
            'success': True,
            'top_genres': top_genres,
            'total_genres': len(genre_counts)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/decades', methods=['GET'])
@login_required
def get_decade_stats():
    """Get statistics by decade"""
    try:
        user_id = current_user.id
        
        # Get all diary entries with their media items
        diary_entries = db.session.query(DiaryEntry, MediaItem).join(
            MediaItem, DiaryEntry.media_id == MediaItem.id
        ).filter(
            DiaryEntry.user_id == user_id
        ).all()
        
        # Count by decade
        decade_counts = Counter()
        for entry, media in diary_entries:
            if media.release_date:
                try:
                    year = media.release_date.year
                    decade = (year // 10) * 10
                    decade_counts[f"{decade}s"] += 1
                except (ValueError, TypeError, AttributeError):
                    pass
        
        # Sort decades
        decade_stats = [
            {'decade': decade, 'count': count}
            for decade, count in sorted(decade_counts.items())
        ]
        
        return jsonify({
            'success': True,
            'decade_stats': decade_stats
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/streaks', methods=['GET'])
@login_required
def get_viewing_streaks():
    """Calculate viewing streaks"""
    try:
        user_id = current_user.id
        
        # Get all watched dates, ordered
        watched_dates = db.session.query(
            func.date(DiaryEntry.watched_date).label('date')
        ).filter(
            DiaryEntry.user_id == user_id,
            DiaryEntry.watched_date.isnot(None)
        ).distinct().order_by('date').all()
        
        if not watched_dates:
            return jsonify({
                'success': True,
                'current_streak': 0,
                'longest_streak': 0,
                'total_days': 0
            })
        
        # Convert to date objects
        dates = [d.date for d in watched_dates]
        
        # Calculate streaks
        current_streak = 0
        longest_streak = 0
        temp_streak = 1
        
        today = datetime.now().date()
        
        for i in range(len(dates) - 1):
            diff = (dates[i + 1] - dates[i]).days
            
            if diff == 1:
                temp_streak += 1
            else:
                longest_streak = max(longest_streak, temp_streak)
                temp_streak = 1
        
        longest_streak = max(longest_streak, temp_streak)
        
        # Calculate current streak
        if dates:
            last_date = dates[-1]
            days_since = (today - last_date).days
            
            if days_since == 0:
                current_streak = temp_streak
            elif days_since == 1:
                current_streak = temp_streak - 1 if temp_streak > 1 else 0
            else:
                current_streak = 0
        
        return jsonify({
            'success': True,
            'current_streak': current_streak,
            'longest_streak': longest_streak,
            'total_days': len(dates)
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/year-in-review/<int:year>', methods=['GET'])
@login_required
def get_year_in_review(year):
    """Generate Year in Review for a specific year"""
    try:
        user_id = current_user.id
        
        # Total watched this year
        total_watched = DiaryEntry.query.filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == year
        ).count()
        
        # Get all watched items for the year with media data
        watched_items = db.session.query(DiaryEntry, MediaItem).join(
            MediaItem, DiaryEntry.media_id == MediaItem.id
        ).filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == year
        ).all()
        
        # Top genres
        genre_counts = Counter()
        for entry, media in watched_items:
            if media.genres:
                genres = media.genres if isinstance(media.genres, list) else media.genres.split(',')
                for genre in genres:
                    genre = genre.strip()
                    if genre:
                        genre_counts[genre] += 1
        
        top_genres = [genre for genre, count in genre_counts.most_common(3)]
        
        # Top rated (reviews from this year)
        top_rated = db.session.query(Review).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            extract('year', Review.created_at) == year,
            Review.rating.isnot(None)
        ).order_by(desc(Review.rating)).limit(10).all()
        
        top_rated_list = [
            {
                'media_id': r.media_id,
                'media_type': r.media_type,
                'title': r.title,
                'rating': r.rating
            }
            for r in top_rated
        ]
        
        # Average rating for the year
        avg_rating = db.session.query(
            func.avg(Review.rating)
        ).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            extract('year', Review.created_at) == year,
            Review.rating.isnot(None)
        ).scalar()
        
        # Busiest month
        busiest_month_data = db.session.query(
            extract('month', DiaryEntry.watched_date).label('month'),
            func.count(DiaryEntry.id).label('count')
        ).filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == year
        ).group_by('month').order_by(desc('count')).first()
        
        busiest_month = {
            'month': int(busiest_month_data.month) if busiest_month_data else 0,
            'month_name': calendar.month_name[int(busiest_month_data.month)] if busiest_month_data else 'N/A',
            'count': busiest_month_data.count if busiest_month_data else 0
        }
        
        # Movie vs TV breakdown
        media_type_counts = db.session.query(
            DiaryEntry.media_type,
            func.count(DiaryEntry.id)
        ).filter(
            DiaryEntry.user_id == user_id,
            extract('year', DiaryEntry.watched_date) == year
        ).group_by(DiaryEntry.media_type).all()
        
        media_breakdown = {
            media_type: count for media_type, count in media_type_counts
        }
        
        return jsonify({
            'success': True,
            'year': year,
            'year_in_review': {
                'total_watched': total_watched,
                'top_genres': top_genres,
                'top_rated': top_rated_list,
                'average_rating': round(float(avg_rating), 2) if avg_rating else 0,
                'busiest_month': busiest_month,
                'media_breakdown': media_breakdown
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/compare/<int:other_user_id>', methods=['GET'])
@login_required
def compare_with_user(other_user_id):
    """Compare stats with another user"""
    try:
        user_id = current_user.id
        
        # Get both users
        other_user = User.query.get(other_user_id)
        if not other_user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Get stats for both users
        user1_watched = DiaryEntry.query.filter_by(user_id=user_id).count()
        user2_watched = DiaryEntry.query.filter_by(user_id=other_user_id).count()
        
        user1_reviews = Review.query.filter_by(
            user_id=user_id,
            is_deleted=False
        ).count()
        user2_reviews = Review.query.filter_by(
            user_id=other_user_id,
            is_deleted=False
        ).count()
        
        user1_avg = db.session.query(func.avg(Review.rating)).filter(
            Review.user_id == user_id,
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).scalar()
        
        user2_avg = db.session.query(func.avg(Review.rating)).filter(
            Review.user_id == other_user_id,
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).scalar()
        
        # Find common watched items
        user1_media = set(
            (v.media_id, v.media_type)
            for v in DiaryEntry.query.filter_by(user_id=user_id).all()
        )
        user2_media = set(
            (v.media_id, v.media_type)
            for v in DiaryEntry.query.filter_by(user_id=other_user_id).all()
        )
        
        common_watched = len(user1_media & user2_media)
        
        # Calculate taste compatibility (simple version)
        if user1_watched > 0 and user2_watched > 0:
            compatibility = (common_watched / min(user1_watched, user2_watched)) * 100
        else:
            compatibility = 0
        
        return jsonify({
            'success': True,
            'comparison': {
                'you': {
                    'username': current_user.username,
                    'watched_count': user1_watched,
                    'reviews_count': user1_reviews,
                    'average_rating': round(float(user1_avg), 2) if user1_avg else 0
                },
                'other_user': {
                    'username': other_user.username,
                    'watched_count': user2_watched,
                    'reviews_count': user2_reviews,
                    'average_rating': round(float(user2_avg), 2) if user2_avg else 0
                },
                'common_watched': common_watched,
                'compatibility_score': round(compatibility, 1)
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@stats_bp.route('/api/stats/platform', methods=['GET'])
@login_required
def get_platform_stats():
    """Get platform-wide statistics"""
    try:
        # Total users
        total_users = User.query.filter_by(is_active=True).count()
        
        # Total reviews
        total_reviews = Review.query.filter_by(is_deleted=False).count()
        
        # Total watched
        total_watched = DiaryEntry.query.count()
        
        # Most watched media
        most_watched = db.session.query(
            DiaryEntry.media_id,
            DiaryEntry.media_type,
            MediaItem.title,
            func.count(DiaryEntry.id).label('watch_count')
        ).join(
            MediaItem, DiaryEntry.media_id == MediaItem.id
        ).group_by(
            DiaryEntry.media_id,
            DiaryEntry.media_type,
            MediaItem.title
        ).order_by(desc('watch_count')).limit(10).all()
        
        most_watched_list = [
            {
                'media_id': m.media_id,
                'media_type': m.media_type,
                'title': m.title,
                'watch_count': m.watch_count
            }
            for m in most_watched
        ]
        
        # Platform average rating
        platform_avg = db.session.query(func.avg(Review.rating)).filter(
            Review.is_deleted.is_(False),
            Review.rating.isnot(None)
        ).scalar()
        
        return jsonify({
            'success': True,
            'platform_stats': {
                'total_users': total_users,
                'total_reviews': total_reviews,
                'total_watched': total_watched,
                'average_rating': round(float(platform_avg), 2) if platform_avg else 0,
                'most_watched': most_watched_list
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

