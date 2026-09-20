"""Unified personal entertainment calendar (Feature 10).

Normalizes the user's OWN upcoming media into one bounded, ordered
event stream:

    tracked shows (TVShowProgress)
        → UpcomingEpisode (populated by the existing daily sync)
    watchlist movies (user_watchlist)
        → MovieReleaseDate (Feature 10B per-region release cache,
          refreshed by scripts/sync_watchlist_release_data.py)
        → MediaItem.release_date (10A legacy fallback for titles the
          release sync has not covered yet)

Semantics:
- CalendarEvent is a PRESENTATION model; internal DB ids are not
  exposed — events carry TMDb ids (public identifiers used by the
  existing /tv/<id> and /movie/<id> detail routes) plus a stable
  deterministic string id.
- TV events never duplicate: UpcomingEpisode is already deduplicated
  by the sync (show, season, episode) and read here per user.
- Movie release dates come from MovieReleaseDate (Feature 10B) — a
  per-region cache the bounded sync job maintains OFF the request
  path. Labels map conservatively from TMDb's release-type integer
  and are NEVER relabelled as digital/streaming without evidence —
  this feature is a release calendar, not Where-to-Watch. A watchlist
  title with no release-cache rows at all still surfaces its legacy
  MediaItem.release_date, so 10A behavior never regresses.
- Watched state does not remove a TV event; tracking is not watching.
- All queries are bounded (fixed statement budget, capped date range,
  capped watchlist hydration); no request-path TMDb access — calendar
  reads local data only.
- Dates are calendar dates (server-UTC semantics, matching the
  existing UpcomingEpisode pipeline). air_time is shown verbatim when
  the sync captured one and never invented or timezone-shifted.
"""
from collections import namedtuple
from datetime import datetime, timedelta

from models import MediaItem, MovieReleaseDate, TVEpisodeWatch, \
    TVShowProgress, UpcomingEpisode
from models.associations import user_watchlist
from models.base import db

# §7: bounded ranges — one month-ish default, hard cap on any request.
MAX_RANGE_DAYS = 62
DEFAULT_PAST_DAYS = 7
DEFAULT_FUTURE_DAYS = 30

VALID_EVENT_TYPES = ("all", "tv", "movie")
VALID_SCOPES = ("all", "watchlist", "tracking")

TV_TRACKING_STATUSES = ("watching", "plan_to_watch")
UNKNOWN_TITLES_CAP = 20
# Watchlist hydration is capped so a pathological watchlist cannot turn
# one calendar request into an unbounded scan. 200 covers any realistic
# list and bounds the release-cache prefetch to ≤ ~6 rows per title.
_WATCHLIST_CAP = 200

# Feature 10B — TMDb release-type integers that produce calendar
# events. 3 = theatrical (incl. limited), 4 = digital. Other types
# (premiere, physical, TV) are cached by the sync but never turned
# into events: the calendar shows availability-relevant releases only.
EVENT_RELEASE_TYPES = (3, 4)
_RELEASE_LABELS = {3: "theatrical", 4: "digital"}

# Batched watchlist hydration row: (tmdb_id, title, poster, legacy date).
_WatchlistMovie = namedtuple(
    "_WatchlistMovie", "tmdb_id title poster_path legacy_release_date")


class _LegacyMedia:
    """Minimal attribute shim so the 10A fallback builder stays untouched."""

    __slots__ = ("tmdb_id", "title", "poster_path", "release_date")

    def __init__(self, tmdb_id, title, poster_path, release_date):
        self.tmdb_id = tmdb_id
        self.title = title
        self.poster_path = poster_path
        self.release_date = release_date


def default_range(today=None):
    """Default bounded window: a little of the recent past + a month ahead."""
    today = today or datetime.utcnow().date()
    return (today - timedelta(days=DEFAULT_PAST_DAYS),
            today + timedelta(days=DEFAULT_FUTURE_DAYS))


def clamp_range(start, end):
    """Clamp a requested range to the hard cap (returns range + flag)."""
    capped = False
    if (end - start).days > MAX_RANGE_DAYS:
        end = start + timedelta(days=MAX_RANGE_DAYS)
        capped = True
    return start, end, capped


