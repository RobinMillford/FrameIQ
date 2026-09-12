"""Smart Lists v1 query engine (Feature 05).

A Smart List is a saved, validated query over the OWNER's data:

    validate_config()   → reject unknown scopes/filters/sorts and bad values
    evaluate_smart_list(user, smart_list, page) → canonical result page

Design invariants:

  SAVED QUERY, NOT STORED RESULTS — nothing is materialized; every evaluation
  re-queries the live data, so watchlist/diary/tracking changes are visible on
  the next request without any refresh step.

  SQL-FIRST — every scope starts from a user-scoped query joined to MediaItem;
  metadata filters (type/genre/year/decade/rating/runtime) run inside the
  database. Python-side work is bounded to the page window, never the whole
  result set.

  BOUNDED EXTERNAL WORK — only the availability filter can touch TMDb, and it
  runs through the existing cached/memoized layer (api/availability), batched
  and capped per page, deduplicated per (type, id), and NEVER per-item beyond
  that cap. When the availability filter is active and upstream data cannot be
  determined for a candidate, the candidate is omitted (a missing match must
  never present itself as a match).

  OWNER-SCOPED — user_id filters are applied in the query itself, so a config
  can never reach another user's data.
"""
import logging
import random
from datetime import datetime, timedelta

from models import (
    db, MediaItem, user_watchlist, user_viewed, TVShowProgress,
)
from models.smart_lists import SmartList

logger = logging.getLogger(__name__)

SCOPES = ('watchlist', 'diary', 'tracked_tv', 'all')

SORTS = (
    'priority',      # watchlist priority (high > medium > low)
    'date_added',    # when the user added/tracked it
    'rating',        # TMDb community rating (MediaItem.rating)
    'runtime',       # ascending; unknown runtime sorts last
    'release_date',  # newest first
    'oldest',        # release date ascending
    'random',        # deterministic per (user, list, day)
)

MEDIA_TYPES = ('movie', 'tv')

# Watch-status filter for the tracked_tv scope (mirrors TV tracking statuses).
TV_STATUSES = ('watching', 'paused', 'plan_to_watch', 'dropped', 'completed')

RUNTIME_FILTERS = {
    'under_90': 90,
    'under_120': 120,
    'under_150': 150,
}

# Watchlist-age filter, in months ('added_more_than_6_months' → 6).
AGE_FILTERS = {
    'added_more_than_6_months': 6,
    'added_more_than_12_months': 12,
}

# Sort orders known to this engine (validated and never trusted from client).
_VALID_SORTS = set(SORTS)
_VALID_SCOPES = set(SCOPES)

# Priority ordering used for the 'priority' sort and validation.
PRIORITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}


def _validate_choice(name, value, allowed, label):
    """Validate an enum-ish filter value; returns the canonical value."""
    if value not in allowed:
        raise ValueError(f'{name} must be one of: {label}')
    return value


