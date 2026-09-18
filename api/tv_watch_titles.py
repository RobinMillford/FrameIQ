"""TV watched-title derivation (read-time composition).

Phases 1/4/7 semantics: a TV show is a WATCHED TITLE for a user when
at least one of its episodes has a TVEpisodeWatch row. Tracking
(TVShowProgress) is a separate concept and contributes nothing here.

The movie half of a title-collection surface comes from the canonical
``user_viewed`` state; the TV half is composed at read time from the
episode ledger — no duplication into ``user_viewed``, no schema
change, and no DiaryEntry fabrication. Two batched statements total,
regardless of how many shows the user watches (no N+1).

Network-free: every detail (title/poster) is resolved from the local
MediaItem cache. Callers that want to hydrate shows missing from the
cache may pass ``hydrate`` — a callback invoked with the list of
missing tmdb show ids; after it runs (e.g. the cached-TMDb hydrator in
routes/tv_tracking.py) the metadata query is simply re-issued against
the now-warmed cache. When ``hydrate`` is omitted, missing shows
degrade to "Unknown Show" placeholders instead of failing the page.
"""
from sqlalchemy import func

from models import db, MediaItem
from models.tv import TVEpisodeWatch


def tv_watch_titles(user_id, hydrate=None):
    """Return watched TV titles as collection-card-compatible objects.

    One GROUP BY over TVEpisodeWatch (episode counts, last-watched
    date) + one batched MediaItem lookup. Ordered by last watched
    (desc), then tmdb id — deterministic.

    Each item: tmdb_id, media_type='tv', title, poster_path,
    release_date=None (shows have no single release date),
    episodes_watched, last_watched (date), rating=None.

    The extra per-show fields are additive display data: the shared
    collection-card partial ignores unknown attributes, so TV cards
    render through the exact same template as movie cards.
    """
    grouped = (
        db.session.query(
            TVEpisodeWatch.show_id,
            func.count(TVEpisodeWatch.id).label("events"),
            func.max(TVEpisodeWatch.watched_date).label("last"),
        )
        .filter(TVEpisodeWatch.user_id == user_id)
        .group_by(TVEpisodeWatch.show_id)
        .order_by(func.max(TVEpisodeWatch.watched_date).desc(),
                  TVEpisodeWatch.show_id)
        .all()
    )
    if not grouped:
        return []

    show_ids = [row.show_id for row in grouped]
    metadata = dict(
        db.session.query(MediaItem.tmdb_id, MediaItem)
        .filter(MediaItem.tmdb_id.in_(show_ids),
                MediaItem.media_type == "tv")
        .all()
    )
    if hydrate is not None:
        missing = [sid for sid in show_ids if sid not in metadata]
        if missing:
            hydrate(missing)
            metadata = dict(
                db.session.query(MediaItem.tmdb_id, MediaItem)
                .filter(MediaItem.tmdb_id.in_(show_ids),
                        MediaItem.media_type == "tv")
                .all()
            )

    titles = []
    for row in grouped:
        media = metadata.get(row.show_id)
        titles.append({
            "tmdb_id": row.show_id,
            "media_type": "tv",
            "title": media.title if media else "Unknown Show",
            "poster_path": media.poster_path if media else None,
            "release_date": None,
            "rating": None,
            "episodes_watched": int(row.events),
            "last_watched": row.last,
        })
    return titles