def _episode_event(row, watched_keys, today):
    watched = (row.show_id, row.season_number, row.episode_number) in watched_keys
    return {
        "id": f"tv-{row.show_id}-s{row.season_number}e{row.episode_number}",
        "event_type": "episode",
        "media_type": "tv",
        "title": row.show_name,
        "poster": row.poster_path,
        "date": row.air_date.isoformat(),
        "time": row.air_time or None,   # only when the sync captured one
        "tmdb_id": row.show_id,
        "season_number": row.season_number,
        "episode_number": row.episode_number,
        "release_type": None,
        "status": "upcoming" if row.air_date >= today else "aired",
        "source": "tracked_show",
        "is_tracked": True,
        "is_watchlisted": False,
        "watched": watched,
        "detail_url": f"/tv/{row.show_id}",
        "metadata": {
            "episode_name": row.episode_name or None,
            "overview": row.episode_overview or None,
            "runtime": row.runtime,
            "days_until": (row.air_date - today).days,
        },
    }


def _movie_event(media, today):
    """10A legacy movie event (MediaItem.release_date fallback)."""
    return {
        "id": f"movie-{media.tmdb_id}",
        "event_type": "movie_release",
        "media_type": "movie",
        "title": media.title,
        "poster": media.poster_path,
        "date": media.release_date.isoformat(),
        "time": None,                    # release dates are date events
        "tmdb_id": media.tmdb_id,
        "season_number": None,
        "episode_number": None,
        # General release date from TMDb movie details — not a verified
        # digital/streaming date, so the label stays conservative.
        "release_type": "theatrical",
        "status": "upcoming" if media.release_date >= today else "released",
        "source": "watchlist",
        "is_tracked": False,
        "is_watchlisted": True,
        "watched": False,
        "detail_url": f"/movie/{media.tmdb_id}",
        "metadata": {},
    }


def _movie_release_event(tmdb_id, title, poster, rdate, rtype, today):
    """Release-cache event (Feature 10B). The id encodes
    (tmdb_id, date, type) so a sync rerun can never yield a duplicate
    event, and identical (date, type) pairs collapse to one id."""
    return {
        "id": "movie-%d-%s-t%s" % (tmdb_id, rdate.isoformat(), rtype),
        "event_type": "movie_release",
        "media_type": "movie",
        "title": title,
        "poster": poster,
        "date": rdate.isoformat(),
        "time": None,                    # release dates are date events
        "tmdb_id": tmdb_id,
        "season_number": None,
        "episode_number": None,
        # Conservative label from TMDb's release-type integer; unknown
        # values label as 'unknown' rather than being fabricated.
        "release_type": _RELEASE_LABELS.get(rtype, "unknown"),
        "status": "upcoming" if rdate >= today else "released",
        "source": "watchlist",
        "is_tracked": False,
        "is_watchlisted": True,
        "watched": False,
        "detail_url": "/movie/%d" % tmdb_id,
        "metadata": {},
    }


def _watchlist_movies(user_id):
    """Batched watchlist hydration: ONE bounded statement regardless of
    watchlist size (no per-movie lookups anywhere)."""
    rows = (
        db.session.query(
            MediaItem.tmdb_id, MediaItem.title, MediaItem.poster_path,
            MediaItem.release_date)
        .join(user_watchlist,
              db.and_(user_watchlist.c.media_id == MediaItem.id,
                      user_watchlist.c.media_type == "movie"))
        .filter(
            user_watchlist.c.user_id == user_id,
            MediaItem.media_type == "movie",
        )
        .order_by(MediaItem.tmdb_id)
        .limit(_WATCHLIST_CAP)
        .all()
    )
    return [_WatchlistMovie(*row) for row in rows]


def _release_cache_rows(wl_ids, region):
    """ALL cached release rows for the hydrated titles in one region —
    deliberately NOT window-filtered, because coverage (not just
    in-window rows) decides the legacy fallback and the unknown
    bucket. One statement, bounded by _WATCHLIST_CAP titles."""
    if not wl_ids:
        return []
    return (
        db.session.query(
            MovieReleaseDate.tmdb_id,
            MovieReleaseDate.release_type,
            MovieReleaseDate.release_date)
        .filter(
            MovieReleaseDate.tmdb_id.in_(wl_ids),
            MovieReleaseDate.region == region,
        )
        .all()
    )


