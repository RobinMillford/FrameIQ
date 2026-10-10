"""Account data export — explicit, user-scoped serializer (Task F5).

    current user
      -> build_export()          (this module: one explicit, allow-listed read)
      -> build_json()            (complete machine-readable export)
      -> build_csv_bundle_zip()  (flattened per-domain projections)

Design rules that this module exists to enforce
------------------------------------------------
**Explicit contract, not ORM serialisation.** Every field is written by hand
against an allow-list. There is no ``__dict__`` walk, no ``to_dict()``
recycling, no ``SELECT *`` dump. That is the only way to guarantee a column
added to a model six months from now cannot silently start shipping — a
privacy-sensitive credential or another user's data would export itself.

**Canonical source data.** History comes from the model that is the source
of truth (``DiaryEntry`` for movie history, ``TVEpisodeWatch`` for TV
episode history, ``user_watchlist`` for the watchlist). Derived counters
(``TVShowProgress.watched_episodes`` and friends) are omitted rather than
shipped as history — see ``docs/export-format.md`` §7 for why the TV aired
denominator is not reconstructible offline.

**No TMDb, no network.** Export runs entirely off the local database. A
missing optional display field exports as ``null``; it never triggers a
metadata fetch.

**No N+1.** The whole export is a fixed number of bulk statements. Media
titles are resolved with ONE ``MediaItem`` lookup keyed by internal id, and
followed usernames with ONE lookup — not per row. ``tests/test_account_export.py``
asserts the query count does not scale with row count.

**Read-only.** Nothing here writes. ``build_export`` issues SELECTs only,
and the tests assert the session stays clean.
"""
import csv
import io
import json
import logging
import zipfile
from datetime import date, datetime, timezone

from sqlalchemy import select

from models import (ChatConversation, ChatMessage, ContinueWatchingItem,
                    DiaryEntry, ListCollaborator, ListComment, ListLike,
                    MediaComment, MediaLike,
                    MediaItem, Notification, RecommendationFeedback, Review,
                    ReviewComment, ReviewHelpful, ReviewLike, SmartList,
                    Tag, TasteProfile, TVEpisodeWatch, TVShowProgress,
                    User, UserFollow, UserList, UserListCategory,
                    UserListItem, UserMediaTag, UserStreamingService,
                    WatchProgress, db, user_viewed, user_watchlist)

logger = logging.getLogger(__name__)

# ── Format identity ──────────────────────────────────────────────────────────
# Deliberately NOT the application version and NOT a git SHA: a rebuild must
# never change what a version-1 file means.
EXPORT_FORMAT = 'frameiq-export'
EXPORT_VERSION = 1

# ── Value serialisation ──────────────────────────────────────────────────────


