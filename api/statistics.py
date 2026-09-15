"""Canonical personal statistics service (Feature #8, Phase 1).

The SINGLE source of truth for a user's viewing-history statistics:

    DiaryEntry (authoritative watch-event history)  +  MediaItem metadata
        ↓  get_statistics(user_id, year=... | lifetime=True)
    bounded SQL aggregation  →  compact presentation dict

Audited duplicate implementations (routes/stats.py endpoints calculate
their own counters at route level) remain untouched; this module is the
canonical service that future surfaces (Wrapped, profile stats) consume.

Source-of-truth semantics (§4, §6, §10, §13):

  WATCH EVENT   one DiaryEntry row. Rewatching a title logs another
                event; it never creates a new distinct title.
  DISTINCT TITLES  unique DiaryEntry.media_id values in the window.
  REWATCH EVENT  any event beyond the first for a title — computed as
                total events − distinct titles (never negative), which
                by construction never counts a first watch as a rewatch.
                The persisted is_rewatch flag (set by routes/diary.py as
                ``watch_count > 0``) agrees with this definition.
  REWATCH RATE   rewatch events / total watch events (0.0 when no events).
  HOURS          sum(MediaItem.runtime) / 60 across EVENTS — a rewatch
                legitimately adds its runtime again. Events whose media
                has no persisted runtime are EXCLUDED from hours (never
                invented) and counted in ``events_missing_runtime``.
                TV runtimes use the existing MediaItem.runtime semantics
                (first episode run time) — no new TV episode-hours model
                is fabricated.
  RATINGS        DiaryEntry.rating only (the application's 0.5–5.0
                scale). Likes, clicks, saves, reviews-without-diary, and
                recommendation feedback are NOT ratings here.
  GENRES         MediaItem.genres (persisted comma-separated labels)
                aggregated per EVENT, plus a distinct-title count per
                genre. Missing metadata simply does not contribute.
  MEDIA TYPE     DiaryEntry.media_type as stored ('movie' / 'tv').

Design invariants:

  LOCAL DATA ONLY — zero network, zero TMDb, zero streaming calls.

  BOUNDED QUERIES — exactly 5 SQL statements per call regardless of
  history size: event aggregates, rating distribution, media-type split,
  monthly trend, and one slim projection of DISTINCT watched media for
  the comma-split genre aggregation (SQL cannot split CSV labels). No
  N+1, no full-diary materialization.

  PURE CORE — the helper functions are deterministic and perform no DB
  or network access.

  PRIVACY — aggregate statistics only; no IDs, payloads, or ORM rows in
  the presentation shape. Statistics describe what the user watched —
  they do NOT read TasteProfile, RecommendationFeedback, watchlist,
  wishlist, or likes, which are different subsystems.
"""
from datetime import date, datetime

from sqlalchemy import distinct, extract, func

from models.base import db
from models.media import MediaItem
from models.social import DiaryEntry

__all__ = [
    "MAX_LIFETIME_MONTHS",
    "TOP_GENRES",
    "get_statistics",
    "hours_watched",
    "rewatch_rate",
    "average_rating",
    "rating_distribution",
    "month_bucket",
    "parse_genres",
    "aggregate_genres",
    "media_type_distribution",
]

# Lifetime monthly trend is bounded so long-lived users never produce
# thousands of rows (§11). The most recent 36 months is plenty for V1.
MAX_LIFETIME_MONTHS = 36
TOP_GENRES = 10

_GENRE_BUCKETS = tuple(f"{0.5 * i:.1f}" for i in range(1, 11))  # 0.5..5.0


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers (no DB, no network — deterministic)
# ════════════════════════════════════════════════════════════════════════════

def hours_watched(total_runtime_minutes):
    """Convert a summed runtime (minutes) to hours, rounded to 1 dp.

    None/negative input → 0.0 (missing runtime is never invented).
    """
    if not total_runtime_minutes or total_runtime_minutes <= 0:
        return 0.0
    return round(total_runtime_minutes / 60.0, 1)


def rewatch_rate(rewatch_events, total_events):
    """rewatch events / total watch events, rounded to 2 dp.

    0.0 when there are no events (never a division by zero).
    """
    if not total_events or total_events <= 0:
        return 0.0
    return round(max(0, rewatch_events) / float(total_events), 2)


