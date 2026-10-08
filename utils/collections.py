"""User collection helpers shared across browse and collection pages."""
import logging

from datetime import datetime

from models import db, MediaItem
from api.tmdb_client import fetch_media_details

logger = logging.getLogger(__name__)


def get_user_collection_ids(user):
    """Return (watchlist_ids, viewed_ids) as sets of (tmdb_id, media_type)
    tuples. Empty sets for anonymous users.

    ``viewed_ids`` is the MOVIE half only, from the ``user_viewed`` mirror,
    which is canonical for movies (written by quick-log/diary). For TV it is
    NOT an authority — see :func:`canonical_tv_viewed_keys`, which the card
    partials use instead (Task F4).
    """
    if not user.is_authenticated:
        return set(), set()
    try:
        return (
            {(i.tmdb_id, i.media_type) for i in user.watchlist},
            {(i.tmdb_id, i.media_type) for i in user.viewed_media},
        )
    except Exception as e:
        logger.warning("Could not load collection ids: %s", e)
        return set(), set()


def canonical_tv_viewed_keys(user, tv_ids):
    """``{(tmdb_id, 'tv')}`` for the shows this user has canonically Viewed.

    Task F4. The card partials used to derive a TV "Viewed" badge straight
    from the ``user_viewed`` mirror, which contradicts the F2 rule that TV
    Viewed is derived from canonical progress: a stale mirror row could put
    a green badge on a show the ledger says is 20/38, and a fully watched
    show with no mirror row got no badge at all. The TV detail hero was
    already canonical, so the two surfaces could disagree.

    This delegates to the same two functions the hero, ``/api/view-state``
    and the profile use — ``canonical_progress_map`` and
    ``tv_viewed_from_progress`` — so there is no third definition and no
    second formula in Jinja.

    Bounded: ONE batched call over ``tv_ids`` (two SQL statements
    regardless of card count), details resolved only for shows the user has
    actually started, through the existing shared TMDb cache. It is
    user-scoped and never cached globally.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    ids = [int(i) for i in (tv_ids or ()) if i]
    if not ids:
        return set()
    try:
        from api.user_view_state import (canonical_progress_map,
                                         tv_viewed_from_progress)
        progress = canonical_progress_map(user, ids)
    except Exception as e:
        logger.warning("Could not resolve canonical TV viewed state: %s", e)
        return set()
    return {(show_id, "tv") for show_id, payload in progress.items()
            if tv_viewed_from_progress(payload)}


def _extract_runtime(media_type, data):
    """Runtime in minutes from a TMDb details payload (None if absent)."""
    if media_type == 'movie':
        value = data.get('runtime')
    else:
        runs = data.get('episode_run_time') or []
        value = runs[0] if runs else None
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def get_or_create_media_item(media_id, media_type):
    """Find a MediaItem by TMDb id, creating it from TMDb if missing.

    Returns the MediaItem, or None if it cannot be found/created.
    """
    media_item = MediaItem.query.filter_by(
        tmdb_id=media_id, media_type=media_type).first()
    if media_item:
        return media_item

    data = fetch_media_details(media_type, media_id)
    if not data:
        return None

    date_str = (data.get('release_date') if media_type == 'movie'
                else data.get('first_air_date'))
    release_date = None
    if date_str:
        try:
            release_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            release_date = None

    media_item = MediaItem(
        tmdb_id=media_id,
        media_type=media_type,
        title=data.get('title') if media_type == 'movie' else data.get('name'),
        release_date=release_date,
        poster_path=data.get('poster_path'),
        overview=data.get('overview'),
        rating=data.get('vote_average'),
        runtime=_extract_runtime(media_type, data),
    )
    db.session.add(media_item)
    db.session.commit()
    return media_item