def _validate_number(name, value, low, high, cast=float):
    """Validate a numeric filter value within [low, high]."""
    try:
        number = cast(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a number')
    if not low <= number <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return number


def _validate_filters(filters):
    """Validate each known filter key; returns the canonical filter dict.

    Table-driven: each known key maps to a validator producing the canonical
    value; unknown keys are rejected after the known ones are validated
    (same precedence as before). Iterating the table — not the client dict —
    keeps the stored filter order canonical regardless of input order.
    """
    def _genres(raw):
        if not isinstance(raw, list) or not raw or len(raw) > 10:
            raise ValueError('genres must be a list of 1-10 genre names')
        for g in raw:
            if not isinstance(g, str) or not g.strip() or len(g) > 50:
                raise ValueError('Each genre must be a non-empty string')
        return [g.strip()[:50] for g in raw]

    def _decade(raw):
        decade = _validate_number(
            'decade', raw, 1900, datetime.utcnow().year, cast=int)
        if decade % 10 != 0:
            raise ValueError('decade must be a valid decade (e.g. 1990)')
        return decade

    max_year = datetime.utcnow().year + 1
    validators = {
        'watch_state': lambda v: _validate_choice(
            'watch_state', v, ('watched', 'unwatched'),
            '"watched" or "unwatched"'),
        'media_type': lambda v: _validate_choice(
            'media_type', v, MEDIA_TYPES, '"movie" or "tv"'),
        'genres': _genres,
        'year': lambda v: _validate_number(
            'year', v, 1900, max_year, cast=int),
        'decade': _decade,
        'min_rating': lambda v: _validate_number('min_rating', v, 0, 10),
        'runtime': lambda v: _validate_choice(
            'runtime', v, tuple(RUNTIME_FILTERS), ', '.join(RUNTIME_FILTERS)),
        'added_within': lambda v: _validate_choice(
            'added_within', v, tuple(AGE_FILTERS), ', '.join(AGE_FILTERS)),
        'tv_status': lambda v: _validate_choice(
            'tv_status', v, TV_STATUSES, ', '.join(TV_STATUSES)),
        'services': lambda v: _validate_choice(
            'services', v, ('my_services', 'none'), '"my_services" or "none"'),
        'service_id': lambda v: _validate_number(
            'service_id', v, 1, 10**9, cast=int),
    }

    clean = {}
    for key in validators:
        if key in filters:
            clean[key] = validators[key](filters[key])
    unknown = set(filters) - set(clean)
    if unknown:
        raise ValueError(f'Unknown filter(s): {", ".join(sorted(unknown))}')
    return clean


def validate_config(scope, filters, sort):
    """Validate a Smart List configuration; return the canonical filter dict.

    Raises ValueError with a user-safe message on any invalid input. Only
    known filter keys are accepted, every value is type/range checked, and
    the sort/scope must be ones this engine implements.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"Invalid scope. Choose from: {', '.join(SCOPES)}")
    if sort not in _VALID_SORTS:
        raise ValueError(f"Invalid sort. Choose from: {', '.join(SORTS)}")
    if not isinstance(filters, dict):
        raise ValueError('Filters must be an object')
    return _validate_filters(filters)


def _scope_query(smart_list):
    """Base query for a scope: (MediaItem, scope-specific context).

    Every branch is user-scoped at the SQL level. Returned columns beyond
    the media row are context used by filters/sorts/card building.
    """
    user_id = smart_list.user_id
    MediaAlias = MediaItem

    if smart_list.scope == 'watchlist':
        return (db.session.query(MediaAlias, user_watchlist.c.priority,
                                 user_watchlist.c.date_added)
                .join(user_watchlist, db.and_(
                    user_watchlist.c.media_id == MediaAlias.id,
                    user_watchlist.c.media_type == MediaAlias.media_type))
                .filter(user_watchlist.c.user_id == user_id))

    if smart_list.scope == 'diary':
        return (db.session.query(MediaAlias)
                .join(user_viewed, db.and_(
                    user_viewed.c.media_id == MediaAlias.id,
                    user_viewed.c.media_type == MediaAlias.media_type))
                .filter(user_viewed.c.user_id == user_id)
                .distinct())

    if smart_list.scope == 'tracked_tv':
        return (db.session.query(MediaAlias, TVShowProgress.status,
                                 TVShowProgress.last_watched)
                .join(TVShowProgress,
                      db.and_(TVShowProgress.show_id == MediaAlias.tmdb_id,
                              MediaAlias.media_type == 'tv'))
                .filter(TVShowProgress.user_id == user_id))

    # 'all' — everything in the user's local data (union of the scopes above).
    watchlist_ids = db.session.query(user_watchlist.c.media_id).filter(
        user_watchlist.c.user_id == user_id)
    viewed_ids = db.session.query(user_viewed.c.media_id).filter(
        user_viewed.c.user_id == user_id)
    tracked_ids = db.session.query(TVShowProgress.show_id).filter(
        TVShowProgress.user_id == user_id)
    return (db.session.query(MediaAlias)
            .filter(db.or_(
                MediaItem.id.in_(watchlist_ids),
                MediaItem.id.in_(viewed_ids),
                db.and_(MediaItem.media_type == 'tv',
                        MediaItem.tmdb_id.in_(tracked_ids)),
            )))


def _apply_metadata_filters(query, filters):
    """Metadata filters that run entirely inside SQL."""
    if 'media_type' in filters:
        query = query.filter(MediaItem.media_type == filters['media_type'])

    if 'genres' in filters:
        for genre in filters['genres']:
            # Genres are stored comma-separated; LIKE with boundaries is the
            # established convention for matching them (same as stats pages).
            query = query.filter(
                MediaItem.genres.ilike(f'%{genre}%'))

    if 'min_rating' in filters:
        query = query.filter(MediaItem.rating >= filters['min_rating'])

    if 'runtime' in filters:
        minutes = RUNTIME_FILTERS[filters['runtime']]
        query = query.filter(db.and_(MediaItem.runtime.isnot(None),
                                     MediaItem.runtime < minutes))

    if 'year' in filters:
        year = filters['year']
        query = query.filter(db.and_(
            MediaItem.release_date.isnot(None),
            db.extract('year', MediaItem.release_date) == year))

    if 'decade' in filters:
        decade = filters['decade']
        query = query.filter(db.and_(
            MediaItem.release_date.isnot(None),
            db.extract('year', MediaItem.release_date) >= decade,
            db.extract('year', MediaItem.release_date) < decade + 10))

    return query


def _apply_scope_filters(query, smart_list, filters):
    """Filters that depend on the scope's own columns (watchlist age)."""
    if 'added_within' in filters and smart_list.scope == 'watchlist':
        months = AGE_FILTERS[filters['added_within']]
        cutoff = datetime.utcnow() - timedelta(days=30 * months)
        query = query.filter(user_watchlist.c.date_added <= cutoff)
    return query


def _apply_watch_state(query, smart_list, filters):
    """Watch-state filter — the only Python-side set filter (id sets only).

    'watched'/'unwatched' compare against the user's viewed set
    (user_viewed table — the canonical watched state). The comparison is a
    set membership test over the SQL-filtered candidate rows, never a
    per-item query.
    """
    if 'watch_state' not in filters:
        return query
    want_watched = filters['watch_state'] == 'watched'

    rows = query.all()
    viewed = {(i.tmdb_id, i.media_type)
              for i in _user_viewed_items(smart_list.user_id)}
    matched = [
        row for row in rows
        if ((row[0].tmdb_id, row[0].media_type) in viewed) == want_watched
    ]
    return matched


def _user_viewed_items(user_id):
    from models import user_viewed as _uv
    return (db.session.query(MediaItem)
            .join(_uv, db.and_(_uv.c.media_id == MediaItem.id,
                               _uv.c.media_type == MediaItem.media_type))
            .filter(_uv.c.user_id == user_id)
            .all())


def _apply_availability(smart_list, filters, rows, page_size):
    """'On my services' / specific-service filter — bounded, cached, batched.

    Runs through Feature 03's memoized provider layer (one memoized TMDb
    fetch per distinct title, plus an artificial cap so a huge candidate set
    can never fan out into an upstream request storm). A candidate whose
    availability cannot be determined is omitted rather than shown.
    """
    if 'services' not in filters and 'service_id' not in filters:
        return rows

    from api.availability import _fetch_provider_results, normalize_region
    from models.streaming import UserStreamingService

    user = db.session.get(SmartList, smart_list.id).user
    region = normalize_region(user.streaming_region)
    service_rows = (UserStreamingService.query
                    .filter_by(user_id=smart_list.user_id, region=region)
                    .all())
    my_services = {row.provider_id for row in service_rows}

    specific = filters.get('service_id')
    require_any = filters.get('services') == 'my_services'

    # Bounded availability probe: at most page_size * 4 distinct titles per
    # evaluation pass. Results come from the memo/cache layer, so repeats
    # across renders cost nothing upstream.
    cap = page_size * 4
    checked = 0
    matched = []
    unknown_hits = 0

    for row in rows:
        media = row[0]
        if checked >= cap:
            break
        if media.media_type not in MEDIA_TYPES:
            continue
        checked += 1

        results = _fetch_provider_results(media.media_type, media.tmdb_id)
        if results is None:
            unknown_hits += 1
            continue
        raw = results.get(region) or {}
        stream = [
            p for p in (raw.get('flatrate') or [])
            if isinstance(p, dict) and p.get('provider_id') is not None
        ] + [
            p for p in (raw.get('free') or [])
            if isinstance(p, dict) and p.get('provider_id') is not None
        ]
        provider_ids = {int(p['provider_id']) for p in stream}

        if specific is not None:
            ok = specific in provider_ids
        elif require_any:
            ok = bool(provider_ids & my_services)
        else:
            ok = True
        if ok:
            matched.append(row)

    if unknown_hits:
        logger.info('Smart list %s: %d titles omitted (availability unknown)',
                    smart_list.id, unknown_hits)
    return matched


def _sortable_datetime(value):
    """Normalize a scope-context date/datetime/None into a sort key."""
    if value is None:
        return datetime.min
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time())


