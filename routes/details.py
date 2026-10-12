from flask import Blueprint, abort, render_template, request
from flask_login import current_user
from api.tmdb_client import fetch_movie_details, fetch_tv_show_details, fetch_actor_details
from api.user_view_state import tv_viewed_from_progress
from datetime import datetime
from models import UserListItem, DiaryEntry, Review, MediaItem
from models.director import Director, MediaDirector
from sqlalchemy.orm import joinedload
from utils.request_guard import expensive_page_limit

details = Blueprint('details', __name__)


def _taste_match(user_id, item_genres):
    """Percentage (0-100) affinity between the user's rating history and
    the item's genres. None when there's no history to judge with."""
    if not item_genres:
        return None
    try:
        item_set = {g.strip().lower() for g in item_genres if g.strip()}
        if not item_set:
            return None
        reviews = (
            Review.query.options(joinedload(Review.media)).filter_by(user_id=user_id)
            .order_by(Review.created_at.desc()).limit(40).all()
        )
        if len(reviews) < 2:
            return None
        genre_hits = {}
        for r in reviews:
            if r.media and r.media.genres:
                for g in str(r.media.genres).split(','):
                    g = g.strip().lower()
                    if g:
                        genre_hits[g] = genre_hits.get(g, 0) + 1
        if not genre_hits:
            return None
        overlap = sum(genre_hits.get(g, 0) for g in item_set)
        # 55% floor — anything shares some DNA with your history
        raw = overlap / max(len(item_set), 1)
        return int(55 + min(raw, 1.0) * 44)
    except Exception:
        return None