def _movie_events_and_unknown(user_id, start, end, today, region):
    """Watchlist movie release events (Feature 10B).

    Returns (events, unknown) — the in-window cache-backed events, the
    10A legacy fallback for uncovered titles, and the bounded
    release_date_unknown bucket.
    """
    watchlist_movies = _watchlist_movies(user_id)
    cache_rows = _release_cache_rows(
        [m.tmdb_id for m in watchlist_movies], region)

    covered = {r.tmdb_id for r in cache_rows}
    title_by_tmdb = {m.tmdb_id: (m.title, m.poster_path)
                     for m in watchlist_movies}

    # In-window release-cache events. Identical (date, type) pairs
    # collapse by construction of the deterministic event id; the
    # (tmdb_id, region, release_type) unique constraint means a sync
    # rerun can never add a second row for the same date/type.
    events = []
    seen = set()
    for tmdb_id, rtype, rdate in cache_rows:
        if rtype not in EVENT_RELEASE_TYPES:
            continue
        if rdate < start or rdate > end:
            continue
        key = (tmdb_id, rdate, rtype)
        if key in seen:
            continue
        seen.add(key)
        title, poster = title_by_tmdb[tmdb_id]
        events.append(_movie_release_event(
            tmdb_id, title, poster, rdate, rtype, today))

    # 10A legacy fallback: titles with NO release-cache coverage at
    # all (the sync job may not have run yet) keep surfacing their
    # MediaItem.release_date. Once the cache covers a title, the
    # legacy date is dropped — the cache is strictly fresher.
    for m in watchlist_movies:
        if m.legacy_release_date is None or m.tmdb_id in covered:
            continue
        if m.legacy_release_date < start or m.legacy_release_date > end:
            continue
        events.append(_movie_event(_LegacyMedia(
            m.tmdb_id, m.title, m.poster_path,
            m.legacy_release_date), today))

    # Watchlist movies with NO release data anywhere (no cache row,
    # no legacy date): never block the calendar — bounded titles.
    unknown_titles = sorted(
        m.title for m in watchlist_movies
        if m.legacy_release_date is None and m.tmdb_id not in covered)
    unknown = {
        "count": len(unknown_titles),
        "titles": unknown_titles[:UNKNOWN_TITLES_CAP],
    }
    return events, unknown


def get_calendar_events(user_id, start, end, event_type="all", scope="all",
                        region="US"):
    """Build the unified calendar event stream for one user.

    Fixed query budget regardless of event count:
      1  tracked show ids
      2  upcoming episodes in range
      3  watched-episode keys for those shows
      4  watchlisted movies (batched hydration, capped)
      5  release-cache rows for those movies (one region, capped)

    Returns (events, meta). Deterministic order:
    (date asc, event_type, title, id).
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError("event_type must be one of %r" % (VALID_EVENT_TYPES,))
    if scope not in VALID_SCOPES:
        raise ValueError("scope must be one of %r" % (VALID_SCOPES,))

    today = datetime.utcnow().date()
    events = []
    # Zeroed unless movie events are included: the unknown-release bucket
    # only describes watchlisted movies, so TV-only/tracking-scoped reads
    # correctly report no unknown releases.
    unknown = {"count": 0, "titles": []}

    include_tv = event_type in ("all", "tv") and scope in ("all", "tracking")
    include_movies = (
        event_type in ("all", "movie") and scope in ("all", "watchlist"))

    if include_tv:
        show_ids = [
            s.show_id for s in TVShowProgress.query.with_entities(
                TVShowProgress.show_id)
            .filter(TVShowProgress.user_id == user_id)
            .filter(TVShowProgress.status.in_(TV_TRACKING_STATUSES))
            .all()
        ]
        if show_ids:
            upcoming = UpcomingEpisode.query.filter(
                UpcomingEpisode.show_id.in_(show_ids),
                UpcomingEpisode.air_date >= start,
                UpcomingEpisode.air_date <= end,
            ).order_by(UpcomingEpisode.air_date).all()
            if upcoming:
                watched_keys = {
                    (w.show_id, w.season_number, w.episode_number)
                    for w in TVEpisodeWatch.query.with_entities(
                        TVEpisodeWatch.show_id, TVEpisodeWatch.season_number,
                        TVEpisodeWatch.episode_number)
                    .filter(TVEpisodeWatch.user_id == user_id)
                    .filter(TVEpisodeWatch.show_id.in_(
                        {ep.show_id for ep in upcoming}))
                    .all()
                }
                events.extend(
                    _episode_event(ep, watched_keys, today) for ep in upcoming)

    if include_movies:
        movie_events, unknown = _movie_events_and_unknown(
            user_id, start, end, today, region)
        events.extend(movie_events)

    events.sort(key=lambda e: (e["date"], e["event_type"], e["title"], e["id"]))
    meta = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "today": today.isoformat(),
        "counts": {
            "tv": sum(1 for e in events if e["event_type"] == "episode"),
            "movie": sum(1 for e in events
                         if e["event_type"] == "movie_release"),
            "total": len(events),
        },
        "release_date_unknown": unknown,
    }
    return events, meta
