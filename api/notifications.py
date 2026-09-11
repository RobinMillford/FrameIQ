"""Notification service (Feature 04).

Bounded, idempotent notification creation driven by the UpcomingEpisode sync —
NEVER by page rendering. A notification represents a meaningful state
transition (upcoming → aired), so repeated sync executions cannot duplicate
one: dedup happens against existing rows AND via uq_notification_user_episode.

Fan-out is batched: one query for tracked users per batch of shows, then bulk
INSERT ... ON CONFLICT DO NOTHING — never a per-episode × per-user query loop.

The target URL is built server-side from the UpcomingEpisode row itself
(TMDb-sourced season/episode numbers validated by the sync), so no invalid or
future episode URLs can enter the system through client input.
"""
import logging
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from models import db, TVShowProgress, TVEpisodeWatch, UpcomingEpisode
from models.notification import Notification

logger = logging.getLogger(__name__)

#: Watch statuses eligible for new-episode notifications. 'completed' and
#: 'dropped' users deliberately receive nothing (mirrors Feature 02 semantics).
NOTIFY_STATUSES = ('watching', 'paused')


def _watched_episode_keys(pairs):
    """Set of (user_id, season, episode) watched rows for the given
    (user_id, show_id) pairs. One chunked query — no per-user N+1."""
    watched = set()
    CHUNK = 500
    for i in range(0, len(pairs), CHUNK):
        chunk = pairs[i:i + CHUNK]
        conds = [db.and_(TVEpisodeWatch.user_id == uid,
                         TVEpisodeWatch.show_id == sid) for uid, sid in chunk]
        rows = (db.session.query(TVEpisodeWatch.user_id,
                                 TVEpisodeWatch.season_number,
                                 TVEpisodeWatch.episode_number)
                .filter(db.or_(*conds))
                .all())
        for uid, s, e in rows:
            watched.add((uid, s, e))
    return watched


def notify_newly_aired_episodes(now=None):
    """Create notifications for tracked users for episodes that just aired.

    "Just aired" = an UpcomingEpisode whose air_date is <= today and still
    present in the upcoming table. The sync purges `air_date < today` at the
    START of each run, so this window is exactly the fresh transition; the
    unique constraint makes the operation idempotent across runs.

    Returns the number of notifications created.
    """
    now = now or datetime.utcnow()
    today = now.date()

    # Ep1 — episodes that transitioned to "aired" (bounded; next sync will
    # purge them). Ordered for deterministic newest-first notification order.
    aired = (UpcomingEpisode.query
             .filter(UpcomingEpisode.air_date <= today)
             .order_by(UpcomingEpisode.show_id,
                       UpcomingEpisode.season_number,
                       UpcomingEpisode.episode_number)
             .all())
    if not aired:
        return 0

    show_ids = sorted({ep.show_id for ep in aired})

    # Ep2 — one query for ALL eligible trackers of these shows (no N+1).
    trackers = (db.session.query(TVShowProgress.user_id, TVShowProgress.show_id)
                .filter(TVShowProgress.show_id.in_(show_ids),
                        TVShowProgress.status.in_(NOTIFY_STATUSES))
                .all())
    if not trackers:
        return 0

    # Ep3 — only episodes the user has NOT already watched. One chunked query
    # over the relevant (user, show) pairs, then membership-checked in Python.
    pairs = sorted({(uid, sid) for uid, sid in trackers})
    watched = _watched_episode_keys(pairs)

    def _show_name(ep):
        return ep.show_name or f"Show {ep.show_id}"  # TMDb-sourced; fallback is last-resort

    candidates = []
    for ep in aired:
        for uid, sid in trackers:
            if sid != ep.show_id:
                continue
            if (uid, ep.season_number, ep.episode_number) in watched:
                continue  # user already watched it — not "new" for them
            candidates.append((uid, ep))

    # Ep4 — bulk insert, idempotent. (user, type, show, season, episode) is
    # unique; ON CONFLICT DO NOTHING makes re-runs no-ops.
    payloads = [{
        'user_id': uid,
        'type': Notification.TYPE_NEW_EPISODE,
        'title': 'New episode available',
        'body': f"{_show_name(ep)} · S{ep.season_number}E{ep.episode_number} is now available.",
        'target_url': f"/watch/tv/{ep.show_id}/{ep.season_number}/{ep.episode_number}",
        'show_id': ep.show_id,
        'season': ep.season_number,
        'episode': ep.episode_number,
        'episode_name': ep.episode_name,
        'poster_path': ep.poster_path,
    } for uid, ep in candidates]

    created = _bulk_insert_no_conflict(payloads)

    db.session.commit()

    db.session.commit()
    if created:
        logger.info("Notifications: created %d new-episode alerts", created)
    return created


def _bulk_insert_no_conflict(payloads):
    """Bulk INSERT with ON CONFLICT DO NOTHING on the idempotency columns.

    Dialect-aware: Postgres (production) and SQLite (tests) both expose
    on_conflict_do_nothing on their dialect inserts. Column-list form
    (index_elements) targets any unique index over those columns — our
    uq_notification_user_episode — so re-runs are no-ops.
    """
    insert = pg_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
    conflict_cols = ['user_id', 'type', 'show_id', 'season', 'episode']
    created = 0
    CHUNK = 500
    for i in range(0, len(payloads), CHUNK):
        chunk = payloads[i:i + CHUNK]
        result = db.session.execute(
            insert(Notification)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=conflict_cols)
        )
        created += result.rowcount or 0
    return created


def unread_count(user_id):
    """Unread notification count for a user (indexed lookup)."""
    return (Notification.query
            .filter_by(user_id=user_id)
            .filter(Notification.read_at.is_(None))
            .count())


def list_notifications(user_id, limit=30):
    """Newest-first notifications for a user (bounded)."""
    return (Notification.query
            .filter_by(user_id=user_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .all())
