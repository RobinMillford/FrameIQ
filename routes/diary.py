"""
Diary API Routes
Handles user diary entries for logging watched movies/shows
"""
import logging
import threading
import time

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from datetime import datetime, date
import requests
import os

from extensions import limiter
from models import db, User, DiaryEntry, MediaItem, user_viewed
from models.tv import TVEpisodeWatch

logger = logging.getLogger(__name__)

_EPOCH_DT = datetime(1970, 1, 1)  # sort fallback for NULL created_at

diary = Blueprint('diary', __name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")


# ── Quick log (Feature #1) ───────────────────────────────────────────────────
# Canonical watched state: DiaryEntry is the source of truth for movie watch
# history (one row per watch event, rewatches included). The user_viewed
# junction is a derived boolean set ("has watched at least once") kept in
# sync on every log event; migrates/migrate_canonical_watched.py reconciles
# pre-existing data once.

# Duplicate-submission window (double-click, rapid taps, browser retry).
_QUICKLOG_COOLDOWN_SECONDS = 3.0
_quicklog_guard = {}          # (user_id, tmdb_id, media_type) -> monotonic ts
_quicklog_guard_lock = threading.Lock()


def _quicklog_is_duplicate(user_id, tmdb_id, media_type):
    """True when the same quick-log action was submitted within the cooldown
    window; records this submission either way.

    Deliberately time-based rather than a DB unique constraint: two genuine
    watches on the same day are legitimate history and must both be
    recordable (the second is a rewatch). Only *rapid duplicate requests of
    the same action* are collapsed."""
    now = time.monotonic()
    key = (user_id, tmdb_id, media_type)
    with _quicklog_guard_lock:
        # Keep the guard dict bounded.
        if len(_quicklog_guard) > 10000:
            cutoff = now - _QUICKLOG_COOLDOWN_SECONDS * 20
            for stale in [k for k, ts in _quicklog_guard.items() if ts < cutoff]:
                _quicklog_guard.pop(stale, None)
        last = _quicklog_guard.get(key)
        _quicklog_guard[key] = now
    return last is not None and (now - last) < _QUICKLOG_COOLDOWN_SECONDS


def _get_or_create_movie(media_id, data):
    """Find the MediaItem for a TMDb movie id, creating it locally when
    possible.

    Prefers metadata sent from the card (title/poster/release date are
    already on the page), so a quick log normally performs no external
    request. Falls back to the shared TMDb-backed lookup only when we know
    nothing about the title."""
    media_item = MediaItem.query.filter_by(
        tmdb_id=media_id, media_type='movie').first()
    if media_item:
        return media_item

    title = (data.get('title') or '').strip()
    if title:
        release_date = None
        date_str = data.get('release_date') or ''
        if date_str:
            try:
                release_date = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
            except ValueError:
                release_date = None
        media_item = MediaItem(
            tmdb_id=media_id,
            media_type='movie',
            title=title[:200],
            release_date=release_date,
            poster_path=(data.get('poster_path') or None),
        )
        db.session.add(media_item)
        db.session.flush()
        return media_item

    from utils.collections import get_or_create_media_item
    return get_or_create_media_item(media_id, 'movie')


def quick_log_movie_core(user_id, tmdb_id, title=None, poster_path=None,
                         rating=None):
    """Canonical movie-watched recording (shared with Continue Watching finish).

    Creates today's watch event (a rewatch when the movie was already watched)
    and syncs the derived viewed state. No playback math, no telemetry.
    """
    media_item = _get_or_create_movie(tmdb_id, {
        'title': title or '',
        'poster_path': poster_path or '',
    })
    if not media_item:
        return {'success': False, 'error': 'Media item not found'}

    watch_count = DiaryEntry.query.filter_by(
        user_id=user_id,
        media_id=media_item.id,
        media_type='movie',
    ).count()
    is_rewatch = watch_count > 0

    entry = DiaryEntry(
        user_id=user_id,
        media_id=media_item.id,
        media_type='movie',
        watched_date=date.today(),
        rating=rating,
        is_rewatch=is_rewatch,
    )
    db.session.add(entry)

    user = db.session.get(User, user_id)
    user.total_movies_watched = (user.total_movies_watched or 0) + 1

    # Sync the derived viewed set inside a savepoint so a concurrent log's
    # insert cannot discard our diary entry.
    try:
        with db.session.begin_nested():
            db.session.execute(user_viewed.insert().values(
                user_id=user_id,
                media_id=media_item.id,
                media_type='movie',
                date_viewed=datetime.utcnow(),
            ))
    except IntegrityError:
        pass  # viewed row already exists (concurrent log) — that's the goal

    db.session.commit()
    return {
        'success': True,
        'watched': True,
        'logged_today': True,
        'is_rewatch': is_rewatch,
        'watch_count': watch_count + 1,
        'entry_id': entry.id,
        'title': media_item.title,
    }


@diary.route('/api/media/<int:media_id>/log', methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def quick_log_movie(media_id):
    """One-tap movie logging.

    Creates today's watch event (a rewatch when the movie was already
    watched) and syncs the derived viewed state. Defaults: watched today,
    no rating, no review. Duplicate submissions inside the cooldown window
    return the first submission's outcome instead of duplicating history."""
    data = request.get_json(silent=True) or {}
    media_type = data.get('media_type', 'movie')
    if media_type != 'movie':
        return jsonify({'error': 'Quick log is for movies; TV tracking is separate'}), 400

    if _quicklog_is_duplicate(current_user.id, media_id, 'movie'):
        # Same action re-submitted: report the state the first submission
        # produced instead of creating a second event.
        return jsonify({
            'success': True,
            'watched': True,
            'logged_today': True,
            'duplicate': True,
        }), 200

    rating = data.get('rating')
    if rating is not None:
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid rating'}), 400
        if not (0.5 <= rating <= 5.0):
            return jsonify({'error': 'Rating must be between 0.5 and 5.0'}), 400

    try:
        media_item = _get_or_create_movie(media_id, data)
        if not media_item:
            return jsonify({'error': 'Media item not found'}), 404

        watch_count = DiaryEntry.query.filter_by(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type='movie',
        ).count()
        is_rewatch = watch_count > 0

        entry = DiaryEntry(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type='movie',
            watched_date=date.today(),
            rating=rating,
            is_rewatch=is_rewatch,
        )
        db.session.add(entry)

        # Mirror the existing /api/diary/log behavior.
        current_user.total_movies_watched = (
            current_user.total_movies_watched or 0) + 1

        # Sync the derived viewed set inside a savepoint so a concurrent
        # log's insert cannot discard our diary entry.
        try:
            with db.session.begin_nested():
                db.session.execute(user_viewed.insert().values(
                    user_id=current_user.id,
                    media_id=media_item.id,
                    media_type='movie',
                    date_viewed=datetime.utcnow(),
                ))
        except IntegrityError:
            pass  # viewed row already exists (concurrent log) — that's the goal

        db.session.commit()

        return jsonify({
            'success': True,
            'watched': True,
            'logged_today': True,
            'is_rewatch': is_rewatch,
            'watch_count': watch_count + 1,
            'entry_id': entry.id,
            'title': media_item.title,
        }), 201

    except Exception:
        db.session.rollback()
        logger.error("Quick log failed", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@diary.route('/diary')
@login_required
def diary_page():
    """Render the user's diary page"""
    return render_template('diary.html')


def _tv_event_dict(row, user, tv_media):
    """Serialize a TVEpisodeWatch row as an additive diary event dict.

    Same top-level keys as DiaryEntry.to_dict() (diary.html and
    profile-page.js consume the shared shape) plus an ``episode``
    object with season/number/name/notes. ``tv_media`` maps show tmdb
    id → MediaItem (batch-hydrated by the caller, no per-event query).
    """
    media = tv_media.get(row.show_id)
    return {
        "id": row.id,
        "user": {
            "id": user.id,
            "username": user.username,
            "profile_picture": user.profile_picture,
        },
        "media": {
            "id": row.show_id,
            "title": media.title if media else "Unknown Show",
            "poster_path": media.poster_path if media else None,
            "media_type": "tv",
        },
        "watched_date": row.watched_date.isoformat(),
        "rating": row.rating,
        "review_id": None,
        "is_rewatch": bool(row.is_rewatch),
        "created_at": row.created_at.isoformat()
        if row.created_at else None,
        "episode": {
            "season": row.season_number,
            "number": row.episode_number,
            "name": row.episode_name,
            "notes": row.notes,
        },
    }


def _count_diary_events(user_id, year=None, month=None):
    """Bounded COUNT over both diary sources with the same filters."""
    movie_q = db.session.query(func.count(DiaryEntry.id)).filter(
        DiaryEntry.user_id == user_id)
    tv_q = db.session.query(func.count(TVEpisodeWatch.id)).filter(
        TVEpisodeWatch.user_id == user_id)
    if year:
        movie_q = movie_q.filter(
            db.extract('year', DiaryEntry.watched_date) == year)
        tv_q = tv_q.filter(
            db.extract('year', TVEpisodeWatch.watched_date) == year)
    if month:
        movie_q = movie_q.filter(
            db.extract('month', DiaryEntry.watched_date) == month)
        tv_q = tv_q.filter(
            db.extract('month', TVEpisodeWatch.watched_date) == month)
    return (movie_q.scalar() or 0) + (tv_q.scalar() or 0)


def _hydrate_diary_page(page_items):
    """Batch-load display metadata for one diary page.

    Movies: one joinedload query (to_dict() must not lazy-load).
    TV: one id IN MediaItem lookup. Returns the tv_media map —
    no per-event queries regardless of page composition.
    """
    movie_rows = [e[4] for e in page_items if e[3] == "movie"]
    if movie_rows:
        db.session.query(DiaryEntry).options(
            joinedload(DiaryEntry.user), joinedload(DiaryEntry.media)
        ).filter(DiaryEntry.id.in_([r.id for r in movie_rows])).all()

    tv_rows = [e[4] for e in page_items if e[3] == "tv"]
    if not tv_rows:
        return {}
    show_ids = {r.show_id for r in tv_rows}
    return dict(
        db.session.query(MediaItem.tmdb_id, MediaItem)
        .filter(MediaItem.tmdb_id.in_(show_ids),
                MediaItem.media_type == "tv")
        .all())


def _serialize_diary_page(page_items, user, tv_media):
    """Mixed movie/TV page items → stable JSON entries."""
    entries = []
    for _, _, _, kind, row in page_items:
        if kind == "movie":
            entries.append(row.to_dict())
        else:
            entries.append(_tv_event_dict(row, user, tv_media))
    return entries


@diary.route('/api/diary', methods=['GET'])
@login_required
def get_diary_entries():  # noqa: F811 — defined below the quick-log helpers
    """Unified diary event stream (Phase 5/6).

    The diary is the CHRONOLOGICAL WATCH-EVENT JOURNAL: movie
    DiaryEntry rows + TVEpisodeWatch rows merged into one deterministic
    descending stream (tie-break: created_at/id, newer first).
    Previously this endpoint returned only ``diary_entries`` — TV
    episode watches (which never create DiaryEntry rows — see the
    TV note in routes/tv_tracking.py) were structurally invisible.

    Contract preserved for existing consumers (diary.html,
    profile-page.js): entries/total/pages/has_next/has_prev, same
    entry dict fields. TV events are ADDITIVE dicts with the same
    keys (id/user/media/watched_date/rating/is_rewatch/created_at)
    plus episode fields. No DiaryEntry rows are fabricated.

    Pagination stays correct WITHOUT loading full histories: for page
    N, each source can affect the window with at most N*per_page+1
    rows (the merged prefix before the window holds (N-1)*per_page
    items across ALL sources, plus per_page window items). Fetch that
    bounded prefix per source, merge-sort, and slice — the sorted
    pool's [start:start+per_page] slice is exactly the true window.
    """
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    def _movie_rows(offset, limit):
        q = (
            db.session.query(DiaryEntry)
            .filter(DiaryEntry.user_id == current_user.id)
            .order_by(DiaryEntry.watched_date.desc(),
                      DiaryEntry.created_at.desc(), DiaryEntry.id.desc())
        )
        if year:
            q = q.filter(db.extract('year', DiaryEntry.watched_date) == year)
        if month:
            q = q.filter(db.extract('month', DiaryEntry.watched_date) == month)
        return q.offset(offset).limit(limit).all()

    def _tv_rows(offset, limit):
        q = (
            db.session.query(TVEpisodeWatch)
            .filter(TVEpisodeWatch.user_id == current_user.id)
            .order_by(TVEpisodeWatch.watched_date.desc(),
                      TVEpisodeWatch.created_at.desc(),
                      TVEpisodeWatch.id.desc())
        )
        if year:
            q = q.filter(
                db.extract('year', TVEpisodeWatch.watched_date) == year)
        if month:
            q = q.filter(
                db.extract('month', TVEpisodeWatch.watched_date) == month)
        return q.offset(offset).limit(limit).all()

    # Bounded prefix fetch (see docstring): page N needs at most
    # N*per_page+1 rows per source to fill the merged window exactly.
    limit = page * per_page + 1
    movie_page = _movie_rows(0, limit)
    tv_page = _tv_rows(0, limit)

    events = (
        [(row.watched_date, row.created_at or _EPOCH_DT, row.id,
          "movie", row) for row in movie_page]
        + [(row.watched_date, row.created_at or _EPOCH_DT, row.id,
            "tv", row) for row in tv_page]
    )
    # Global deterministic order: date desc, created_at desc, kind,
    # id desc. created_at is DateTime (tz-naive UTC in both models)
    # so the key tuples are directly comparable.
    events.sort(
        key=lambda e: (e[0], e[1], e[3], e[2]), reverse=True)

    start = (page - 1) * per_page
    page_items = events[start:start + per_page]
    has_next = len(events) > start + per_page
    has_prev = page > 1

    tv_media = _hydrate_diary_page(page_items)
    entries = _serialize_diary_page(page_items, current_user, tv_media)

    # Totals: two bounded COUNT statements (page payloads are already
    # bounded above; the counts give stable pagination numbers).
    total = _count_diary_events(current_user.id, year, month)
    return jsonify({
        'entries': entries,
        'total': total,
        'pages': max(1, -(-total // per_page)),
        'current_page': page,
        'has_next': has_next,
        'has_prev': has_prev,
    }), 200


@diary.route('/api/users/<int:user_id>/diary', methods=['GET'])
def get_user_diary(user_id):
    """Get diary entries for a specific user (public view).

    Same unified movie + TV event stream as the private /api/diary,
    scoped to the target user (no user_id parameter bypass — the id
    is the URL argument, exactly as before).
    """
    user = User.query.get_or_404(user_id)

    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 100)
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    def _movie_rows(offset, limit):
        q = (
            db.session.query(DiaryEntry)
            .filter(DiaryEntry.user_id == user.id)
            .order_by(DiaryEntry.watched_date.desc(),
                      DiaryEntry.created_at.desc(), DiaryEntry.id.desc())
        )
        if year:
            q = q.filter(db.extract('year', DiaryEntry.watched_date) == year)
        if month:
            q = q.filter(db.extract('month', DiaryEntry.watched_date) == month)
        return q.offset(offset).limit(limit).all()

    def _tv_rows(offset, limit):
        q = (
            db.session.query(TVEpisodeWatch)
            .filter(TVEpisodeWatch.user_id == user.id)
            .order_by(TVEpisodeWatch.watched_date.desc(),
                      TVEpisodeWatch.created_at.desc(),
                      TVEpisodeWatch.id.desc())
        )
        if year:
            q = q.filter(
                db.extract('year', TVEpisodeWatch.watched_date) == year)
        if month:
            q = q.filter(
                db.extract('month', TVEpisodeWatch.watched_date) == month)
        return q.offset(offset).limit(limit).all()

    # Bounded prefix fetch, identical to the private stream.
    limit = page * per_page + 1
    movie_page = _movie_rows(0, limit)
    tv_page = _tv_rows(0, limit)

    events = (
        [(row.watched_date, row.created_at or _EPOCH_DT, row.id,
          "movie", row) for row in movie_page]
        + [(row.watched_date, row.created_at or _EPOCH_DT, row.id,
            "tv", row) for row in tv_page]
    )
    events.sort(
        key=lambda e: (e[0], e[1], e[3], e[2]), reverse=True)

    start = (page - 1) * per_page
    page_items = events[start:start + per_page]
    has_next = len(events) > start + per_page

    tv_media = _hydrate_diary_page(page_items)
    entries = _serialize_diary_page(page_items, user, tv_media)
    total = _count_diary_events(user.id, year, month)

    return jsonify({
        'entries': entries,
        'total': total,
        'pages': max(1, -(-total // per_page)),
        'current_page': page,
        'has_next': has_next,
        'has_prev': page > 1,
        'user': {
            'id': user.id,
            'username': user.username,
            'profile_picture': user.profile_picture
        }
    }), 200


@diary.route('/api/diary/log', methods=['POST'])
@login_required
def log_diary_entry():
    """Log a new diary entry"""
    data = request.get_json()

    media_id = data.get('media_id')
    media_type = data.get('media_type')
    watched_date_str = data.get('watched_date')

    if not media_id or not media_type or not watched_date_str:
        return jsonify({'error': 'media_id, media_type, and watched_date are required'}), 400

    try:
        # Parse watched date
        watched_date = datetime.strptime(watched_date_str, '%Y-%m-%d').date()

        # Check if media item exists in our database, if not create it
        media_item = MediaItem.query.filter_by(tmdb_id=media_id, media_type=media_type).first()
        if not media_item:
            # Fetch from TMDB API
            url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
            response = requests.get(url, timeout=(3, 10))
            if response.status_code == 200:
                tmdb_data = response.json()
                title = tmdb_data.get('title') if media_type == 'movie' else tmdb_data.get('name')
                release_date_str = tmdb_data.get('release_date') if media_type == 'movie' else tmdb_data.get('first_air_date')

                # Convert string date to Python date object
                release_date = None
                if release_date_str:
                    try:
                        release_date = datetime.strptime(release_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        release_date = None

                media_item = MediaItem(
                    tmdb_id=media_id,
                    media_type=media_type,
                    title=title,
                    release_date=release_date,
                    poster_path=tmdb_data.get('poster_path'),
                    overview=tmdb_data.get('overview'),
                    rating=tmdb_data.get('vote_average')
                )
                db.session.add(media_item)
                db.session.commit()
            else:
                return jsonify({'error': 'Media item not found'}), 404

        # Check if this is a rewatch
        existing_entries = DiaryEntry.query.filter_by(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type
        ).count()

        is_rewatch = existing_entries > 0

        # Create diary entry
        entry = DiaryEntry(
            user_id=current_user.id,
            media_id=media_item.id,
            media_type=media_type,
            watched_date=watched_date,
            rating=data.get('rating'),
            is_rewatch=is_rewatch
        )

        db.session.add(entry)

        # Update user's total movies watched count
        current_user.total_movies_watched = (current_user.total_movies_watched or 0) + 1

        db.session.commit()

        return jsonify({
            'message': f'Logged {media_item.title} to diary',
            'entry': entry.to_dict()
        }), 201

    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception:
        db.session.rollback()
        logger.error("Failed to add diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@diary.route('/api/diary/<int:entry_id>/update', methods=['PUT'])
@login_required
def update_diary_entry(entry_id):
    """Update a diary entry"""
    entry = DiaryEntry.query.get_or_404(entry_id)

    # Check ownership
    if entry.user_id != current_user.id:
        return jsonify({'error': 'You can only edit your own diary entries'}), 403

    data = request.get_json()

    try:
        if 'watched_date' in data:
            entry.watched_date = datetime.strptime(data['watched_date'], '%Y-%m-%d').date()
        if 'rating' in data:
            entry.rating = data['rating']

        db.session.commit()

        return jsonify({
            'message': 'Diary entry updated successfully',
            'entry': entry.to_dict()
        }), 200

    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    except Exception:
        db.session.rollback()
        logger.error("Failed to update diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


@diary.route('/api/diary/<int:entry_id>/delete', methods=['DELETE'])
@login_required
def delete_diary_entry(entry_id):
    """Delete a diary entry"""
    entry = DiaryEntry.query.get_or_404(entry_id)

    # Check ownership
    if entry.user_id != current_user.id:
        return jsonify({'error': 'You can only delete your own diary entries'}), 403

    try:
        db.session.delete(entry)

        # Update user's total movies watched count
        current_user.total_movies_watched = max(0, (current_user.total_movies_watched or 0) - 1)

        db.session.commit()

        return jsonify({'message': 'Diary entry deleted successfully'}), 200

    except Exception:
        db.session.rollback()
        logger.error("Failed to delete diary entry", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500
