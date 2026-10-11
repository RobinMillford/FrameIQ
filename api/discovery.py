"""F11 — discovery facet contract (validation + TMDb parameter mapping).

FrameIQ's Browse/Search surface is TMDb-backed, not local-DB backed: results
come from TMDb's ``/discover`` endpoints through ``routes/tmdb_proxy.py``.
This module owns the *vocabulary and validation* for the F11 facet UI and for
saved discovery presets, so both the live query string and a stored preset are
validated by exactly one implementation.

Design invariants:

  SERVER IS THE SOURCE OF TRUTH — ``parse_discovery_args`` canonicalises raw
  query args and ``build_discover_params`` is the only place TMDb parameter
  names are produced. The browser never invents a parameter; it replays the
  canonical set the server handed it.

  MEDIA-CORRECT DATE SEMANTICS — movies filter on ``primary_release_date.*``,
  TV on ``first_air_date.*``. They are NOT interchangeable, and neither is
  reused for the other.

  NO STREAMING CLAIMS — provider/availability is deliberately absent. FrameIQ
  has its own region-scoped availability model (``api/availability``); a
  non-region-aware TMDb provider filter would let the UI imply a title is on a
  local service when that may be false. Deferred, not approximated.

  BOUNDED — page numbers are clamped, genre selections are capped, and every
  numeric value is range-checked before it can reach TMDb.
"""

# ── Genre vocabulary ─────────────────────────────────────────────────────────
# TMDb genre ids mirrored from the maps routes/browse.py already exposes for
# /genre/<name> and /tv_genre/<name>, so a discovery URL and an existing genre
# URL speak the same language. Keys are URL-safe (underscored) for sharing.
MOVIE_GENRES = {
    'romance': 10749, 'horror': 27, 'fantasy': 14, 'science_fiction': 878,
    'mystery': 9648, 'western': 37, 'drama': 18, 'action': 28, 'comedy': 35,
    'thriller': 53, 'adventure': 12, 'animation': 16, 'crime': 80,
    'family': 10751, 'history': 36, 'music': 10402, 'war': 10752,
    'documentary': 99, 'tv_movie': 10770,
}

TV_GENRES = {
    'action_adventure': 10759, 'animation': 16, 'comedy': 35, 'crime': 80,
    'documentary': 99, 'drama': 18, 'family': 10751, 'kids': 10762,
    'mystery': 9648, 'news': 10763, 'reality': 10764, 'sci_fi_fantasy': 10765,
    'soap': 10766, 'talk': 10767, 'war_politics': 10768, 'western': 37,
}

MEDIA_TYPES = ('movie', 'tv')

# Human labels for the UI; the underscored key stays the URL value.
GENRE_LABELS = {
    'romance': 'Romance', 'horror': 'Horror', 'fantasy': 'Fantasy',
    'science_fiction': 'Science Fiction', 'mystery': 'Mystery',
    'western': 'Western', 'drama': 'Drama', 'action': 'Action',
    'comedy': 'Comedy', 'thriller': 'Thriller', 'adventure': 'Adventure',
    'animation': 'Animation', 'crime': 'Crime', 'family': 'Family',
    'history': 'History', 'music': 'Music', 'war': 'War',
    'documentary': 'Documentary', 'tv_movie': 'TV Movie',
    'action_adventure': 'Action & Adventure', 'kids': 'Kids',
    'news': 'News', 'reality': 'Reality', 'sci_fi_fantasy': 'Sci-Fi & Fantasy',
    'soap': 'Soap', 'talk': 'Talk', 'war_politics': 'War & Politics',
}

# ── Sorting ──────────────────────────────────────────────────────────────────
# Only values TMDb documents for /discover. Date sorts are media-specific and
# are NOT interchangeable between the two endpoints.
_COMMON_SORTS = ('popularity.desc', 'vote_average.desc', 'vote_count.desc')

MOVIE_SORTS = _COMMON_SORTS + ('primary_release_date.desc',
                               'primary_release_date.asc')
TV_SORTS = _COMMON_SORTS + ('first_air_date.desc', 'first_air_date.asc')

