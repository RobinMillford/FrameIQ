"""User collection helpers shared across browse and collection pages."""
import logging

from datetime import datetime

from models import db, MediaItem
from api.tmdb_client import fetch_media_details

logger = logging.getLogger(__name__)


def get_user_collection_ids(user):
    """Return (watchlist_ids, wishlist_ids, viewed_ids) as sets of
    (tmdb_id, media_type) tuples. Empty sets for anonymous users."""
    if not user.is_authenticated:
        return set(), set(), set()
    try:
        return (
            {(i.tmdb_id, i.media_type) for i in user.watchlist},
            {(i.tmdb_id, i.media_type) for i in user.wishlist},
            {(i.tmdb_id, i.media_type) for i in user.viewed_media},
        )
    except Exception as e:
        logger.warning("Could not load collection ids: %s", e)
        return set(), set(), set()


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
    )
    db.session.add(media_item)
    db.session.commit()
    return media_item
