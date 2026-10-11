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
from api.notification_preferences import preferences_for_users, DEFAULTS

logger = logging.getLogger(__name__)

#: Watch statuses eligible for new-episode notifications. 'completed' and
#: 'dropped' users deliberately receive nothing (mirrors Feature 02 semantics).
NOTIFY_STATUSES = ('watching', 'paused')

#: Season premieres are the first episode of a season — the same aired row the
#: new-episode producer already trusts, classified differently. NEVER a second
#: notification for the same event (see _already_notified).


def _is_season_premiere(episode):
    return episode.episode_number == 1 and episode.season_number >= 1


def _classify(episode, prefs):
    """Decide this user's notification type for an aired episode.

    Returns ``(type, gate_category)``, or ``None`` when the user has switched
    off whichever category governs this episode.

    Compatibility rule (Feature F12): a season premiere is an ALTERNATIVE
    PRESENTATION of the first episode, not an extra event. A user who has NOT
    opted into season premieres therefore still receives the ordinary
    new-episode notification for S1E1 — their behaviour is unchanged from
    before F12.
    """
    if _is_season_premiere(episode) and prefs.get('season_premiere'):
        category = 'season_premiere'
        ntype = Notification.TYPE_SEASON_PREMIERE
    else:
        category = 'new_episode'
        ntype = Notification.TYPE_NEW_EPISODE
    if not prefs.get(category, DEFAULTS[category]):
        return None
    return ntype, category


def _already_notified(pairs):
    """(user_id, show_id, season, episode) triples already notified, ANY type.

    Deliberately type-agnostic: without this, a user who receives a plain
    episode notification and later enables season premieres would get a second
    notification for the very same episode, because the uniqueness constraint
    is (user, type, show, season, episode) and the type would differ.
    """
    seen = set()
    CHUNK = 500
    keys = sorted(pairs)
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        conds = [db.and_(
            Notification.user_id == uid,
            Notification.show_id == sid,
            Notification.season == season,
            Notification.episode == episode)
            for uid, sid, season, episode in chunk]
        rows = (db.session.query(Notification.user_id, Notification.show_id,
                                 Notification.season, Notification.episode)
                .filter(db.or_(*conds))
                .all())
        for uid, sid, season, episode in rows:
            seen.add((uid, sid, season, episode))
    return seen


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
    present in the upcoming table.

    Task F4: that table now retains ``AIRED_RETENTION_DAYS`` of aired
    episodes instead of purging them the day after they air, so this scan
    is no longer bounded by "arrived today" — it covers the whole retention
    window. That is safe and intended: the per-(user, show, season, episode)
    unique constraint is what makes the fan-out idempotent, so a wider scan
    re-finds rows it already notified about and skips them. The scan stays
    one bounded SELECT regardless of how long the window is.

    Returns the number of notifications created.
    """
    now = now or datetime.utcnow()
    today = now.date()

    # Ep1 — every retained episode that has aired. Ordered for deterministic
    # newest-first notification order; already-notified rows are filtered out
    # downstream by the unique constraint.
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

    # F12: one batched preference lookup for every candidate user — never a
    # lookup per (user, episode). Users with no stored row resolve to DEFAULTS,
    # which preserves the pre-F12 behaviour exactly.
    candidate_users = {uid for uid, _ in trackers}
    preferences = preferences_for_users(candidate_users)

    # One batched query for everything already notified, so a category change
    # mid-window cannot produce a second notification for the same episode.
    prior = _already_notified({
        (uid, ep.show_id, ep.season_number, ep.episode_number)
        for ep in aired for uid, sid in trackers if sid == ep.show_id})

    candidates = []
    for ep in aired:
        for uid, sid in trackers:
            if sid != ep.show_id:
                continue
            prefs = preferences.get(uid, DEFAULTS)
            decided = _classify(ep, prefs)
            if decided is None:
                continue  # the governing category is switched off
            if (uid, ep.season_number, ep.episode_number) in watched:
                continue  # user already watched it — not "new" for them
            if (uid, ep.show_id, ep.season_number, ep.episode_number) in prior:
                continue  # already notified for this episode, in any type
            candidates.append((uid, ep, decided[0]))

    # Ep4 — bulk insert, idempotent. (user, type, show, season, episode) is
    # unique; ON CONFLICT DO NOTHING makes re-runs no-ops.
    payloads = [_payload(uid, ep, ntype) for uid, ep, ntype in candidates]

    created = _bulk_insert_no_conflict(payloads)

    db.session.commit()

    if created:
        logger.info("Notifications: created %d episode alerts", created)
    return created


def _payload(user_id, episode, notification_type):
    """One notification row. The target URL is server-generated from the
    TMDb-sourced UpcomingEpisode row, never from client input."""
    premiere = notification_type == Notification.TYPE_SEASON_PREMIERE
    if premiere:
        title = 'New season available'
        body = (f"{episode.show_name or f'Show {episode.show_id}'} · Season "
                f"{episode.season_number} premieres with "
                f"S{episode.season_number}E1.")
    else:
        title = 'New episode available'
        body = (f"{episode.show_name or f'Show {episode.show_id}'} · "
                f"S{episode.season_number}E{episode.episode_number} is now "
                f"available.")
    return {
        'user_id': user_id,
        'type': notification_type,
        'title': title,
        'body': body,
        'target_url': (f"/watch/tv/{episode.show_id}/"
                       f"{episode.season_number}/{episode.episode_number}"),
        'show_id': episode.show_id,
        'season': episode.season_number,
        'episode': episode.episode_number,
        'episode_name': episode.episode_name,
        'poster_path': episode.poster_path,
    }


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