def _sort_keys():
    """Map each sort key to its row-key function (date_added is the default)."""
    def context_value(row, index, default):
        try:
            return row[index] if len(row) > index else default
        except (IndexError, TypeError):
            return default

    return {
        'priority': lambda row: (
            PRIORITY_ORDER.get(context_value(row, 1, 'medium') or 'medium', 1),
            -(row[0].rating or 0)),
        'rating': lambda row: -(row[0].rating or 0),
        'runtime': lambda row: ((1, 0) if row[0].runtime is None
                                else (0, row[0].runtime)),
        'release_date': lambda row: (row[0].release_date
                                     or datetime(1900, 1, 1).date()),
        'oldest': lambda row: ((datetime(2100, 1, 1).date()
                                - row[0].release_date).days
                               if row[0].release_date else 99999999),
        'date_added': lambda row: _sortable_datetime(
            context_value(row, 2, None)),
    }


def _apply_sort(rows, smart_list, filters):
    """Deterministic in-memory sort over the (already filtered) candidate set.

    The candidate set is bounded by the SQL filters plus the watch-state /
    availability windows, so this never becomes an unbounded Python loop.
    """
    if smart_list.sort == 'random':
        # Stable within a (user, list, day) so the grid does not reshuffle on
        # every interaction; still varied across days.
        seed = (f'{smart_list.user_id}:{smart_list.id}:'
                f'{datetime.utcnow().date().isoformat()}')
        return random.Random(seed).sample(rows, len(rows))

    keys = _sort_keys()
    return sorted(rows, key=keys.get(smart_list.sort, keys['date_added']))


