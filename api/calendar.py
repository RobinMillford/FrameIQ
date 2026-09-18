"""Unified personal entertainment calendar (Feature 10).

Normalizes the user's OWN upcoming media into one bounded, ordered
event stream:

    tracked shows (TVShowProgress)
        → UpcomingEpisode (populated by the existing daily sync)
    watchlist movies (user_watchlist)
        → MediaItem.release_date (cached at watchlist-add time)

Semantics:
- CalendarEvent is a PRESENTATION model; internal DB ids are not
  exposed — events carry TMDb ids (public identifiers used by the
  existing /tv/<id> and /movie/<id> detail routes) plus a stable
  deterministic string id.
- TV events never duplicate: UpcomingEpisode is already deduplicated
  by the sync (show, season, episode) and read here per user.
- MediaItem.release_date is the general/theatrical release date from
  TMDb movie details. It is NEVER relabelled as a digital/streaming
  date — this feature is a release calendar, not Where-to-Watch.
- Watched state does not remove a TV event; tracking is not watching.
- All queries are bounded (fixed statement budget, capped date range);
  no request-path TMDb access (calendar reads local data only).
- Dates are calendar dates (server-UTC semantics, matching the
  existing UpcomingEpisode pipeline). air_time is shown verbatim when
  the sync captured one and never invented or timezone-shifted.
"""
from datetime import datetime, timedelta

from models import MediaItem, TVEpisodeWatch, TVShowProgress, UpcomingEpisode
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


def get_calendar_events(user_id, start, end, event_type="all", scope="all"):
    """Build the unified calendar event stream for one user.

    Fixed query budget regardless of event count:
      1  tracked show ids
      2  upcoming episodes in range
      3  watched-episode keys for those shows
      4  watchlist movies with release dates in range
      5  watchlist movies missing a release date (unknown bucket)

    Returns (events, meta). Deterministic order:
    (date asc, event_type, title, id).
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError("event_type must be one of %r" % (VALID_EVENT_TYPES,))
    if scope not in VALID_SCOPES:
        raise ValueError("scope must be one of %r" % (VALID_SCOPES,))

    today = datetime.utcnow().date()
    events = []

    include_tv = event_type in ("all", "tv") and scope in ("all", "tracking")
    include_movies = (
        event_type in ("all", "movie") and scope in ("all", "watchlist"))

    unknown = {"count": 0, "titles": []}

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
        # Watchlist movies whose cached release date falls in the window.
        dated = (
            db.session.query(MediaItem)
            .join(user_watchlist, user_watchlist.c.media_id == MediaItem.id)
            .filter(
                user_watchlist.c.user_id == user_id,
                user_watchlist.c.media_type == "movie",
                MediaItem.media_type == "movie",
                MediaItem.release_date >= start,
                MediaItem.release_date <= end,
            )
            .all()
        )
        events.extend(_movie_event(m, today) for m in dated)

        # Watchlist movies with no cached release date: never block the
        # calendar on them — surface them as an unknown-date bucket.
        unknown_rows = (
            db.session.query(MediaItem.tmdb_id, MediaItem.title)
            .join(user_watchlist, user_watchlist.c.media_id == MediaItem.id)
            .filter(
                user_watchlist.c.user_id == user_id,
                user_watchlist.c.media_type == "movie",
                MediaItem.media_type == "movie",
                MediaItem.release_date.is_(None),
            )
            .order_by(MediaItem.title)
            .limit(UNKNOWN_TITLES_CAP)
            .all()
        )
        unknown = {
            "count": len(unknown_rows),
            "titles": [t for _, t in unknown_rows],
        }

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