def average_rating(ratings):
    """Arithmetic mean of the given ratings, rounded to 2 dp.

    Empty/None-only input → None (no rating exists; do not fabricate 0).
    """
    values = [r for r in (ratings or []) if r is not None]
    if not values:
        return None
    return round(sum(values) / float(len(values)), 2)


def rating_distribution(ratings):
    """Count ratings into the application's fixed 0.5–5.0 half-star buckets.

    Always returns all ten buckets (zeros included) for stable output.
    Ratings outside the valid scale are ignored (defensive; the model
    CHECK constraint already prevents them).
    """
    counts = {bucket: 0 for bucket in _GENRE_BUCKETS}
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

    Deterministic: stripped, order-preserving, de-duplicated within one
    title (a repeated label in the metadata must not double-count).
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
    (already de-duplicated per title by parse_genres, so a multi-genre
    title is not rewarded for metadata repetition). Returns
    ``[{name, count, titles}]`` sorted deterministically: count desc,
    then name asc, bounded to the top ``TOP_GENRES``.
    """
    counts, titles = {}, {}
    for names in genres_by_event:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    # titles are filled by the caller per distinct title; kept separate
    # from event counts so rewatch-heavy genres do not inflate breadth.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"name": name, "count": count, "titles": titles.get(name, 0)}
        for name, count in ranked[:TOP_GENRES]
    ]


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


# ════════════════════════════════════════════════════════════════════════════
# Service
# ════════════════════════════════════════════════════════════════════════════

def _window_bounds(year, lifetime):
    """Resolve the query window. Returns (start, end, kind, resolved_year).

    Semantics (§3): default = the CURRENT calendar year; ``year=N``
    selects that calendar year (1900 ≤ N ≤ current year); ``lifetime``
    ignores year and covers all history. ``watched_date`` is NOT NULL in
    the model, so a lower/upper bound is exact.
    """
    current_year = datetime.now().year
    if lifetime:
        return None, None, "lifetime", None
    if year is None:
        year = current_year
    if not isinstance(year, int) or isinstance(year, bool):
        raise ValueError("year must be an integer")
    if not (1900 <= year <= current_year):
        raise ValueError(
            f"year must be between 1900 and {current_year}")
    return date(year, 1, 1), date(year, 12, 31), "year", year


def _base_query(user_id, start_date, end_date):
    """DiaryEntry rows for the user, optionally bounded by the window."""
    q = db.session.query(DiaryEntry).filter(DiaryEntry.user_id == user_id)
    if start_date is not None:
        q = q.filter(DiaryEntry.watched_date >= start_date)
    if end_date is not None:
        q = q.filter(DiaryEntry.watched_date <= end_date)
    return q


def _monthly_trend(user_id, start_date, end_date, kind, year):
    """Monthly watch-event counts (query 4).

    Year window → always exactly 12 rows (Jan–Dec, zeros included).
    Lifetime → per (year, month) descending, bounded to the most recent
    MAX_LIFETIME_MONTHS rows so output never grows unboundedly.
    """
    rows = (
        db.session.query(
            extract("year", DiaryEntry.watched_date).label("y"),
            extract("month", DiaryEntry.watched_date).label("m"),
            func.count(DiaryEntry.id),
        )
        .filter(DiaryEntry.user_id == user_id)
        .group_by("y", "m")
        .all()
    )
    if start_date is not None:
        rows = [r for r in rows
                if start_date.year <= r.y <= end_date.year]
    by_bucket = {f"{int(r.y):04d}-{int(r.m):02d}": int(r[2]) for r in rows}
    if kind == "year":
        return [{"month": f"{year:04d}-{mm:02d}",
                 "count": by_bucket.get(f"{year:04d}-{mm:02d}", 0)}
                for mm in range(1, 13)]
    # lifetime: most recent bounded window, ascending for readability
    capped = sorted(by_bucket.items())[-MAX_LIFETIME_MONTHS:]
    return [{"month": bucket, "count": count}
            for bucket, count in capped]


def get_statistics(user_id, year=None, lifetime=False):
    """Compute the canonical statistics presentation model for a user.

    Exactly 5 bounded SQL statements (§14). Returns a compact
    JSON-ready dict — never ORM rows, never internal IDs. A user with
    no history in the window gets the full zeroed shape with
    ``available: false`` (deterministic empty presentation, §30).
    """
    start_date, end_date, kind, resolved_year = _window_bounds(
        year, lifetime)

    # Query 1 — event aggregates (one join, SQL-side sums/counts).
    event_row = (
        db.session.query(
            func.count(DiaryEntry.id),
            func.count(distinct(DiaryEntry.media_id)),
            func.coalesce(func.sum(MediaItem.runtime), 0),
            func.count(MediaItem.runtime),
            func.sum(
                db.case((MediaItem.runtime.is_(None), 1), else_=0)),
        )
        .select_from(DiaryEntry)
        .outerjoin(MediaItem, DiaryEntry.media_id == MediaItem.id)
        .filter(DiaryEntry.user_id == user_id)
    )
    if start_date is not None:
        event_row = event_row.filter(
            DiaryEntry.watched_date >= start_date)
    if end_date is not None:
        event_row = event_row.filter(
            DiaryEntry.watched_date <= end_date)
    total_events, distinct_titles, runtime_sum, runtime_count, missing_runtime = \
        event_row.one()
    total_events = int(total_events or 0)
    distinct_titles = int(distinct_titles or 0)
    rewatch_events = max(0, total_events - distinct_titles)

    # Query 2 — rating distribution (bounded: ≤ 10 distinct buckets).
    rating_rows = (
        _base_query(user_id, start_date, end_date)
        .with_entities(DiaryEntry.rating, func.count(DiaryEntry.id))
        .filter(DiaryEntry.rating.isnot(None))
        .group_by(DiaryEntry.rating)
        .all()
    )
    ratings_flat = [
        float(rating) for rating, count in rating_rows
        for _ in range(int(count))
    ]

    # Query 3 — media-type split (bounded: 2 stored values).
    type_rows = (
        _base_query(user_id, start_date, end_date)
        .with_entities(DiaryEntry.media_type, func.count(DiaryEntry.id))
        .group_by(DiaryEntry.media_type)
        .all()
    )

    # Query 4 — monthly trend (GROUP BY year+month; shaped in
    # _monthly_trend to the window contract).
    months = _monthly_trend(
        user_id, start_date, end_date, kind, resolved_year)

    # Query 5 — slim per-event projection for the CSV genre split (SQL
    # cannot split comma-separated labels). One row per watch event,
    # bounded by the window's event count; media IDs are used only to
    # de-duplicate title breadth in Python and never leave this module.
    media_rows = (
        db.session.query(
            DiaryEntry.media_id, MediaItem.genres,
        )
        .select_from(DiaryEntry)
        .join(MediaItem, DiaryEntry.media_id == MediaItem.id)
        .filter(DiaryEntry.user_id == user_id)
    )
    if start_date is not None:
        media_rows = media_rows.filter(
            DiaryEntry.watched_date >= start_date)
    if end_date is not None:
        media_rows = media_rows.filter(
            DiaryEntry.watched_date <= end_date)
    event_genres, title_genres, seen_media = [], {}, set()
    for media_id, genres_value in media_rows.all():
        names = parse_genres(genres_value)
        event_genres.append(names)
        if media_id not in seen_media:
            seen_media.add(media_id)
            for name in names:
                title_genres[name] = title_genres.get(name, 0) + 1
    genre_rows = aggregate_genres(event_genres)
    for row in genre_rows:
        row["titles"] = title_genres.get(row["name"], 0)

    return {
        "available": total_events > 0,
        "window": {"kind": kind, "year": year if kind == "year" else None},
        "watch": {
            "events": total_events,
            "distinct_titles": distinct_titles,
            "rewatch_events": rewatch_events,
            "rewatch_rate": rewatch_rate(rewatch_events, total_events),
            "hours_watched": hours_watched(runtime_sum),
            "events_missing_runtime": int(missing_runtime or 0),
        },
        "media_types": media_type_distribution(
            {t: c for t, c in type_rows}),
        "ratings": {
            "count": len(ratings_flat),
            "average": average_rating(ratings_flat),
            "distribution": rating_distribution(ratings_flat),
        },
        "genres": genre_rows,
        "months": months,
    }