@details.route('/movie/<int:movie_id>')
@expensive_page_limit
def movie_detail(movie_id):
    try:
        movie = fetch_movie_details(movie_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_viewed_ids = set()
        user_lists_with_movie = []
        diary_entries = []
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
            
            # Association tables (user_list_item, diary_entry) store the
            # INTERNAL MediaItem.id, not the TMDb id (see routes/lists.py
            # and routes/diary.py inserts). Map TMDb id -> MediaItem first.
            # Filter-only lookup: never create a MediaItem from a read path.
            # If it is absent, the user cannot have list/diary rows for it.
            media_item = MediaItem.query.filter_by(
                tmdb_id=movie_id, media_type='movie').first()
            if media_item:
                # Find which lists contain this movie
                list_items = UserListItem.query.options(
                    joinedload(UserListItem.list)
                ).filter_by(
                    media_id=media_item.id,
                    media_type='movie'
                ).all()

                for item in list_items:
                    if item.list.user_id == current_user.id:
                        user_lists_with_movie.append(item.list)

                # Find diary entries for this movie
                diary_entries = DiaryEntry.query.filter_by(
                    user_id=current_user.id,
                    media_id=media_item.id,
                    media_type='movie'
                ).order_by(DiaryEntry.watched_date.desc()).all()
        
        taste_match = _taste_match(current_user.id, movie.get('genres')) if current_user.is_authenticated else None

        return render_template('movie_detail.html', movie=movie,
                               taste_match=taste_match,
                               user_watchlist_ids=user_watchlist_ids,
                               user_viewed_ids=user_viewed_ids,
                               user_lists_with_movie=user_lists_with_movie,
                               diary_entries=diary_entries,
                               today=datetime.now().strftime('%Y-%m-%d'))
    except Exception as e:
        print(f"Error fetching movie details: {e}")
        return render_template('error.html', message="Could not fetch movie details. Please try again later.")


@details.route('/tv/<int:show_id>')
@expensive_page_limit
def tv_detail(show_id):
    try:
        show = fetch_tv_show_details(show_id)
        
        # Get user's lists if authenticated
        user_watchlist_ids = set()
        user_viewed_ids = set()
        user_lists_with_show = []
        diary_entries = []
        
        if current_user.is_authenticated:
            user_watchlist_ids = {(item.tmdb_id, item.media_type) for item in current_user.watchlist}
            user_viewed_ids = {(item.tmdb_id, item.media_type) for item in current_user.viewed_media}
            
            # Same internal-id mapping as the movie route above.
            media_item = MediaItem.query.filter_by(
                tmdb_id=show_id, media_type='tv').first()
            if media_item:
                # Find which lists contain this TV show
                list_items = UserListItem.query.options(
                    joinedload(UserListItem.list)
                ).filter_by(
                    media_id=media_item.id,
                    media_type='tv'
                ).all()

                for item in list_items:
                    if item.list.user_id == current_user.id:
                        user_lists_with_show.append(item.list)

                # Find diary entries for this TV show (show-level entries only;
                # episode watches live in TVEpisodeWatch and are NOT counted here)
                diary_entries = DiaryEntry.query.filter_by(
                    user_id=current_user.id,
                    media_id=media_item.id,
                    media_type='tv'
                ).order_by(DiaryEntry.watched_date.desc()).all()

        # Overall aired-episode progress for the hero (shared contract,
        # api/user_view_state.py). Recomputed per request so a newly aired
        # episode immediately lowers a "100% watched" running show.
        # Task F2: the hero Viewed badge and the Mark/Unmark action are
        # derived from this SAME canonical payload (tv_viewed_from_progress)
        # — ``user_viewed`` can never put a Viewed badge on a show whose
        # canonical watched state is below 100% (spec §18).
        tv_progress = None
        if current_user.is_authenticated:
            from api.user_view_state import tv_aired_progress
            tv_progress = tv_aired_progress(
                current_user, [show_id]).get(show_id)
        tv_viewed = tv_viewed_from_progress(tv_progress)

        # Last-watched episode for Watch Now button (Continue Watching intent)
        watch_resume = None
        if current_user.is_authenticated:
            from models import ContinueWatchingItem
            cw_item = (ContinueWatchingItem.query
                       .filter(
                           ContinueWatchingItem.user_id == current_user.id,
                           ContinueWatchingItem.tmdb_id == show_id,
                           ContinueWatchingItem.media_type == 'tv',
                       )
                       .order_by(ContinueWatchingItem.started_at.desc())
                       .first())
            if cw_item and cw_item.season is not None and cw_item.episode is not None:
                watch_resume = cw_item

        taste_match = _taste_match(current_user.id, show.get('genres')) if current_user.is_authenticated else None

        return render_template('tv_detail.html', show=show,
                               taste_match=taste_match,
                               user_watchlist_ids=user_watchlist_ids,
                               user_viewed_ids=user_viewed_ids,
                               user_lists_with_show=user_lists_with_show,
                               diary_entries=diary_entries,
                               tv_progress=tv_progress,
                               tv_viewed=tv_viewed,
                               watch_resume=watch_resume,
                               today=datetime.now().strftime('%Y-%m-%d'))
    except Exception as e:
        print(f"Error fetching TV show details: {e}")
        return render_template('error.html', message="Could not fetch TV show details. Please try again later.")


# ── Director detail (Feature F19) ────────────────────────────────────────────
# Unlike /actor, this page is served ENTIRELY from local persisted rows and
# makes NO TMDb call: the Director/MediaDirector relationships are already the
# canonical local source, and a detail page that reached out to TMDb would add
# an external dependency (and a failure mode) to a page that needs none.
#
# The URL takes the LOCAL Director primary key, not tmdb_person_id. That is
# deliberate: nothing in the database exposes tmdb_person_id, the Taste DNA
# payload resolves against the local row, and F14/F18 already established that
# a name is only safe to link when it maps to exactly one local record.
DIRECTOR_FILMOGRAPHY_PER_PAGE = 24


@details.route('/director/<int:director_id>')
@expensive_page_limit
def director_detail(director_id):
    """A director and their locally-linked film credits.

    Public, like /actor and /movie: it renders public catalogue metadata only.
    No viewer-specific state (watchlist, diary, ratings) is read, so a signed-in
    visitor's private rows never touch this page.
    """
    director = Director.query.filter_by(id=director_id).first()
    if director is None:
        abort(404)

    page = request.args.get('page', 1, type=int)
    # ONE query for the director, ONE joined+paginated query for the films.
    # MediaItem is the primary entity (joining from MediaDirector would leave
    # the association rows as the result and the template would silently read
    # the wrong columns). No per-movie accessor is touched -> no N+1.
    pagination = (
        MediaItem.query
        .join(MediaDirector, MediaDirector.media_item_id == MediaItem.id)
        .filter(
            MediaDirector.director_id == director.id,
            MediaItem.media_type == 'movie',
        )
        # Deterministic total order: newest first, then title, then id.
        .order_by(MediaItem.release_date.desc().nullslast(),
                  MediaItem.title, MediaItem.id)
        .paginate(page=page, per_page=DIRECTOR_FILMOGRAPHY_PER_PAGE,
                  error_out=False)
    )

    return render_template('director_detail.html', director=director,
                           pagination=pagination)


@details.route('/actor/<int:actor_id>')
@expensive_page_limit
def actor_detail(actor_id):
    try:
        actor = fetch_actor_details(actor_id)
        return render_template('actor_detail.html', actor=actor)
    except Exception as e:
        print(f"Error fetching actor details: {e}")
        return render_template('error.html', message="Could not fetch actor details. Please try again later.")