DEFAULT_SORT = 'popularity.desc'

SORT_LABELS = {
    'popularity.desc': 'Most popular',
    'vote_average.desc': 'Highest rated',
    'vote_count.desc': 'Most rated',
    'primary_release_date.desc': 'Newest first',
    'primary_release_date.asc': 'Oldest first',
    'first_air_date.desc': 'Newest first',
    'first_air_date.asc': 'Oldest first',
}

# ── Bounds ───────────────────────────────────────────────────────────────────
MIN_YEAR = 1888                 # earliest film TMDb catalogues in practice
MAX_YEAR_HORIZON = 2            # allow next year + 1 for announced releases
MIN_RATING = 0.0
MAX_RATING = 10.0
MAX_GENRES = 10                 # matches the SmartList genre cap
MAX_PAGE = 500                  # TMDb caps discover pagination at 500 pages
DEFAULT_PER_PAGE = 24

# Filters recognised inside a stored SmartList preset.
PRESET_KEYS = ('kind', 'discovery')

# Discriminator stored in filters_json. See api/smart_lists.is_discovery_preset.
DISCOVERY_KIND = 'discovery'

# Compatibility value for the non-nullable, validated SmartList `scope` column.
# It is never interpreted for a preset: every reader checks
# is_discovery_preset() BEFORE looking at scope, so this placeholder never
# reaches the SQL-first evaluator or renders as a normal "Everything" list.
DISCOVERY_COMPAT_SCOPE = 'all'
DISCOVERY_COMPAT_SORT = 'date_added'   # inert; real sort lives in the config


class DiscoveryError(ValueError):
    """Raised when a discovery configuration cannot be canonicalised."""


def genres_for(media_type):
    """Return the genre vocabulary for a media type."""
    return TV_GENRES if media_type == 'tv' else MOVIE_GENRES


def sorts_for(media_type):
    """Return the sort allowlist for a media type."""
    return TV_SORTS if media_type == 'tv' else MOVIE_SORTS


def genre_options(media_type):
    """UI options as [{value, label}] for a media type, alphabetically."""
    vocab = genres_for(media_type)
    return [{'value': k, 'label': GENRE_LABELS.get(k, k.replace('_', ' ').title())}
            for k in sorted(vocab)]


def _clean_int(raw, name):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise DiscoveryError(f'{name} must be a whole number')


def _clean_float(raw, name):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise DiscoveryError(f'{name} must be a number')
    return value


def _multi_values(raw):
    """Normalise a repeated or comma-separated query value into a list."""
    if raw is None:
        return []
    items = []
    for chunk in (raw if isinstance(raw, (list, tuple)) else [raw]):
        for piece in str(chunk).split(','):
            piece = piece.strip()
            if piece and piece not in items:
                items.append(piece)
    return items


def _max_year():
    from datetime import datetime
    return datetime.utcnow().year + MAX_YEAR_HORIZON


