"""Bounded watchlist release-date synchronization (Feature 10B).

Refreshes MovieReleaseDate from TMDb's per-region release_dates for
movies that any user currently has watchlisted. Invoked ONLY by
scripts/sync_watchlist_release_data.py (scheduled daily via
.github/workflows/sync-watchlist-release-data.yml) — never on the
calendar request path, which reads the cache read-only.

Bounds and safety:
- _MAX_TITLES_PER_RUN: hard cap on TMDb titles touched per run.
- _STALE_AFTER_DAYS: titles with a fresh-enough cache are skipped;
  only stale or never-synced titles are fetched.
- Deterministic selection: candidates are the distinct watchlisted
  movie tmdb_ids ordered by oldest sync timestamp (never-synced sort
  first via coalesce) then tmdb_id — repeated runs converge and no
  title is starved, because a synced title's fetched_at moves it to
  the back.
- Failure isolation: one failing title never aborts the run (per-title
  try/except + per-title commit, mirroring the TV episode sync).
- Idempotent: (tmdb_id, region, release_type) unique upsert — re-runs
  never duplicate rows or events.
- All regions are cached in one pass per title (TMDb returns every
  region in a single release_dates response), so any user's region is
  served from the cache without per-user fetches.
"""
from datetime import datetime, timedelta

from sqlalchemy import asc, func

from api.tmdb.movies import fetch_movie_release_dates
from models import MediaItem, MovieReleaseDate
from models.associations import user_watchlist
from models.base import db

MAX_TITLES_PER_RUN = 150
STALE_AFTER_DAYS = 7


def _watchlisted_movie_ids(max_titles):
    """Deterministic bounded candidate selection (distinct across ALL
    users — release rows are title-level shared cache, not per-user)."""
    rows = (
        db.session.query(
            MediaItem.tmdb_id,
            func.coalesce(
                func.min(MovieReleaseDate.fetched_at),
                datetime(1970, 1, 1)).label("oldest"))
        .select_from(MediaItem)
        .join(user_watchlist, db.and_(
            user_watchlist.c.media_id == MediaItem.id,
            user_watchlist.c.media_type == "movie"))
        .outerjoin(MovieReleaseDate,
                   MovieReleaseDate.tmdb_id == MediaItem.tmdb_id)
        .filter(MediaItem.media_type == "movie")
        .group_by(MediaItem.tmdb_id)
        .order_by(asc(func.coalesce(func.min(MovieReleaseDate.fetched_at),
                                    datetime(1970, 1, 1))),
                  asc(MediaItem.tmdb_id))
        .limit(max_titles)
        .all()
    )
    return [row[0] for row in rows]


def sync_watchlist_release_data(max_titles=MAX_TITLES_PER_RUN,
                                stale_after_days=STALE_AFTER_DAYS):
    """One bounded sync pass. Requires an active Flask app context.

    Returns {'selected', 'succeeded', 'failed'} — deterministic counts
    for tests and the scheduled-job log.
    """
    max_titles = max(0, min(int(max_titles), MAX_TITLES_PER_RUN))
    stale_after_days = max(1, int(stale_after_days))

    candidates = _watchlisted_movie_ids(max_titles)
    if not candidates:
        return {"selected": 0, "succeeded": 0, "failed": 0}

    stale_cutoff = datetime.utcnow() - timedelta(days=stale_after_days)
    fresh_ids = {
        row[0] for row in
        db.session.query(MovieReleaseDate.tmdb_id)
        .filter(MovieReleaseDate.tmdb_id.in_(candidates),
                MovieReleaseDate.fetched_at > stale_cutoff)
        .distinct()
        .all()
    }
    targets = [t for t in candidates if t not in fresh_ids]

    succeeded = failed = 0
    for tmdb_id in targets:
        try:
            releases = fetch_movie_release_dates(tmdb_id)
        except Exception:
            # One bad title (network/timeout/parse) never aborts the run.
            db.session.rollback()
            failed += 1
            continue

        now = datetime.utcnow()
        existing = {
            (r.region, r.release_type): r
            for r in MovieReleaseDate.query.filter_by(tmdb_id=tmdb_id).all()
        }
        seen = set()
        for region, rtype, rdate in releases:
            key = (region, rtype)
            if key in seen:          # TMDb quirk guard: identical entries
                continue
            seen.add(key)
            row = existing.get(key)
            if row is None:
                db.session.add(MovieReleaseDate(
                    tmdb_id=tmdb_id, region=region, release_type=rtype,
                    release_date=rdate, fetched_at=now))
            else:
                row.release_date = rdate
                row.fetched_at = now
        try:
            db.session.commit()
            succeeded += 1
        except Exception:
            db.session.rollback()
            failed += 1

    return {"selected": len(targets), "succeeded": succeeded,
            "failed": failed}