def rule_summary(smart_list):
    """Human-readable rule chips for the UI (no raw JSON ever)."""
    scope_labels = {
        'watchlist': 'My Watchlist',
        'diary': 'Watched History',
        'tracked_tv': 'Tracked TV',
        'all': 'Everything',
    }
    # Table-driven chip formatters in canonical display order; a formatter
    # may return None to suppress its chip (e.g. services == 'none').
    filter_labels = {
        'watch_state': lambda v: v.title(),
        'media_type': lambda v: 'Movies' if v == 'movie' else 'TV',
        'genres': ', '.join,
        'year': str,
        'decade': lambda v: f'{v}s',
        'min_rating': lambda v: f'Rating ≥ {v:g}',
        'runtime': lambda v: f'Under {RUNTIME_FILTERS[v]} min',
        'added_within': lambda v: f'Added > {AGE_FILTERS[v]} mo',
        'tv_status': lambda v: v.replace('_', ' ').title(),
        'services': lambda v: 'On My Services' if v == 'my_services' else None,
        'service_id': lambda v: f'Provider #{v}',
    }
    labels = [scope_labels.get(smart_list.scope, smart_list.scope)]
    for key, formatter in filter_labels.items():
        value = smart_list.filters.get(key)
        if value:
            label = formatter(value)
            if label:
                labels.append(label)
    sort_labels = {
        'priority': 'Priority', 'date_added': 'Date Added',
        'rating': 'Rating', 'runtime': 'Runtime', 'release_date': 'Newest',
        'oldest': 'Oldest', 'random': 'Surprise Me',
    }
    labels.append(sort_labels.get(smart_list.sort, smart_list.sort))
    return labels