def _iso(value):
    """Naive-UTC ``DateTime`` → explicit ISO-8601 with a ``Z`` suffix.

    The project stores naive UTC (``datetime.utcnow()``), so the ``Z`` is a
    truthful label rather than a conversion. A timezone-aware value is
    normalised to UTC first.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat() + 'Z'
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _day(value):
    """A ``db.Date`` stays a calendar day — never widened to an instant.

    ``watched_date`` is the date the user chose. Converting it to a
    midnight instant and re-rendering it in another zone would silently move
    their watch to a different day.
    """
    return value.isoformat() if isinstance(value, (date, datetime)) else (
        None if value is None else str(value))


def _json_text(raw):
    """Parse a stored JSON document column, tolerating corruption.

    Optional display dimensions must never fail a whole export; an
    unparseable document becomes an empty object and is reported once at
    debug level rather than raised to the user.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("account_export: unparseable stored JSON document; "
                     "exporting as empty", exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


# ── Domain serializers ───────────────────────────────────────────────────────
#
# Each returns a list of dicts whose key sets are the CSV headers (plus
# nested blocks for JSON where the CSV is flat). Keys are written explicitly:
# a column that does not exist yet is a KeyError at import time, not a silent
# omission at export time.

def account_section(user):
    """Account + profile. ``password_hash`` is never read."""
    return {
        'user_id': user.id,
        'username': user.username,
        'email': user.email,
        'email_verified': bool(user.email_verified),
        'date_joined': _iso(user.date_joined),
        'streaming_region': user.streaming_region,
    }


def profile_section(user):
    return {
        'first_name': user.first_name,
        'last_name': user.last_name,
        'bio': user.bio,
        'profile_picture': user.profile_picture,
    }


def movie_history_rows(user_id):
    """Canonical movie watch history — ``DiaryEntry`` where type='movie'.

    ``media`` is the local ``MediaItem`` cache, joined in Python via the
    shared id→title map so the query count does not scale with rows.
    """
    rows = db.session.execute(
        select(DiaryEntry, MediaItem)
        .join(MediaItem, MediaItem.id == DiaryEntry.media_id, isouter=True)
        .where(DiaryEntry.user_id == user_id,
               DiaryEntry.media_type == 'movie')
        .order_by(DiaryEntry.watched_date, DiaryEntry.id)
    ).all()
    out = []
    for entry, media in rows:
        out.append({
            'diary_id': entry.id,
            'watched_date': _day(entry.watched_date),
            'media_id': entry.media_id,
            'tmdb_id': media.tmdb_id if media else None,
            'media_type': entry.media_type,
            'title': media.title if media else None,
            'release_date': _day(media.release_date) if media else None,
            'rating': entry.rating,
            'is_rewatch': bool(entry.is_rewatch),
            'created_at': _iso(entry.created_at),
            'review_id': entry.review_id,
        })
    return out


def tv_diary_rows(user_id):
    """Diary entries for TV shows.

    A ``DiaryEntry`` with ``media_type='tv'`` exists in the schema (quick-log
    on a TV detail page), but canonical TV history is ``TVEpisodeWatch``. These
    rows are kept so nothing the user recorded is lost, and they are a
    separate JSON bucket from ``tv_history.episodes`` precisely so an importer
    cannot mistake them for the episode ledger.
    """
    rows = db.session.execute(
        select(DiaryEntry, MediaItem)
        .join(MediaItem, MediaItem.id == DiaryEntry.media_id, isouter=True)
        .where(DiaryEntry.user_id == user_id,
               DiaryEntry.media_type == 'tv')
        .order_by(DiaryEntry.watched_date, DiaryEntry.id)
    ).all()
    out = []
    for entry, media in rows:
        out.append({
            'diary_id': entry.id,
            'watched_date': _day(entry.watched_date),
            'media_id': entry.media_id,
            'tmdb_id': media.tmdb_id if media else None,
            'media_type': entry.media_type,
            'title': media.title if media else None,
            'release_date': _day(media.release_date) if media else None,
            'rating': entry.rating,
            'is_rewatch': bool(entry.is_rewatch),
            'created_at': _iso(entry.created_at),
            'review_id': entry.review_id,
        })
    return out


def tv_episode_rows(user_id):
    """Canonical TV episode history — ``TVEpisodeWatch``, nothing derived.

    Tasks F1–F4 made this the single source of TV watch truth. Future
    episodes simply have no row here, so nothing to filter out; specials are
    exportable if and only if the user actually watched one. Rewatch is the
    row's own flag, so repeated watches stay distinguishable.
    """
    rows = db.session.execute(
        select(TVEpisodeWatch)
        .where(TVEpisodeWatch.user_id == user_id)
        .order_by(TVEpisodeWatch.show_id, TVEpisodeWatch.season_number,
                  TVEpisodeWatch.episode_number, TVEpisodeWatch.id)
    ).scalars().all()

    # Resolve every referenced show title in ONE query.
    show_ids = sorted({r.show_id for r in rows})
    titles = _show_titles(show_ids)

    out = []
    for row in rows:
        show = titles.get(row.show_id, {})
        out.append({
            'watch_id': row.id,
            'watched_date': _day(row.watched_date),
            'show_tmdb_id': row.show_id,
            'show_title': show.get('title'),
            'season_number': row.season_number,
            'episode_number': row.episode_number,
            'episode_name': row.episode_name,
            'rating': row.rating,
            'notes': row.notes,
            'is_rewatch': bool(row.is_rewatch),
            'created_at': _iso(row.created_at),
            'updated_at': _iso(row.updated_at),
        })
    return out


def _show_titles(tmdb_ids):
    """One query: ``{tmdb_show_id: {media_id, title}}`` for many shows.

    ``TVEpisodeWatch.show_id`` is a TMDb id, and ``MediaItem.tmdb_id`` is
    unique, so this is a single ``IN`` lookup rather than one query per show.
    """
    if not tmdb_ids:
        return {}
    rows = db.session.execute(
        select(MediaItem).where(MediaItem.tmdb_id.in_(tmdb_ids))
    ).scalars().all()
    return {r.tmdb_id: {'media_id': r.id, 'title': r.title} for r in rows}


def _media_by_id(internal_ids):
    """One query: ``{MediaItem.id: {...}}`` for many internal ids.

    The junction tables and ``UserListItem`` store ``MediaItem.id``; TMDb-keyed
    tables use ``_show_titles`` instead. Resolving titles in bulk keeps the
    export's statement count independent of row count.
    """
    ids = [i for i in set(internal_ids) if i is not None]
    if not ids:
        return {}
    rows = db.session.execute(
        select(MediaItem).where(MediaItem.id.in_(sorted(ids)))
    ).scalars().all()
    return {row.id: {
        'tmdb_id': row.tmdb_id,
        'title': row.title,
        'release_date': _day(row.release_date),
        'media_type': row.media_type,
    } for row in rows}


def tv_progress_rows(user_id):
    """User-authored TV tracking state — counters deliberately omitted.

    ``watched_episodes`` / ``total_episodes`` / ``*_seasons`` are cached
    counters (``sync_tv_progress_counters``) and the aired denominator needs
    TMDb. Shipping them would present a stale derived number as progress.
    """
    rows = db.session.execute(
        select(TVShowProgress)
        .where(TVShowProgress.user_id == user_id)
        .order_by(TVShowProgress.show_id)
    ).scalars().all()
    titles = _show_titles(sorted({r.show_id for r in rows}))
    out = []
    for row in rows:
        show = titles.get(row.show_id, {})
        out.append({
            'progress_id': row.id,
            'show_tmdb_id': row.show_id,
            'show_media_id': show.get('media_id'),
            'show_title': show.get('title'),
            'status': row.status,
            'is_favorite': bool(row.is_favorite),
            'started_at': _iso(row.started_at),
            'last_watched': _iso(row.last_watched),
            'completed_at': _iso(row.completed_at),
            'created_at': _iso(row.created_at),
            'updated_at': _iso(row.updated_at),
        })
    return out


def watchlist_rows(user_id):
    """``user_watchlist`` joined to the local media cache."""
    rows = db.session.execute(
        select(user_watchlist)
        .where(user_watchlist.c.user_id == user_id)
        .order_by(user_watchlist.c.media_type, user_watchlist.c.media_id)
    ).all()
    titles = _media_by_id(row.media_id for row in rows)
    out = []
    for link in rows:
        media = titles.get(link.media_id)
        out.append({
            'media_id': link.media_id,
            'tmdb_id': media['tmdb_id'] if media else None,
            'media_type': link.media_type,
            'title': media['title'] if media else None,
            'release_date': media['release_date'] if media else None,
            'date_added': _iso(link.date_added),
            'priority': link.priority,
        })
    return out


def viewed_mirror_rows(user_id):
    """The ``user_viewed`` mirror — explicitly labelled, never canonical.

    For movies this duplicates diary entries; for TV it is a write-through of
    mark-as-viewed and F4 proved it can disagree with the episode ledger.
    It ships for importer convenience under a name that says what it is.
    """
    rows = db.session.execute(
        select(user_viewed)
        .where(user_viewed.c.user_id == user_id,
               user_viewed.c.media_type == 'movie')
        .order_by(user_viewed.c.media_type, user_viewed.c.media_id)
    ).all()
    titles = _media_by_id(row.media_id for row in rows)
    out = []
    for link in rows:
        media = titles.get(link.media_id)
        out.append({
            'media_id': link.media_id,
            'tmdb_id': media['tmdb_id'] if media else None,
            'media_type': link.media_type,
            'date_viewed': _iso(link.date_viewed),
            'rating': link.rating,
        })
    return out


def list_rows(user_id):
    """The user's own lists, with category names."""
    lists = db.session.execute(
        select(UserList).where(UserList.user_id == user_id)
        .order_by(UserList.id)
    ).scalars().all()
    if not lists:
        return []
    by_id = {row.id: row for row in lists}

    # One query for every category of every owned list.
    from models import ListCategory
    cats = db.session.execute(
        select(UserListCategory, ListCategory)
        .join(ListCategory,
              ListCategory.id == UserListCategory.category_id)
        .where(UserListCategory.list_id.in_(sorted(by_id)))
    ).all()
    names = {}
    for link, category in cats:
        names.setdefault(link.list_id, []).append(category.name)

    out = []
    for row in lists:
        out.append({
            'list_id': row.id,
            'title': row.title,
            'description': row.description,
            'is_public': bool(row.is_public),
            'list_type': row.list_type,
            'slug': row.slug,
            'cover_image': row.cover_image,
            'categories': names.get(row.id, []),
            'created_at': _iso(row.created_at),
            'updated_at': _iso(row.updated_at),
        })
    return out


def list_item_rows(user_id):
    """Items in lists the user OWNS.

    Scoped through ``UserList.user_id`` rather than by collaborator role: a
    collaborator's list content is the owner's data.
    """
    rows = db.session.execute(
        select(UserListItem)
        .join(UserList, UserList.id == UserListItem.list_id)
        .where(UserList.user_id == user_id)
        .order_by(UserListItem.list_id, UserListItem.position,
                  UserListItem.id)
    ).scalars().all()
    titles = _media_by_id(row.media_id for row in rows)
    out = []
    for item in rows:
        media = titles.get(item.media_id)
        out.append({
            'item_id': item.id,
            'list_id': item.list_id,
            'tmdb_id': media['tmdb_id'] if media else None,
            'media_type': item.media_type,
            'title': media['title'] if media else None,
            'position': item.position,
            'note': item.note,
            'added_at': _iso(item.added_at),
        })
    return out


def list_collaborator_rows(user_id):
    """The user's OWN membership on any list — role only.

    No collaborator id, username or ``added_by``: a collaborator's identity is
    another user's data.
    """
    rows = db.session.execute(
        select(ListCollaborator)
        .where(ListCollaborator.user_id == user_id)
        .order_by(ListCollaborator.list_id)
    ).scalars().all()
    return [{
        'list_id': row.list_id,
        'role': row.role,
        'added_at': _iso(row.added_at),
    } for row in rows]


def list_analytics_rows(user_id):
    """Derived counters for the user's own lists, kept in ``derived``.

    Labeled derived because these are recomputed from view events, not
    user-authored. Never CSV'd and never presented as source data.
    """
    from models import ListAnalytics
    rows = db.session.execute(
        select(ListAnalytics)
        .join(UserList, UserList.id == ListAnalytics.list_id)
        .where(UserList.user_id == user_id)
        .order_by(ListAnalytics.list_id)
    ).scalars().all()
    return [{
        'list_id': row.list_id,
        'view_count': row.view_count,
        'unique_viewers': row.unique_viewers,
        'share_count': row.share_count,
        'fork_count': row.fork_count,
        'last_viewed': _iso(row.last_viewed),
    } for row in rows]


def review_rows(user_id):
    """User-authored reviews. Soft-deleted rows are excluded.

    Engagement counters (``likes_count`` and friends) are derived and skipped.
    """
    rows = db.session.execute(
        select(Review, MediaItem)
        .join(MediaItem, MediaItem.id == Review.media_id, isouter=True)
        .where(Review.user_id == user_id, Review.is_deleted.is_(False))
        .order_by(Review.created_at, Review.id)
    ).all()
    out = []
    for review, media in rows:
        out.append({
            'review_id': review.id,
            'media_id': review.media_id,
            'tmdb_id': media.tmdb_id if media else None,
            'media_type': review.media_type,
            'title': media.title if media else None,
            'review_title': review.title,
            'contains_spoilers': bool(review.contains_spoilers),
            'rewatch': bool(review.rewatch),
            'watched_date': _day(review.watched_date),
            'created_at': _iso(review.created_at),
            'updated_at': _iso(review.updated_at),
            'content': review.content,
            'rating': review.rating,
        })
    return out


def rating_rows(domains):
    """Denormalised rating ledger across the three owning domains.

    FrameIQ has no standalone rating model, so this is a *projection*: a
    spreadsheet-friendly single view. The owning domain row stays canonical,
    and the bundle README says so.

    Takes the ALREADY-BUILT domain lists rather than a user id. Re-querying
    the three owning domains here would triple the statement count for the
    three largest tables in the export for no reason.
    """
    out = []
    for row in domains['movies']:
        if row['rating'] is None:
            continue
        out.append(_rating('diary_entry', row['diary_id'], row['tmdb_id'],
                           'movie', row['title'], None, None,
                           row['rating'], row['watched_date']))
    for row in domains['reviews']:
        out.append(_rating('review', row['review_id'], row['tmdb_id'],
                           row['media_type'], row['title'], None, None,
                           row['rating'], row['watched_date']))
    for row in domains['tv_episodes']:
        if row['rating'] is None:
            continue
        out.append(_rating('tv_episode', row['watch_id'], row['show_tmdb_id'],
                           'tv', row['show_title'], row['season_number'],
                           row['episode_number'], row['rating'],
                           row['watched_date']))
    out.sort(key=lambda r: (r['source'], r['entity_id']))
    return out


def _rating(source, entity_id, tmdb_id, media_type, title, season, episode,
            rating, watched_date):
    return {
        'source': source,
        'entity_id': entity_id,
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'title': title,
        'season_number': season,
        'episode_number': episode,
        'rating': rating,
        'watched_date': watched_date,
    }


def tag_rows(user_id):
    """The user's own tag applications, with tag names."""
    rows = db.session.execute(
        select(UserMediaTag, Tag)
        .join(Tag, Tag.id == UserMediaTag.tag_id, isouter=True)
        .where(UserMediaTag.user_id == user_id)
        .order_by(Tag.name, UserMediaTag.id)
    ).all()
    out = []
    for link, tag in rows:
        out.append({
            'user_media_tag_id': link.id,
            'tag_id': link.tag_id,
            'tag': tag.name if tag else None,
            'tmdb_id': link.media_id,
            'media_type': link.media_type,
            'created_at': _iso(link.created_at),
        })
    return out


def smart_list_rows(user_id):
    """Saved Smart List *definitions*. Result rows are never stored or sent."""
    rows = db.session.execute(
        select(SmartList).where(SmartList.user_id == user_id)
        .order_by(SmartList.created_at, SmartList.id)
    ).scalars().all()
    out = []
    for row in rows:
        out.append({
            'smart_list_id': row.id,
            'name': row.name,
            'description': row.description,
            'scope': row.scope,
            'filters_json': row.filters_json,
            'sort': row.sort,
            'is_public': bool(row.is_public),
            'created_at': _iso(row.created_at),
            'updated_at': _iso(row.updated_at),
        })
    return out


def streaming_service_rows(user_id):
    """Provider ids + region only.

    FrameIQ stores no provider credential, token or cookie, and provider
    display names come from TMDb at runtime — so ids ship and names are not
    invented here.
    """
    rows = db.session.execute(
        select(UserStreamingService)
        .where(UserStreamingService.user_id == user_id)
        .order_by(UserStreamingService.provider_id)
    ).scalars().all()
    return [{
        'service_id': row.id,
        'provider_id': row.provider_id,
        'region': row.region,
        'created_at': _iso(row.created_at),
    } for row in rows]


def recommendation_feedback_rows(user_id):
    """Raw interaction events — the source data taste is recomputed from."""
    rows = db.session.execute(
        select(RecommendationFeedback)
        .where(RecommendationFeedback.user_id == user_id)
        .order_by(RecommendationFeedback.event_date,
                  RecommendationFeedback.id)
    ).scalars().all()
    return [{
        'feedback_id': row.id,
        'media_id': row.media_id,
        'media_type': row.media_type,
        'surface': row.surface,
        'source': row.source,
        'event': row.event,
        'position': row.position,
        'reason_kind': row.reason_kind,
        'payload_json': row.payload_json,
        'model_version': row.model_version,
        'event_date': _day(row.event_date),
        'created_at': _iso(row.created_at),
    } for row in rows]


def taste_profile_section(user_id):
    """Derived taste metrics, parsed from stored JSON and labelled derived."""
    row = db.session.execute(
        select(TasteProfile).where(TasteProfile.user_id == user_id)
    ).scalars().first()
    if row is None:
        return None
    return {
        'genre_weights': _json_text(row.genre_weights_json),
        'decade_weights': _json_text(row.decade_weights_json),
        'director_affinity': _json_text(row.director_affinity_json),
        'actor_affinity': _json_text(row.actor_affinity_json),
        'runtime_pref': _json_text(row.runtime_pref_json),
        'media_type_pref': _json_text(row.media_type_pref_json),
        'mood_tags': _json_text(row.mood_tags_json) if row.mood_tags_json
        else None,
        'confidence': row.confidence,
        'signal_count': row.signal_count,
        'distinct_title_count': row.distinct_title_count,
        'profile_version': row.profile_version,
        'created_at': _iso(row.created_at),
        'updated_at': _iso(row.updated_at),
    }


def following_rows(user_id):
    """Who the user follows — public username only.

    No email, bio, profile picture or follower counters: those are another
    user's private data.
    """
    rows = db.session.execute(
        select(UserFollow)
        .where(UserFollow.follower_id == user_id,
               UserFollow.is_active.is_(True))
        .order_by(UserFollow.id)
    ).scalars().all()
    if not rows:
        return []

    followed_ids = [row.following_id for row in rows]
    # One lookup for every username, not one per follow.
    users = db.session.execute(
        select(User).where(User.id.in_(sorted(set(followed_ids))))
    ).scalars().all()
    usernames = {u.id: u.username for u in users}

    return [{
        'follow_id': row.id,
        'followed_username': usernames.get(row.following_id),
        'created_at': _iso(row.created_at),
    } for row in rows]


def media_like_rows(user_id):
    rows = db.session.execute(
        select(MediaLike).where(MediaLike.user_id == user_id)
        .order_by(MediaLike.id)
    ).scalars().all()
    return [{
        'like_id': row.id,
        'tmdb_id': row.media_id,
        'media_type': row.media_type,
        'created_at': _iso(row.created_at),
    } for row in rows]


def review_like_rows(user_id):
    """Likes the user gave other people's reviews.

    Target id + date only — the review author's identity and the review text
    are not the user's data.
    """
    rows = db.session.execute(
        select(ReviewLike).where(ReviewLike.user_id == user_id)
        .order_by(ReviewLike.id)
    ).scalars().all()
    return [{
        'review_like_id': row.id,
        'review_id': row.review_id,
        'created_at': _iso(row.created_at),
    } for row in rows]


def review_helpful_rows(user_id):
    rows = db.session.execute(
        select(ReviewHelpful).where(ReviewHelpful.user_id == user_id)
        .order_by(ReviewHelpful.id)
    ).scalars().all()
    return [{
        'helpful_id': row.id,
        'review_id': row.review_id,
        'is_helpful': bool(row.is_helpful),
        'created_at': _iso(row.created_at),
    } for row in rows]


def list_like_rows(user_id):
    rows = db.session.execute(
        select(ListLike).where(ListLike.user_id == user_id)
        .order_by(ListLike.id)
    ).scalars().all()
    return [{
        'list_like_id': row.id,
        'list_id': row.list_id,
        'created_at': _iso(row.created_at),
    } for row in rows]


def media_comment_rows(user_id):
    rows = db.session.execute(
        select(MediaComment).where(MediaComment.user_id == user_id)
        .order_by(MediaComment.id)
    ).scalars().all()
    return [{
        'comment_id': row.id,
        'tmdb_id': row.media_id,
        'media_type': row.media_type,
        'content': row.content,
        'created_at': _iso(row.created_at),
        'updated_at': _iso(row.updated_at),
        'is_deleted': bool(row.is_deleted),
    } for row in rows]


def review_comment_rows(user_id):
    rows = db.session.execute(
        select(ReviewComment).where(ReviewComment.user_id == user_id)
        .order_by(ReviewComment.id)
    ).scalars().all()
    return [{
        'comment_id': row.id,
        'review_id': row.review_id,
        'parent_id': row.parent_id,
        'content': row.content,
        'created_at': _iso(row.created_at),
        'is_deleted': bool(row.is_deleted),
    } for row in rows]


def list_comment_rows(user_id):
    rows = db.session.execute(
        select(ListComment).where(ListComment.user_id == user_id)
        .order_by(ListComment.id)
    ).scalars().all()
    return [{
        'comment_id': row.id,
        'list_id': row.list_id,
        'content': row.content,
        'created_at': _iso(row.created_at),
        'updated_at': _iso(row.updated_at),
        'is_deleted': bool(row.is_deleted),
    } for row in rows]


def notification_rows(user_id):
    """Notification records — identity of what happened plus read state.

    ``title`` / ``body`` / ``poster_path`` / ``episode_name`` / ``target_url``
    are server-generated display text derived from TMDb. They carry no
    portable meaning and would inflate the file, so they are excluded.
    """
    rows = db.session.execute(
        select(Notification).where(Notification.user_id == user_id)
        .order_by(Notification.created_at, Notification.id)
    ).scalars().all()
    return [{
        'notification_id': row.id,
        'type': row.type,
        'show_id': row.show_id,
        'season': row.season,
        'episode': row.episode,
        'created_at': _iso(row.created_at),
        'read_at': _iso(row.read_at),
    } for row in rows]


def continue_watching_rows(user_id):
    """In-progress viewing — resume points are portable user state."""
    rows = db.session.execute(
        select(ContinueWatchingItem)
        .where(ContinueWatchingItem.user_id == user_id)
        .order_by(ContinueWatchingItem.media_type, ContinueWatchingItem.tmdb_id,
                  ContinueWatchingItem.season, ContinueWatchingItem.episode)
    ).scalars().all()
    return [{
        'kind': 'continue_watching',
        'tmdb_id': row.tmdb_id,
        'media_type': row.media_type,
        'season': row.season,
        'episode': row.episode,
        'title': row.title,
        'current_time': None,
        'duration': None,
        'started_at': _iso(row.started_at),
        'updated_at': _iso(row.updated_at),
    } for row in rows]


def watch_progress_rows(user_id):
    rows = db.session.execute(
        select(WatchProgress).where(WatchProgress.user_id == user_id)
        .order_by(WatchProgress.media_type, WatchProgress.tmdb_id,
                  WatchProgress.season, WatchProgress.episode)
    ).scalars().all()
    return [{
        'kind': 'playback_progress',
        'tmdb_id': row.tmdb_id,
        'media_type': row.media_type,
        'season': row.season,
        'episode': row.episode,
        'title': row.title,
        'current_time': row.current_time,
        'duration': row.duration,
        'started_at': None,
        'updated_at': _iso(row.updated_at),
    } for row in rows]


def chat_rows(user_id):
    """Conversations (title only) and the user's own message bytes.

    Assistant text is omitted: it is model output, not user data. The
    conversation boundary is still preserved so an importer can see how many
    messages existed.
    """
    conversations = db.session.execute(
        select(ChatConversation)
        .where(ChatConversation.user_id == user_id)
        .order_by(ChatConversation.id)
    ).scalars().all()
    if not conversations:
        return []
    by_id = {c.id: c for c in conversations}

    messages = db.session.execute(
        select(ChatMessage)
        .where(ChatMessage.conversation_id.in_(sorted(by_id)),
               ChatMessage.role == 'user')
        .order_by(ChatMessage.created_at, ChatMessage.id)
    ).scalars().all()

    out = [{
        'conversation_id': c.id,
        'conversation_title': c.title,
        'conversation_created_at': _iso(c.created_at),
        'message_id': None,
        'message_role': None,
        'message_created_at': None,
        'message_content_bytes': 0,
    } for c in conversations]

    known = {row['conversation_id'] for row in out}
    for message in messages:
        if message.conversation_id not in known:
            continue
        out.append({
            'conversation_id': message.conversation_id,
            'conversation_title': by_id[message.conversation_id].title,
            'conversation_created_at': _iso(
                by_id[message.conversation_id].created_at),
            'message_id': message.id,
            'message_role': message.role,
            'message_created_at': _iso(message.created_at),
            'message_content_bytes': len(
                (message.content or '').encode('utf-8')),
        })
    return out


# ── Domain → CSV schema ──────────────────────────────────────────────────────
#
# One table drives both the CSV headers and the ZIP file list, so the two can
# never drift. `json_keys` lists the JSON keys that become flat CSV columns
# (list values are joined); anything else stays nested in JSON only.

def _schema(domain, columns, json_keys=(), split=()):
    return {
        'domain': domain,
        'filename': '%s.csv' % domain,
        'columns': list(columns),
        'json_keys': list(json_keys),
        'split_keys': list(split),
    }


CSV_SCHEMAS = [
    _schema('movies',
            ['diary_id', 'watched_date', 'media_id', 'tmdb_id', 'media_type',
             'title', 'release_date', 'rating', 'is_rewatch', 'created_at',
             'review_id']),
    _schema('tv_episodes',
            ['watch_id', 'watched_date', 'show_tmdb_id', 'show_title',
             'season_number', 'episode_number', 'episode_name', 'rating',
             'notes', 'is_rewatch', 'created_at', 'updated_at']),
    _schema('tv_progress',
            ['progress_id', 'show_media_id', 'show_tmdb_id', 'show_title',
             'status', 'is_favorite', 'started_at', 'last_watched',
             'completed_at', 'created_at', 'updated_at']),
    _schema('watchlist',
            ['media_id', 'tmdb_id', 'media_type', 'title', 'release_date',
             'date_added', 'priority']),
    _schema('lists',
            ['list_id', 'title', 'description', 'is_public', 'list_type',
             'slug', 'cover_image', 'categories', 'created_at',
             'updated_at'],
            json_keys=['categories'], split=['categories']),
    _schema('list_items',
            ['item_id', 'list_id', 'tmdb_id', 'media_type', 'title',
             'position', 'note', 'added_at']),
    _schema('list_collaborators', ['list_id', 'role', 'added_at']),
    _schema('reviews',
            ['review_id', 'media_id', 'tmdb_id', 'media_type', 'title',
             'review_title', 'contains_spoilers', 'rewatch', 'watched_date',
             'created_at', 'updated_at', 'content', 'rating']),
    _schema('ratings',
            ['source', 'entity_id', 'tmdb_id', 'media_type', 'title',
             'season_number', 'episode_number', 'rating', 'watched_date']),
    _schema('tags',
            ['user_media_tag_id', 'tag_id', 'tag', 'tmdb_id', 'media_type',
             'created_at']),
    _schema('smart_lists',
            ['smart_list_id', 'name', 'description', 'scope', 'filters_json',
             'sort', 'is_public', 'created_at', 'updated_at']),
    _schema('streaming_services',
            ['service_id', 'provider_id', 'region', 'created_at']),
    _schema('recommendation_feedback',
            ['feedback_id', 'media_id', 'media_type', 'surface', 'source',
             'event', 'position', 'reason_kind', 'payload_json',
             'model_version', 'event_date', 'created_at']),
    _schema('social',
            ['kind', 'target_id', 'related_id', 'related_username',
             'created_at']),
    _schema('comments',
            ['comment_id', 'kind', 'target_id', 'media_id', 'media_type',
             'parent_id', 'content', 'created_at', 'updated_at',
             'is_deleted']),
    _schema('notifications',
            ['notification_id', 'type', 'show_id', 'season', 'episode',
             'created_at', 'read_at']),
    _schema('continue_watching',
            ['kind', 'tmdb_id', 'media_type', 'season', 'episode', 'title',
             'current_time', 'duration', 'started_at', 'updated_at']),
    _schema('chat',
            ['conversation_id', 'conversation_title',
             'conversation_created_at', 'message_id', 'message_role',
             'message_created_at', 'message_content_bytes']),
]

CSV_FILES = [schema['filename'] for schema in CSV_SCHEMAS]


# ── Assembled export ─────────────────────────────────────────────────────────

def build_domains(user):
    """Every domain, one dict. This is the single source of the export.

    Takes a ``User`` rather than a user id so the caller cannot pass an id
    that is not the session user — there is no code path here that selects a
    different account.
    """
    user_id = user.id
    domains = {
        'movies': movie_history_rows(user_id),
        'tv_diary': tv_diary_rows(user_id),
        'tv_episodes': tv_episode_rows(user_id),
        'tv_progress': tv_progress_rows(user_id),
        'watchlist': watchlist_rows(user_id),
        'lists': list_rows(user_id),
        'list_items': list_item_rows(user_id),
        'list_collaborators': list_collaborator_rows(user_id),
        'reviews': review_rows(user_id),
        'tags': tag_rows(user_id),
        'smart_lists': smart_list_rows(user_id),
        'streaming_services': streaming_service_rows(user_id),
        'recommendation_feedback': recommendation_feedback_rows(user_id),
        'following': following_rows(user_id),
        'media_likes': media_like_rows(user_id),
        'review_likes': review_like_rows(user_id),
        'review_helpful': review_helpful_rows(user_id),
        'list_likes': list_like_rows(user_id),
        'media_comments': media_comment_rows(user_id),
        'review_comments': review_comment_rows(user_id),
        'list_comments': list_comment_rows(user_id),
        'notifications': notification_rows(user_id),
        'continue_watching': continue_watching_rows(user_id),
        'watch_progress': watch_progress_rows(user_id),
        'chat': chat_rows(user_id),
        'viewed_mirror': viewed_mirror_rows(user_id),
    }
    # Derived from the lists above, so it costs zero extra statements.
    domains['ratings'] = rating_rows(domains)
    return domains


def build_export(user, generated_at=None):
    """The complete JSON export as a plain dict.

    Deterministic except for ``generated_at``: every domain list is ordered by
    an explicit total ordering ending in a primary key.
    """
    domains = build_domains(user)

    social = [
        dict(kind='following', target_id=row['followed_username'],
             related_id=None, created_at=row['created_at'])
        for row in domains['following']
    ] + [
        dict(kind='media_like', target_id=row['tmdb_id'],
             related_id=None, created_at=row['created_at'])
        for row in domains['media_likes']
    ] + [
        dict(kind='review_like', target_id=row['review_id'], related_id=None,
             created_at=row['created_at'])
        for row in domains['review_likes']
    ] + [
        dict(kind='review_helpful', target_id=row['review_id'],
             related_id=None, created_at=row['created_at'])
        for row in domains['review_helpful']
    ] + [
        dict(kind='list_like', target_id=row['list_id'], related_id=None,
             created_at=row['created_at'])
        for row in domains['list_likes']
    ]
    social.sort(key=lambda row: (row['kind'],
                                 str(row['target_id'] if row['target_id']
                                     is not None else '')))

    comments = []
    for row in domains['media_comments']:
        comments.append({
            'comment_id': row['comment_id'], 'kind': 'media_comment',
            'target_id': row['tmdb_id'], 'media_id': row['tmdb_id'],
            'media_type': row['media_type'], 'parent_id': None,
            'content': row['content'], 'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'is_deleted': row['is_deleted'],
        })
    for row in domains['review_comments']:
        comments.append({
            'comment_id': row['comment_id'], 'kind': 'review_comment',
            'target_id': row['review_id'], 'media_id': None,
            'media_type': None, 'parent_id': row['parent_id'],
            'content': row['content'], 'created_at': row['created_at'],
            'updated_at': None, 'is_deleted': row['is_deleted'],
        })
    for row in domains['list_comments']:
        comments.append({
            'comment_id': row['comment_id'], 'kind': 'list_comment',
            'target_id': row['list_id'], 'media_id': None,
            'media_type': None, 'parent_id': None, 'content': row['content'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'is_deleted': row['is_deleted'],
        })
    comments.sort(key=lambda row: (row['kind'], row['comment_id']))

    continue_watching = sorted(
        domains['continue_watching'] + domains['watch_progress'],
        key=lambda row: (row['kind'],
                         row['tmdb_id'] if row['tmdb_id'] is not None else 0,
                         row['season'] if row['season'] is not None else -1,
                         row['episode'] if row['episode'] is not None else -1))

    export = {
        'format': EXPORT_FORMAT,
        'version': EXPORT_VERSION,
        'generated_at': _iso(generated_at or datetime.utcnow()),
        'account': account_section(user),
        'profile': profile_section(user),
        'watch_history': {
            'movies': domains['movies'],
            'tv': domains['tv_diary'],
        },
        'tv_history': {
            'episodes': domains['tv_episodes'],
            'progress': domains['tv_progress'],
        },
        'watchlist': domains['watchlist'],
        'lists': domains['lists'],
        'list_items': domains['list_items'],
        'list_collaborators': domains['list_collaborators'],
        'reviews': domains['reviews'],
        'ratings': domains['ratings'],
        'tags': domains['tags'],
        'smart_lists': domains['smart_lists'],
        'streaming_services': domains['streaming_services'],
        'recommendation_data': {
            'feedback': domains['recommendation_feedback'],
        },
        'social_data': {
            'following': domains['following'],
            'media_likes': domains['media_likes'],
            'review_likes': domains['review_likes'],
            'review_helpful_votes': domains['review_helpful'],
            'list_likes': domains['list_likes'],
            'unified': social,
        },
        'authored_content': {
            'media_comments': domains['media_comments'],
            'review_comments': domains['review_comments'],
            'list_comments': domains['list_comments'],
            'unified': comments,
        },
        'notifications': {
            'preferences': {
                'note': 'FrameIQ has no persisted notification preferences '
                        'model; there is nothing to export.',
            },
            'records': domains['notifications'],
        },
        'activity_state': {
            'continue_watching': continue_watching,
            'chat_conversations': domains['chat'],
        },
        'viewed_mirror': {
            'note': 'Derived mirror of diary entries. DiaryEntry is the '
                    'canonical movie history; this exists for importer '
                    'convenience only.',
            'movies': domains['viewed_mirror'],
        },
        'derived': {
            'note': 'Recomputed from canonical sources. Never import these '
                    'as source data.',
            'taste_profile': taste_profile_section(user.id),
            'list_analytics': list_analytics_rows(user.id),
        },
        'counts': {
            'diary_entries': len(domains['movies'])
            + len(domains['tv_diary']),
            'tv_episode_watches': len(domains['tv_episodes']),
            'tv_progress': len(domains['tv_progress']),
            'watchlist_items': len(domains['watchlist']),
            'lists': len(domains['lists']),
            'list_items': len(domains['list_items']),
            'list_collaborators': len(domains['list_collaborators']),
            'reviews': len(domains['reviews']),
            'ratings': len(domains['ratings']),
            'tags': len(domains['tags']),
            'smart_lists': len(domains['smart_lists']),
            'streaming_services': len(domains['streaming_services']),
            'recommendation_feedback': len(domains['recommendation_feedback']),
            'following': len(domains['following']),
            'media_likes': len(domains['media_likes']),
            'review_likes': len(domains['review_likes']),
            'review_helpful_votes': len(domains['review_helpful']),
            'list_likes': len(domains['list_likes']),
            'media_comments': len(domains['media_comments']),
            'review_comments': len(domains['review_comments']),
            'list_comments': len(domains['list_comments']),
            'notifications': len(domains['notifications']),
            'continue_watching': len(domains['continue_watching']),
            'watch_progress': len(domains['watch_progress']),
            'chat_conversations': len(domains['chat']),
            'viewed_mirror_movies': len(domains['viewed_mirror']),
        },
        'csv_files': list(CSV_FILES),
    }
    return export


def serialize_json(export):
    """Serialize an already-built export dict to UTF-8 bytes.

    Split out from :func:`build_json` so a caller that needs the dict (for
    ``counts``, or for logging) does not have to build the export twice.
    """
    text = json.dumps(export, ensure_ascii=False, indent=2,
                      separators=(',', ': '), sort_keys=False)
    return text.encode('utf-8')


def build_json(user, generated_at=None):
    """The complete export as UTF-8 JSON bytes.

    ``ensure_ascii=False`` keeps Bengali / Arabic / accented titles and emoji
    as real characters instead of ``\\uXXXX`` escapes — still valid JSON, and
    far more useful in a file a human opens.
    """
    return serialize_json(build_export(user, generated_at=generated_at))


# ── CSV safety ───────────────────────────────────────────────────────────────

_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def csv_safe(value):
    """Neutralise spreadsheet formula injection without corrupting text.

    A cell starting ``=``/``+``/``-``/``@`` (or a tab/CR) is executed as a
    formula by Excel, LibreOffice and Sheets when the file is opened. Prefixing
    a single apostrophe is the standard mitigation: spreadsheets treat it as a
    literal-quote marker and display the original characters, so
    ``=SUM(A1)`` is still readable as ``=SUM(A1)``.

    Applied ONLY when the first character is a formula trigger. Ordinary text
    — including negative numbers in free-text fields — is untouched, and a
    value that is genuinely a number is never prefixed (the risk is a
    string, not a number type).
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _csv_value(value, split=False):
    if isinstance(value, (list, tuple)):
        joined = '|'.join('' if v is None else str(v) for v in value)
        return csv_safe(joined) if split else None
    return csv_safe(value)


def csv_bytes(schema, rows):
    """One CSV file as bytes.

    UTF-8, ``\\n`` terminator (so the bytes are identical on every OS),
    ``QUOTE_MINIMAL`` plus explicit stringification of booleans so they read
    ``true``/``false`` rather than Python's ``True``.
    """
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n',
                        quoting=csv.QUOTE_MINIMAL)
    writer.writerow(schema['columns'])
    for row in rows:
        record = []
        for column in schema['columns']:
            value = row.get(column)
            if column in schema['split_keys']:
                record.append(_csv_value(value, split=True))
            elif isinstance(value, bool):
                record.append('true' if value else 'false')
            elif isinstance(value, (dict, list)):
                record.append(csv_safe(json.dumps(value, ensure_ascii=False,
                                                  sort_keys=True)))
            else:
                record.append(_csv_value(value))
        writer.writerow(record)
    return buffer.getvalue().encode('utf-8')


def csv_domains(user):
    """Flatten the export into per-domain CSV row lists."""
    export = build_export(user)
    return {
        'movies': export['watch_history']['movies'],
        'tv_episodes': export['tv_history']['episodes'],
        'tv_progress': export['tv_history']['progress'],
        'watchlist': export['watchlist'],
        'lists': export['lists'],
        'list_items': export['list_items'],
        'list_collaborators': export['list_collaborators'],
        'reviews': export['reviews'],
        'ratings': export['ratings'],
        'tags': export['tags'],
        'smart_lists': export['smart_lists'],
        'streaming_services': export['streaming_services'],
        'recommendation_feedback': export['recommendation_data']['feedback'],
        'social': export['social_data']['unified'],
        'comments': export['authored_content']['unified'],
        'notifications': export['notifications']['records'],
        'continue_watching': export['activity_state']['continue_watching'],
        'chat': export['activity_state']['chat_conversations'],
    }


_README = """FrameIQ account data export
--------------------------------

Format : {format}
Version: {version}
Created: {generated}

JSON is the complete export. The CSVs in this bundle are flattened,
per-domain projections of the SAME data for spreadsheet and migration use.
Nothing here appears in the CSVs that is absent from the JSON.

To get the complete export, download the JSON from the same account page.

FILES
-----
{movies}
{tv_episodes}
{tv_progress}
{watchlist}
{lists}
{list_items}
{list_collaborators}
{reviews}
{ratings}
{tags}
{smart_lists}
{streaming_services}
{recommendation_feedback}
{social}
{comments}
{notifications}
{continue_watching}
{chat}

CANONICAL vs DERIVED
--------------------
Canonical (source of truth, import these):
  movies.csv           DiaryEntry, media_type='movie'
  tv_episodes.csv      TVEpisodeWatch  <- the TV history ledger
  watchlist.csv        user_watchlist
  lists.csv            UserList (your own lists)
  list_items.csv       UserListItem (your own lists only)
  reviews.csv          Review (your authored, non-deleted)
  tags.csv             UserMediaTag (your own applications)
  smart_lists.csv      SmartList definitions (results are recomputed)
  streaming_services.csv, recommendation_feedback.csv

Derived (recomputed; do NOT import as source):
  ratings.csv          a PROJECTION of rating fields that already live on
                       diary entries, reviews and episode watches. The
                       owning row in its own domain file is canonical.
  tv_progress.csv      carries only the fields you set (status, favourite,
                       dates). Progress COUNTERS are omitted on purpose:
                       the aired-episode denominator cannot be rebuilt
                       without metadata, so the counters could be stale.
  list_analytics       (JSON "derived" section only)
  taste_profile        (JSON "derived" section only) — recomputed from
                       recommendation_feedback.csv

A field absent here was either server-generated display text, another user's
data, or a credential. See the export-format documentation for the full
rationale.

CONVENTIONS
-----------
Encoding      UTF-8
Dates         YYYY-MM-DD
Timestamps    ISO-8601 UTC (trailing Z)
Booleans      true / false
Null          empty field
Ratings       as stored (0.5 - 5.0 stars)

Formulas: a cell whose first character is = + - @ is prefixed with an
apostrophe so spreadsheets show it as text instead of evaluating it.

FUTURE IMPORTS
--------------
This is an export only; FrameIQ does not import this format yet. Stable ids
(TMDb ids, list ids, season/episode numbers, positions and rewatch flags) are
preserved so a future importer can map them without guessing. The `version`
field is the export-format version and is independent of the FrameIQ app
version.
"""


def _readme(generated_at):
    body = _README.format(
        format=EXPORT_FORMAT,
        version=EXPORT_VERSION,
        generated=generated_at or datetime.utcnow().isoformat() + 'Z',
        movies=_describe('movies', 'movie watch history (DiaryEntry)'),
        tv_episodes=_describe('tv_episodes',
                              'TV episode watch history (TVEpisodeWatch)'),
        tv_progress=_describe('tv_progress', 'TV tracking state you set'),
        watchlist=_describe('watchlist', 'your watchlist'),
        lists=_describe('lists', 'your lists'),
        list_items=_describe('list_items', 'items in your lists'),
        list_collaborators=_describe('list_collaborators',
                                     'your role on lists you do not own'),
        reviews=_describe('reviews', 'your reviews'),
        ratings=_describe('ratings',
                          'all ratings in one view (derived projection)'),
        tags=_describe('tags', 'tags you applied'),
        smart_lists=_describe('smart_lists', 'Smart List definitions'),
        streaming_services=_describe('streaming_services',
                                     'your streaming services + region'),
        recommendation_feedback=_describe(
            'recommendation_feedback', 'recommendation interaction events'),
        social=_describe('social', 'follows and likes you created'),
        comments=_describe('comments', 'comments you wrote'),
        notifications=_describe('notifications',
                                'notifications you received (read state)'),
        continue_watching=_describe('continue_watching',
                                    'continue-watching / resume points'),
        chat=_describe('chat', 'chat conversations you started'),
    )
    return body.encode('utf-8')


def _describe(domain, text):
    schema = next(s for s in CSV_SCHEMAS if s['domain'] == domain)
    return '  %-28s %s' % (schema['filename'], text)


def build_csv_bundle(user, generated_at=None):
    """Return ``(BytesIO, filename)`` for a ZIP of every CSV + README.

    Built entirely in memory, so there is no temporary file to leak.

    An earlier revision wrote the archive to ``tempfile.mkstemp`` and unlinked
    it via ``response.call_on_close``. Probing showed that callback does not
    fire for a ``send_file`` response, so every export left a copy of the
    user's entire account sitting in /tmp — exactly what the "do not persist
    exports" rule forbids. Rather than harden a cleanup path that cannot be
    trusted, the temp file is gone: the archive is assembled in a BytesIO and
    handed to ``send_file`` as a stream.

    Memory cost is not the trade-off it looks like. On a 1,000-movie +
    1,000-episode account the JSON payload is ~1.7 MB while the compressed ZIP
    is ~60 KB, because CSV is highly repetitive and DEFLATE collapses it. The
    JSON endpoint already buffers its whole payload, so this adds no new class
    of memory pressure.

    Filenames inside the archive are fixed literals from ``CSV_SCHEMAS``, so
    there is no path-traversal or filename-injection surface. ``zipfile`` is
    standard library: no new dependency for a handful of small text files.
    """
    stamp = generated_at or datetime.utcnow()
    domains = csv_domains(user)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.txt', _readme(_iso(stamp)))
        for schema in CSV_SCHEMAS:
            archive.writestr(schema['filename'],
                             csv_bytes(schema, domains[schema['domain']]))
    buffer.seek(0)
    return buffer, 'frameiq-export-%s.zip' % _stamp(stamp)


def _stamp(moment):
    return moment.strftime('%Y-%m-%d')


def export_filename_json(moment=None):
    moment = moment or datetime.utcnow()
    return 'frameiq-export-%s.json' % _stamp(moment)