def _strict_genres(raw, vocab, source):
    """Validate a stored genre selection (strict — no silent dropping)."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise DiscoveryError('genres must be a list of genre names')
    requested = _multi_values(raw)
    if not requested:
        # An explicitly empty selection is malformed, not "no genre". (The
        # lenient query-string parser drops these and reports a notice.)
        raise DiscoveryError('genres must name at least one genre')
    genres = []
    for name in requested:
        key = str(name).strip().lower().replace(' ', '_')
        if key not in vocab:
            raise DiscoveryError(f'Unknown {source} genre: {name}')
        if key not in genres:
            genres.append(key)
    if len(genres) > MAX_GENRES:
        raise DiscoveryError(f'Select at most {MAX_GENRES} genres')
    return genres


def _strict_year(raw, label):
    if raw in (None, ''):
        return None
    year = _clean_int(raw, label)
    if not MIN_YEAR <= year <= _max_year():
        raise DiscoveryError(
            f'{label} must be between {MIN_YEAR} and {_max_year()}')
    return year


def _strict_rating(raw):
    if raw in (None, ''):
        return None
    rating = round(_clean_float(raw, 'min_rating'), 1)
    if not MIN_RATING <= rating <= MAX_RATING:
        raise DiscoveryError(
            f'min_rating must be between {MIN_RATING:g} and {MAX_RATING:g}')
    return rating


def validate_discovery_config(raw, *, source='config'):
    """Validate a discovery configuration; return the canonical dict.

    Accepts the storage shape used by saved presets:
    ``{'media_type', 'genres', 'year_from', 'year_to', 'min_rating', 'sort'}``.

    Raises DiscoveryError with a user-safe message on anything invalid. Never
    returns partially-unvalidated data.
    """
    if not isinstance(raw, dict):
        raise DiscoveryError(f'Discovery {source} must be an object')

    media_type = str(raw.get('media_type') or 'movie').strip().lower()
    if media_type not in MEDIA_TYPES:
        raise DiscoveryError('media_type must be "movie" or "tv"')

    genres = _strict_genres(raw.get('genres'), genres_for(media_type), source)
    year_from = _strict_year(raw.get('year_from'), 'year_from')
    year_to = _strict_year(raw.get('year_to'), 'year_to')
    min_rating = _strict_rating(raw.get('min_rating'))

    # Contradictory ranges are normalised (swapped) rather than rejected, so a
    # shared URL stays usable and the UI can explain what happened.
    if year_from and year_to and year_from > year_to:
        year_from, year_to = year_to, year_from

    sort = str(raw.get('sort') or DEFAULT_SORT).strip().lower()
    if sort not in sorts_for(media_type):
        raise DiscoveryError('Unsupported sort for this media type')

    config = {'media_type': media_type, 'sort': sort}
    if genres:
        config['genres'] = genres
    if year_from is not None:
        config['year_from'] = year_from
    if year_to is not None:
        config['year_to'] = year_to
    if min_rating is not None:
        config['min_rating'] = min_rating
    return config


def _lenient_genres(raw, media_type, notices):
    """Query-string genre parsing: unknown values are dropped with a notice."""
    vocab = genres_for(media_type)
    genres = []
    for name in _multi_values(raw):
        key = str(name).strip().lower().replace(' ', '_')
        if key in vocab:
            if key not in genres:
                genres.append(key)
        else:
            notices.append(f'Ignored unknown genre "{name}".')
    if len(genres) > MAX_GENRES:
        notices.append(f'Using the first {MAX_GENRES} genres.')
        genres = genres[:MAX_GENRES]
    return genres


def _lenient_rating(raw, notices):
    if raw in (None, ''):
        return None
    try:
        rating = round(_clean_float(raw, 'min_rating'), 1)
    except DiscoveryError:
        notices.append('Ignored an unreadable rating.')
        return None
    if not MIN_RATING <= rating <= MAX_RATING:
        notices.append('Ignored rating outside 0-10.')
        return None
    return rating


def parse_discovery_args(args, *, source='filter'):
    """Canonicalise raw request args into (config, notices).

    Invalid individual values are DROPPED with a user-facing notice rather than
    failing the whole page — a malformed shared link should still render a
    usable discovery page. Contradictory year ranges are normalised.
    """
    del source  # reserved; keeps the signature stable for callers
    notices = []

    media_type = str(args.get('type') or 'movie').strip().lower()
    if media_type not in MEDIA_TYPES:
        notices.append(f'Ignored unsupported media type "{media_type}".')
        media_type = 'movie'

    genres = _lenient_genres(args.get('genres'), media_type, notices)
    year_from = _year_arg(args.get('year_from'), 'year from', notices)
    year_to = _year_arg(args.get('year_to'), 'year to', notices)
    if year_from and year_to and year_from > year_to:
        notices.append('Swapped the reversed year range.')
        year_from, year_to = year_to, year_from

    min_rating = _lenient_rating(args.get('min_rating'), notices)

    sort = str(args.get('sort') or DEFAULT_SORT).strip().lower()
    if sort not in sorts_for(media_type):
        notices.append('Reverted to the default sort.')
        sort = DEFAULT_SORT

    config = {'media_type': media_type, 'sort': sort}
    if genres:
        config['genres'] = genres
    if year_from is not None:
        config['year_from'] = year_from
    if year_to is not None:
        config['year_to'] = year_to
    if min_rating is not None:
        config['min_rating'] = min_rating
    return config, notices


def _year_arg(raw, label, notices):
    if raw in (None, ''):
        return None
    try:
        year = _clean_int(raw, label)
    except DiscoveryError:
        notices.append(f'Ignored an unreadable {label}.')
        return None
    if not MIN_YEAR <= year <= _max_year():
        notices.append(f'Ignored {label} outside the supported range.')
        return None
    return year


def clamp_page(raw):
    """Coerce a page value into the bounded range TMDb accepts."""
    try:
        page = int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return 1
    return max(1, min(page, MAX_PAGE))


def build_discover_params(config, page=1):
    """Map a canonical config to the TMDb /discover parameters.

    This is the ONLY place TMDb parameter names are written. Media type selects
    the date fields, so TV first-air dates are never read as movie release
    dates and vice versa.
    """
    media_type = config.get('media_type', 'movie')
    params = {}

    genres = config.get('genres') or []
    if genres:
        vocab = genres_for(media_type)
        params['with_genres'] = ','.join(
            str(vocab[g]) for g in genres if g in vocab)

    # Media-correct date window: full-year bounds derived from the requested
    # years, expressed in that endpoint's own field name.
    year_from = config.get('year_from')
    year_to = config.get('year_to')
    field = ('primary_release_date' if media_type == 'movie'
             else 'first_air_date')
    if year_from is not None:
        params[f'{field}.gte'] = f'{year_from:04d}-01-01'
    if year_to is not None:
        params[f'{field}.lte'] = f'{year_to:04d}-12-31'

    min_rating = config.get('min_rating')
    if min_rating is not None:
        params['vote_average.gte'] = min_rating

    params['sort_by'] = config.get('sort') or DEFAULT_SORT
    params['page'] = clamp_page(page)
    params['language'] = 'en-US'
    return params


def discover_path(media_type):
    """The validated proxy path for a media type."""
    return '/discover/tv' if media_type == 'tv' else '/discover/movie'


def config_to_query(config):
    """Serialize a canonical config back to URL query values.

    Only non-default facets are emitted, keeping shared URLs short and stable.
    """
    query = {'type': config.get('media_type', 'movie')}
    if config.get('genres'):
        query['genres'] = ','.join(config['genres'])
    if config.get('year_from') is not None:
        query['year_from'] = config['year_from']
    if config.get('year_to') is not None:
        query['year_to'] = config['year_to']
    if config.get('min_rating') is not None:
        query['min_rating'] = config['min_rating']
    if config.get('sort') and config['sort'] != DEFAULT_SORT:
        query['sort'] = config['sort']
    return query


def summarize(config):
    """User-facing chips for the active facets (server-rendered labels)."""
    chips = []
    media_type = config.get('media_type', 'movie')
    chips.append({'key': 'type', 'label': 'Movies' if media_type == 'movie'
                  else 'TV Shows'})
    for genre in config.get('genres') or []:
        chips.append({'key': 'genres', 'value': genre,
                      'label': GENRE_LABELS.get(
                          genre, genre.replace('_', ' ').title())})
    if config.get('year_from') is not None and config.get('year_to') is not None:
        label = (f'{config["year_from"]}–{config["year_to"]}'
                 if config['year_from'] != config['year_to']
                 else str(config['year_from']))
        chips.append({'key': 'year_from+year_to', 'label': label})
    elif config.get('year_from') is not None:
        chips.append({'key': 'year_from',
                      'label': f'{config["year_from"]} or later'})
    elif config.get('year_to') is not None:
        chips.append({'key': 'year_to',
                      'label': f'{config["year_to"]} or earlier'})
    if config.get('min_rating') is not None:
        chips.append({'key': 'min_rating',
                      'label': f'Rating ≥ {config["min_rating"]:g}'})
    chips.append({'key': 'sort', 'label': SORT_LABELS.get(
        config.get('sort', DEFAULT_SORT), 'Sort')})
    return chips