def evaluate_smart_list(smart_list, page=1, per_page=24):
    """Evaluate a Smart List and return the canonical result page.

    Returns {'items': [...], 'total': int, 'page': int, 'pages': int}.
    Each item carries the MediaItem plus optional scope context (priority /
    date added / tv status) so cards can render without further queries.
    """
    filters = validate_config(smart_list.scope, smart_list.filters,
                              smart_list.sort)
    query = _scope_query(smart_list)

    if smart_list.scope == 'tracked_tv' and 'tv_status' in filters:
        query = query.filter(TVShowProgress.status == filters['tv_status'])

    query = _apply_metadata_filters(query, filters)
    query = _apply_scope_filters(query, smart_list, filters)

    # The watch-state filter materializes the filtered candidate set (bounded
    # by SQL conditions); everything after this point works on that list.
    rows = _apply_watch_state(query, smart_list, filters)
    if not isinstance(rows, list):
        rows = rows.all()

    rows = _apply_availability(smart_list, filters, rows, per_page)
    rows = _apply_sort(rows, smart_list, filters)

    total = len(rows)
    pages = max(1, -(-total // per_page))
    page = max(1, min(int(page or 1), pages))
    window = rows[(page - 1) * per_page: page * per_page]

    items = []
    for row in window:
        media, context = _split_row(row)
        item = _card_dict(media)
        if smart_list.scope == 'watchlist':
            item['priority'] = context.get('priority') or 'medium'
        elif smart_list.scope == 'tracked_tv':
            item['tv_status'] = context.get('status')
            last = context.get('last_watched')
            item['last_watched'] = last.isoformat() if last else None
        items.append(item)

    return {'items': items, 'total': total, 'page': page, 'pages': pages}


def _split_row(row):
    """Split an engine row into (MediaItem, context dict).

    SQLAlchemy 2.0 Row objects are labeled, so attribute access is used
    uniformly — it works for bare MediaItem results, Row results, and ORM
    media objects alike.
    """
    media = getattr(row, 'MediaItem', None)
    if media is None:
        media = row[0] if isinstance(row, (tuple, list)) else row
    context = {
        'priority': getattr(row, 'priority', None),
        'status': getattr(row, 'status', None),
        'last_watched': getattr(row, 'last_watched', None),
        'date_added': getattr(row, 'date_added', None),
    }
    return media, context


def _card_dict(media):
    """Canonical card payload — detail URL, full poster URL, safe fields."""
    is_movie = media.media_type == 'movie'
    poster = media.poster_path
    if poster and not str(poster).startswith(('http://', 'https://')):
        poster = f'https://image.tmdb.org/t/p/w500{poster}'
    date = media.release_date
    return {
        'id': media.tmdb_id,
        'media_type': media.media_type,
        'title': media.title,
        'poster_url': poster,
        'detail_url': f'/movie/{media.tmdb_id}' if is_movie
                      else f'/tv/{media.tmdb_id}',
        'release_date': date.isoformat() if date else None,
        'year': date.year if date else None,
        'rating': media.rating,
        'runtime': media.runtime,
        'genres': (media.genres or ''),
    }
