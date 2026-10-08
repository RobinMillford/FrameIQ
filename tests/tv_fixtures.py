"""Shared deterministic TV fixtures for the F3 offline suites.

One owner for TV test data (spec §47): the profile / canonical-invariant /
offline-boundary suites import these builders instead of growing three
overlapping factories.

Everything here is deterministic and offline. Air-date *boundary* fixtures
use offsets from ``date.today()`` on purpose — the application's aired rule is
``air_date <= today``, so "future"/"today" cases can only be expressed
relative to the current date. All ordinary metadata uses fixed dates.

TMDb id bands: 993xxx belongs to the F2 authority suite, 994xxx to F3.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

from models import MediaItem

# ── id bands + canonical season shapes ──────────────────────────────────────
BANSHEE_SHOW = 994001        # completed multi-season show: 10/10/10/8 = 38 aired
PROFILE_SHOW = 994002        # profile next-episode fixture (S1+S2 complete)
RUNNING_SHOW = 994003        # currently airing: 25 aired, then 26
SPECIALS_SHOW = 994004       # season-0 specials + future episodes
FUTURE_SHOW = 994005         # every aired episode watched, future episode exists

BANSHEE_SEASONS = {1: 10, 2: 10, 3: 10, 4: 8}
BANSHEE_AIRED = 38
PROFILE_SEASONS = {1: 10, 2: 10, 3: 10}
# 12 + 13 aired once both seasons have finished airing.
RUNNING_SEASONS = {1: 12, 2: 13}
SPECIALS_SEASONS = {0: 2, 1: 12}     # 12 listed, 10 currently aired
SPECIALS_AIRED = 10

PASSWORD = "F3Offline1"

# Air-date offsets (days from today) for boundary fixtures.
PAST_OFFSET = -30
TODAY_OFFSET = 0
FUTURE_OFFSET = 30


def days_from_today(offset):
    """ISO air date relative to today (see module docstring)."""
    return (date.today() + timedelta(days=offset)).isoformat()


# ── pure helpers ────────────────────────────────────────────────────────────
def positions(seasons):
    """Every ``(season, episode)`` in a ``{season: episode_count}`` map."""
    return {(season, episode)
            for season, count in seasons.items()
            for episode in range(1, count + 1)}


def season_positions(seasons, season):
    """Every ``(season, episode)`` of ONE season in the map."""
    return {(season, episode) for episode in range(1, seasons[season] + 1)}


def all_but(seasons, missing):
    """Every position except the listed ones (a starting watched set)."""
    return positions(seasons) - set(missing)


def login(client, user):
    """Authenticate the test client as ``user`` (CSRF disabled in tests)."""
    return client.post(
        "/login",
        data={"username": user.username, "password": PASSWORD},
        follow_redirects=True,
    )


# ── DB builders ─────────────────────────────────────────────────────────────
def make_user(db, prefix="f3"):
    from models import User

    tag = f"{prefix}-{uuid4().hex[:8]}"
    user = User(username=tag, email=f"{tag}@example.com", email_verified=True)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def get_or_create_media(db, tmdb_id, title=None, media_type="tv",
                        created=None):
    """The MediaItem row for a show, created once.

    A MediaItem row is a cache keyed by (tmdb_id, media_type), so a second
    call in the same test must REUSE the row rather than collide with the
    unique tmdb_id constraint. ``created`` collects newly inserted rows so
    teardown can remove exactly those.
    """
    existing = MediaItem.query.filter_by(
        tmdb_id=tmdb_id, media_type=media_type).first()
    if existing is not None:
        return existing
    item = make_show(db, tmdb_id, title, media_type)
    if created is not None:
        created.append(item)
    return item


def make_show(db, tmdb_id, title=None, media_type="tv"):
    from models import MediaItem

    item = MediaItem(tmdb_id=tmdb_id, media_type=media_type,
                     title=title or f"Fixture Show {tmdb_id}")
    db.session.add(item)
    db.session.commit()
    return item


def track(db, user, show_id, status="watching", total=0, watched=0,
          watched_seasons=0, total_seasons=0):
    """A ``TVShowProgress`` row (status + stored counters, which are never
    the source of truth — the episode ledger is)."""
    from models import TVShowProgress

    row = TVShowProgress(user_id=user.id, show_id=show_id, status=status,
                         total_episodes=total, watched_episodes=watched,
                         watched_seasons=watched_seasons,
                         total_seasons=total_seasons)
    db.session.add(row)
    db.session.commit()
    return row


def watch_many(db, user, show_id, episode_positions, rewatch=False, **kwargs):
    """Insert ``TVEpisodeWatch`` rows for many positions — one commit."""
    from models.tv import TVEpisodeWatch

    rows = [TVEpisodeWatch(user_id=user.id, show_id=show_id,
                           season_number=season, episode_number=episode,
                           is_rewatch=rewatch, **kwargs)
            for season, episode in sorted(episode_positions)]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def watch(db, user, show_id, season, episode, rewatch=False, **kwargs):
    return watch_many(db, user, show_id, [(season, episode)],
                      rewatch=rewatch, **kwargs)[0]


def schedule_episode(db, show_id, season, episode, air_date, name=None):
    """A synced ``UpcomingEpisode`` calendar row.

    ``air_date`` accepts an ISO string (from :func:`days_from_today`) or a
    ``date``; the column is a real ``Date``.
    """
    from datetime import datetime

    from models import UpcomingEpisode

    if isinstance(air_date, str):
        air_date = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
    row = UpcomingEpisode(show_id=show_id, season_number=season,
                          episode_number=episode, air_date=air_date,
                          show_name=f"Fixture Show {show_id}",
                          episode_name=name or f"S{season}E{episode}")
    db.session.add(row)
    db.session.commit()
    return row


# ── the shared `tv` fixture body ────────────────────────────────────────────
# Registered as a pytest fixture in conftest.py (which owns the `db` fixture).
# It lives here rather than in conftest.py so the deterministic TV data and
# the builders that produce it stay in one file.
def tv_builder(db):
    """Per-test TV fixture builder with surgical teardown.

    Returns a small namespace of closures so a test reads as data setup:

        u = tv.user()
        tv.show(BANSHEE_SHOW)
        tv.track(u, BANSHEE_SHOW)
        tv.watch_all(u, BANSHEE_SHOW, BANSHEE_SEASONS)

    Every row created through the builder is deleted afterwards: the suite
    shares one session database, so isolation is the fixture's job.
    """
    created_users = []
    created_media = []
    scheduled_shows = []
    builder = _build_namespace(
        db, created_users, created_media, scheduled_shows)

    yield builder

    _delete_tv_rows(db, created_users, created_media, scheduled_shows)


def _build_namespace(db, created_users, created_media, scheduled_shows):
    """The closure namespace handed to a test as ``tv``.

    Split out of :func:`tv_builder` so the teardown path and the build path
    are each readable on their own.
    """
    def user(prefix="f3"):
        u = make_user(db, prefix)
        created_users.append(u)
        return u

    def show(tmdb_id, title=None, media_type="tv"):
        """Idempotent — see :func:`get_or_create_media`."""
        return get_or_create_media(db, tmdb_id, title, media_type,
                                   created_media)

    def tracked_show(tmdb_id, title=None):
        """A show present in the local MediaItem cache (so no hydration)."""
        show(tmdb_id, title)
        return tmdb_id

    def track_show(u, tmdb_id, **kwargs):
        return track(db, u, tmdb_id, **kwargs)

    def watch_show(u, tmdb_id, season, episode, **kwargs):
        return watch(db, u, tmdb_id, season, episode, **kwargs)

    def watch_many_show(u, tmdb_id, episode_positions, **kwargs):
        return watch_many(db, u, tmdb_id, set(episode_positions), **kwargs)

    def watch_all(u, tmdb_id, seasons, **kwargs):
        return watch_many(db, u, tmdb_id, positions(seasons), **kwargs)

    def watch_season(u, tmdb_id, seasons, season, **kwargs):
        return watch_many(db, u, tmdb_id, season_positions(seasons, season),
                          **kwargs)

    def schedule(tmdb_id, season, episode, air_date, name=None):
        scheduled_shows.append(tmdb_id)
        return schedule_episode(db, tmdb_id, season, episode, air_date, name)

    # A plain namespace, not a class body: `x = x` inside a class body would
    # rebind the name locally and fail (classic gotcha).
    return SimpleNamespace(
        user=user, show=show, tracked_show=tracked_show, track=track_show,
        watch=watch_show, watch_many=watch_many_show, watch_all=watch_all,
        watch_season=watch_season, schedule=schedule,
        users=created_users,
    )


def _delete_tv_rows(db, created_users, created_media, scheduled_shows):
    """Remove exactly the rows ``tv_builder`` created, children first.

    Order matters twice over: lists before their owner (``user_list.user_id``
    is NOT NULL and ``UserList.user`` has no ORM cascade, so a list that
    outlives its user leaves a resident object the NEXT test's commit tries
    to flush as ``user_id = NULL``), and episodes/progress before the user.
    """
    from models import (DiaryEntry, TVShowProgress, UpcomingEpisode, User,
                        UserList, UserListItem, user_viewed)
    from models.tv import TVEpisodeWatch

    db.session.rollback()
    user_ids = [u.id for u in created_users]
    if user_ids:
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        DiaryEntry.query.filter(
            DiaryEntry.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        TVShowProgress.query.filter(
            TVShowProgress.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.user_id.in_(user_ids)))
        # `user_list.user_id` is NOT NULL and `UserList.user` has no ORM
        # cascade, so a list row that outlives its user leaves a resident
        # object that the NEXT test's commit flushes as `user_id = NULL`.
        # No F3 test creates lists today; clear them anyway so a future one
        # cannot detonate in an unrelated module.
        owned = [row[0] for row in db.session.query(UserList.id).filter(
            UserList.user_id.in_(user_ids)).all()]
        if owned:
            UserListItem.query.filter(
                UserListItem.list_id.in_(owned)).delete(synchronize_session=False)
            UserList.query.filter(
                UserList.id.in_(owned)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(user_ids)).delete(
            synchronize_session=False)
    media_ids = [m.id for m in created_media]
    if media_ids:
        MediaItem.query.filter(
            MediaItem.id.in_(media_ids)).delete(synchronize_session=False)
    if scheduled_shows:
        UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(set(scheduled_shows))
        ).delete(synchronize_session=False)
    db.session.commit()
    db.session.expire_all()
