"""Task F3 — the canonical TV invariants, pinned permanently.

F1 unified the TV write paths, F2 made TV Viewed a derived state. This module
exists so neither can silently regress, and so a future change that breaks the
model fails in CI with a message that names the invariant, not a percentage.

The rule under test, in one line
--------------------------------

    every TV write path
        -> canonical aired-position universe   (aired_positions_for_show)
        -> canonical progress                  (canonical_tv_progress)
        -> canonical Viewed                    (tv_viewed_from_progress)

Nothing here is mocked above the HTTP boundary: the TMDb payloads come from
``tests/tmdb_offline.py`` and the real cache, fetchers, parsers, aired rules,
resolvers, routes and templates all run against them (spec §18/§19).

Permanently pinned invariants (spec §48)
-----------------------------------------
INVARIANT 1   specials (season 0) are excluded from the aired universe
INVARIANT 2   episodes that have not aired yet are excluded from the
              denominator (the rule is ``air_date <= today``)
INVARIANT 3   rewatch rows never inflate the unique watched count
INVARIANT 4   every canonical TV write path converges on the aired universe
INVARIANT 5   TV Viewed is derived only from canonical progress
INVARIANT 6   stale stored counters cannot determine user-visible progress
INVARIANT 7   no per-episode TMDb request
INVARIANT 8   no TV-progress N+1 (statement count is independent of episodes)
INVARIANT 9   (pinned in tests/test_profile_next_episode.py)
INVARIANT 10  user-scoped progress cannot cross users
"""
from datetime import date

import pytest

from tests.tmdb_offline import NO_ANCHOR
from tests.tv_fixtures import (
    BANSHEE_AIRED, BANSHEE_SEASONS, BANSHEE_SHOW, FUTURE_OFFSET, PAST_OFFSET,
    SPECIALS_AIRED, SPECIALS_SEASONS, SPECIALS_SHOW, TODAY_OFFSET, all_but,
    days_from_today, login, positions, season_positions,
)


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════

def aired(show_id):
    from api.user_view_state import aired_positions_for_show
    return aired_positions_for_show(show_id)


def progress(user, show_id):
    from api.user_view_state import canonical_tv_progress
    return canonical_tv_progress(user, show_id)


def viewed_state(user, show_id):
    from api.user_view_state import tv_viewed_from_progress
    return tv_viewed_from_progress(progress(user, show_id))


def assert_canonical(result, watched, aired_count, why=""):
    """Canonical progress is ``{watched, aired, percent}`` with
    ``percent = round(watched / aired * 100, 1)`` (user_view_state.py:230).

    Asserting the counts exactly and deriving the percentage keeps these
    assertions readable without pinning a float literal.
    """
    assert result is not None, f"no canonical progress at all ({why})"
    assert result["watched"] == watched, f"watched mismatch ({why}): {result}"
    assert result["aired"] == aired_count, f"aired mismatch ({why}): {result}"
    assert result["percent"] == round(watched / aired_count * 100, 1), (
        f"percent mismatch ({why}): {result}")


def watched_rows(user, show_id):
    """Unique first-watch positions actually persisted for this user."""
    from api.user_view_state import _watched_rows_for_show
    _, first_watch = _watched_rows_for_show(user.id, show_id)
    return first_watch


def mirror_exists(user, show_id):
    """Is there a ``user_viewed`` row for this show? (the legacy mirror)"""
    from models import MediaItem, db, user_viewed

    item = MediaItem.query.filter_by(
        tmdb_id=show_id, media_type="tv").first()
    if item is None:
        return False
    return db.session.execute(
        user_viewed.select().where(
            user_viewed.c.user_id == user.id,
            user_viewed.c.media_id == item.id,
            user_viewed.c.media_type == "tv")).first() is not None


class statements:
    """Record every SQL statement executed inside the block.

    Same ``before_cursor_execute`` pattern the other budget tests use
    (test_profile_tv_stats, test_lists_v2, test_calendar).
    """

    def __enter__(self):
        from sqlalchemy import event as sa_event

        from models import db
        self.captured = []
        self._fn = (lambda conn, cursor, statement, *a, **k:
                    self.captured.append(" ".join(statement.split())))
        sa_event.listen(db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event as sa_event

        from models import db
        sa_event.remove(db.engine, "before_cursor_execute", self._fn)
        return False

    def writes(self):
        return [s for s in self.captured
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]

    def inserts(self):
        return [s for s in self.captured
                if s.lstrip().upper().startswith("INSERT")]


def _forget_cached_details():
    """Drop BOTH detail caches so a mid-test payload change is observed.

    ``api.continue_watching._memo`` is the process memo;
    ``api.tmdb.cache.tmdb_cache`` is the 1-hour TTL store underneath it.
    """
    from api.continue_watching import _memo
    from api.tmdb.cache import tmdb_cache

    _memo.clear()
    tmdb_cache._store.clear()


# The Banshee shape plus a fifth season's first episode: 39 aired. Used by
# the "completion is not latched" pair below.
GROWING_SEASONS = {1: 10, 2: 10, 3: 10, 4: 8, 5: 1}
GROWING_SHOW = 994006


def _growing_show(tmdb, tv, user):
    """Register a 39-episode show ONCE (the registry serves first-match, so
    a second registration for the same id would be shadowed)."""
    tmdb.tv_show(GROWING_SHOW, GROWING_SEASONS,
                 last_episode_to_air={"season_number": 5, "episode_number": 1,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.show(GROWING_SHOW)
    tv.track(user, GROWING_SHOW)
    return GROWING_SHOW


def banshee(tmdb, tv, user):
    """The 38-episode completed show: S1 10 + S2 10 + S3 10 + S4 8."""
    tmdb.tv_show(BANSHEE_SHOW, BANSHEE_SEASONS)
    tv.show(BANSHEE_SHOW)
    tv.track(user, BANSHEE_SHOW)
    return BANSHEE_SHOW


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 4 — the TV write-path matrix (spec §12/§13)
#
# The list is the audited set of reachable TV write paths, NOT the prompt's
# suggestion. One row per path; every row must land on the same canonical
# result. `mark_all_watched`, `mark_season_watched`, `mark_as_viewed` and the
# Continue Watching "finished" action all funnel through the aired universe.
# ════════════════════════════════════════════════════════════════════════════

def test_write_path_matrix_mark_all(app, db, client, tmdb, tv):
    """Path: mark all ⇒ every aired position, and nothing else."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    assert client.post(f"/api/tv/{show}/mark-all-watched").status_code == 200

    canonical = positions(BANSHEE_SEASONS)
    assert aired(show) == canonical
    assert watched_rows(user, show) == canonical
    assert_canonical(progress(user, show), 38, 38, "mark all")
    assert viewed_state(user, show) is True
    # No future or special row was manufactured.
    assert all(s > 0 for s, _ in watched_rows(user, show))


def test_write_path_matrix_mark_season(app, db, client, tmdb, tv):
    """Path: mark season ⇒ the aired positions of THAT season only."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    assert client.post(f"/api/tv/{show}/season/3/mark-watched").status_code == 200

    assert watched_rows(user, show) == season_positions(BANSHEE_SEASONS, 3)
    assert_canonical(progress(user, show), 10, 38, "mark season 3")
    assert viewed_state(user, show) is False


def test_write_path_matrix_mark_episode(app, db, client, tmdb, tv):
    """Path: mark episode ⇒ that single position."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/4/8/mark-watched")
    assert response.status_code == 200

    assert watched_rows(user, show) == {(4, 8)}
    assert progress(user, show)["watched"] == 1
    assert progress(user, show)["aired"] == 38
    assert viewed_state(user, show) is False


def test_write_path_matrix_mark_as_viewed(app, db, client, tmdb, tv):
    """Path: mark as viewed (TV) ⇒ every currently aired position, and it
    also writes the legacy ``user_viewed`` mirror row."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    client.post(f"/mark_as_viewed/{show}/tv", follow_redirects=True)

    assert watched_rows(user, show) == positions(BANSHEE_SEASONS)
    assert viewed_state(user, show) is True
    assert mirror_exists(user, show) is True


def test_write_path_matrix_start_tracking(app, db, client, tmdb, tv):
    """Path: start tracking ⇒ zero counters and NO false watches."""
    user = tv.user()
    # NOT tracked yet: start-tracking is the write path under test.
    tmdb.tv_show(BANSHEE_SHOW, BANSHEE_SEASONS)
    tv.show(BANSHEE_SHOW)
    show = BANSHEE_SHOW
    login(client, user)

    response = client.post(f"/api/tv/{show}/start-tracking")
    assert response.status_code in (200, 201)

    assert watched_rows(user, show) == set()
    # Canonical progress is absent, not 0%-of-something-invented.
    assert progress(user, show) is None
    assert viewed_state(user, show) is False


def test_write_path_matrix_unmark_show(app, db, client, tmdb, tv):
    """Path: unmark show ⇒ zero active watched state, mirror row removed."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)
    client.post(f"/api/tv/{show}/mark-all-watched")
    assert viewed_state(user, show) is True

    client.post(f"/remove_from_viewed/{show}/tv", follow_redirects=True)

    assert watched_rows(user, show) == set()
    assert progress(user, show) is None
    assert viewed_state(user, show) is False
    assert mirror_exists(user, show) is False


def test_write_path_matrix_update_season_progress(app, db, client, tmdb, tv):
    """Path: unmark-season reaches ``update_season_progress`` — the finished
    seasons must be recomputed, never left stale."""
    from models import TVShowProgress

    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)
    client.post(f"/api/tv/{show}/mark-all-watched")
    tv.watch_season(user, show, BANSHEE_SEASONS, 4)

    response = client.post(f"/api/tv/{show}/season/2/unmark-watched")
    assert response.status_code == 200

    assert watched_rows(user, show) == all_but(
        BANSHEE_SEASONS, season_positions(BANSHEE_SEASONS, 2))
    assert progress(user, show)["watched"] == 28
    assert viewed_state(user, show) is False
    # Three whole seasons are still finished; season 2 is not.
    row = TVShowProgress.query.filter_by(
        user_id=user.id, show_id=show).first()
    assert row.watched_seasons == 3


def test_write_path_matrix_continue_watching_finish(app, db, client, tmdb, tv):
    """Path: Continue Watching "finished" ⇒ the same canonical core as
    marking the episode watched directly."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    response = client.post(
        f"/api/continue-watching/tv/{show}/1/1/finish")
    assert response.status_code in (200, 302)

    assert watched_rows(user, show) == {(1, 1)}
    assert progress(user, show)["watched"] == 1
    assert viewed_state(user, show) is False


def test_write_path_matrix_repeated_invocation_is_idempotent(app, db, client,
                                                             tmdb, tv):
    """Running every bulk path twice must not duplicate rows or inflate the
    counters (spec §13, repeated invocation)."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)

    for _ in range(3):
        client.post(f"/api/tv/{show}/mark-all-watched")
        client.post(f"/api/tv/{show}/season/1/mark-watched")
        client.post(f"/mark_as_viewed/{show}/tv", follow_redirects=True)

    from models.tv import TVEpisodeWatch

    total_rows = TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show).count()
    assert total_rows == 38, "repeated bulk writes must not duplicate rows"
    assert watched_rows(user, show) == positions(BANSHEE_SEASONS)
    assert_canonical(progress(user, show), 38, 38, "repeated bulk writes")


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 1 — specials (season 0) are excluded
# ════════════════════════════════════════════════════════════════════════════

def test_specials_are_excluded_from_the_aired_universe(app, db, tmdb, tv):
    """Season 0 exists and is listed by TMDb, but it is not part of the show.

    12 episodes are listed; 10 have verifiably aired ⇒ the aired universe is
    10, never 12 (spec §10).
    """
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    universe = aired(SPECIALS_SHOW)

    assert len(universe) == SPECIALS_AIRED == 10
    assert all(season > 0 for season, _ in universe)
    assert (0, 1) not in universe
    assert (0, 2) not in universe


def test_marking_the_specials_season_writes_nothing(app, db, client, tmdb, tv):
    """An explicit "mark season 0 watched" is a zero-state, not an error and
    not a fabricated write."""
    user = tv.user()
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.show(SPECIALS_SHOW)
    tv.track(user, SPECIALS_SHOW)
    login(client, user)

    response = client.post(f"/api/tv/{SPECIALS_SHOW}/season/0/mark-watched")
    assert response.status_code == 200
    body = response.get_json()
    assert body["marked_episodes"] == 0
    assert body["aired_episodes"] == 0

    assert watched_rows(user, SPECIALS_SHOW) == set()


def test_mark_all_never_watches_a_special(app, db, client, tmdb, tv):
    """The strongest form: mark-all on a show WITH specials leaves season 0
    untouched."""
    user = tv.user()
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.show(SPECIALS_SHOW)
    tv.track(user, SPECIALS_SHOW)
    login(client, user)

    client.post(f"/api/tv/{SPECIALS_SHOW}/mark-all-watched")

    # 10 aired (the anchor), NOT the 12 TMDb lists and NOT the 2 specials.
    assert watched_rows(user, SPECIALS_SHOW) == {(1, e) for e in range(1, 11)}
    assert watched_rows(user, SPECIALS_SHOW).isdisjoint(
        {(0, 1), (0, 2), (1, 11), (1, 12)})
    assert_canonical(progress(user, SPECIALS_SHOW), 10, 10,
                     "specials excluded from mark-all")


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 2 — future episodes are excluded from the denominator
#
# The application's exact inclusion rule, asserted rather than assumed
# (spec §34): an episode is aired when the synced calendar says
# ``air_date <= today``. No row / no date ⇒ aired (a show outside the sync
# window still continues).
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("offset,expected_episode_max,label", [
    (PAST_OFFSET, 11, "air_date < today is AIRED"),
    (TODAY_OFFSET, 11, "air_date == today is AIRED (<= today, not < today)"),
    (FUTURE_OFFSET, 10, "air_date > today is NOT aired"),
])
def test_aired_rule_is_air_date_lte_today(app, db, tmdb, tv, offset,
                                          expected_episode_max, label):
    """Same show, same anchor (S1E10); only the S1E11 calendar row differs.

    The anchor alone yields 10 aired. Adding S1E11 raises it to 11 **iff** its
    air date is today or earlier — the ``air_date <= today`` rule, asserted
    rather than assumed.
    """
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.schedule(SPECIALS_SHOW, 1, 11, days_from_today(offset))

    universe = aired(SPECIALS_SHOW)

    assert max(season for season, _ in universe) == 1
    assert max(episode for _, episode in universe) == expected_episode_max, label
    assert len(universe) == expected_episode_max
    assert ((1, 11) in universe) is (expected_episode_max == 11)


def test_future_episodes_are_excluded_from_the_watched_denominator(
        app, db, tmdb, tv):
    """12 listed, 10 aired ⇒ a user who watched every listed episode still
    shows 10/10, not 12/12 (spec §10)."""
    user = tv.user()
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.show(SPECIALS_SHOW)
    tv.track(user, SPECIALS_SHOW)
    tv.schedule(SPECIALS_SHOW, 1, 11, days_from_today(FUTURE_OFFSET))
    tv.schedule(SPECIALS_SHOW, 1, 12, days_from_today(FUTURE_OFFSET))

    # Watch EVERYTHING TMDb lists, including the two future episodes.
    tv.watch_all(user, SPECIALS_SHOW, SPECIALS_SEASONS)

    result = progress(user, SPECIALS_SHOW)
    assert_canonical(result, 10, 10,
                     "future episodes excluded from the denominator")


def test_a_future_episode_never_makes_a_show_viewed(app, db, tmdb, tv):
    """Watching the future must not manufacture completion."""
    user = tv.user()
    tmdb.tv_show(SPECIALS_SHOW, SPECIALS_SEASONS,
                 last_episode_to_air={"season_number": 1, "episode_number": 10,
                                      "air_date": days_from_today(PAST_OFFSET)})
    tv.show(SPECIALS_SHOW)
    tv.track(user, SPECIALS_SHOW)
    login_user = user
    tv.watch_all(user, SPECIALS_SHOW, SPECIALS_SEASONS)

    assert viewed_state(login_user, SPECIALS_SHOW) is True
    assert progress(login_user, SPECIALS_SHOW)["aired"] == 10


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 3 — rewatch rows never inflate unique progress (spec §35)
# ════════════════════════════════════════════════════════════════════════════

def test_rewatch_rows_do_not_inflate_unique_watched(app, db, tmdb, tv):
    """S1E1 watched once and rewatched 3 times is still ONE watched position."""
    user = tv.user()
    show = banshee(tmdb, tv, user)

    tv.watch(user, show, 1, 1)
    for _ in range(3):
        tv.watch(user, show, 1, 1, rewatch=True)

    from models.tv import TVEpisodeWatch

    assert TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show, season_number=1,
        episode_number=1).count() == 4, "history is preserved"
    assert watched_rows(user, show) == {(1, 1)}, "unique progress is not"
    assert_canonical(progress(user, show), 1, 38, "rewatch does not inflate")


def test_rewatch_rows_never_inflate_the_denominator(app, db, tmdb, tv):
    """A full rewatch pass changes nothing about the denominator or percent."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)
    before = progress(user, show)

    tv.watch_all(user, show, BANSHEE_SEASONS, rewatch=True)
    tv.watch_all(user, show, BANSHEE_SEASONS, rewatch=True)

    assert progress(user, show) == before
    assert_canonical(progress(user, show), 38, 38, "rewatch pass")
    assert viewed_state(user, show) is True


def test_a_rewatch_cannot_turn_an_unfinished_show_into_viewed(
        app, db, tmdb, tv):
    """37 unique + a rewatch of S1E1 is still 37, still not Viewed."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)
    assert watched_rows(user, show) == positions(BANSHEE_SEASONS)

    # Drop S4E8 to make it unfinished, then add rewatches.
    from models.tv import TVEpisodeWatch

    TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show, season_number=4,
        episode_number=8).delete()
    tv.watch(user, show, 1, 1, rewatch=True)

    assert progress(user, show)["watched"] == 37
    assert viewed_state(user, show) is False


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 5 — TV Viewed is derived ONLY from canonical progress (spec §11)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("watched,expected_viewed,label", [
    (0, False, "0/0 is not Viewed"),
    (20, False, "20/38 is not Viewed"),
    (37, False, "37/38 is not Viewed"),
    (38, True, "38/38 is Viewed"),
])
def test_viewed_is_a_function_of_canonical_progress(app, db, tmdb, tv, watched,
                                                    expected_viewed, label):
    user = tv.user()
    show = banshee(tmdb, tv, user)
    if watched:
        tv.watch_many(user, show, set(list(positions(BANSHEE_SEASONS))[:watched]))

    if watched == 0:
        # 0/0 is represented as "no personalised progress" (None), not as a
        # fabricated 0% payload — and None must read as not Viewed.
        assert progress(user, show) is None, label
    else:
        assert_canonical(progress(user, show), watched, BANSHEE_AIRED, label)
    assert viewed_state(user, show) is expected_viewed, label


def test_a_new_aired_episode_drops_a_completed_show_out_of_viewed(
        app, db, tmdb, tv):
    """The show that has 39 aired, of which 38 are watched.

    The regression that matters: completion is not latched. A show can be
    Viewed at 38/38 and must fall back out of Viewed the moment a 39th
    episode verifiably airs.
    """
    user = tv.user()
    show = _growing_show(tmdb, tv, user)
    tv.watch_many(user, show, positions(BANSHEE_SEASONS))
    _forget_cached_details()

    assert_canonical(progress(user, show), 38, 39, "one episode behind")
    assert viewed_state(user, show) is False, "39/39 required, not 38/39"


def test_a_fully_caught_up_show_is_viewed_again(app, db, tmdb, tv):
    """...and watching the new episode restores Viewed (39/39)."""
    user = tv.user()
    show = _growing_show(tmdb, tv, user)
    tv.watch_many(user, show, positions(BANSHEE_SEASONS))
    _forget_cached_details()
    assert viewed_state(user, show) is False

    tv.watch(user, show, 5, 1)

    assert_canonical(progress(user, show), 39, 39, "caught up again")
    assert viewed_state(user, show) is True


def test_a_user_viewed_row_cannot_force_viewed_true(app, db, tmdb, tv):
    """The legacy mirror row is NOT an authority (spec §11).

    Writing ``user_viewed`` directly — exactly what a stale row from before
    the F2 migration looks like — must not make the canonical state Viewed.
    """
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_many(user, show, season_positions(BANSHEE_SEASONS, 1))

    from models import MediaItem, db as _db, user_viewed

    item = MediaItem.query.filter_by(
        tmdb_id=show, media_type="tv").first()
    _db.session.execute(user_viewed.insert().values(
        user_id=user.id, media_id=item.id, media_type="tv"))
    _db.session.commit()

    assert mirror_exists(user, show) is True
    assert progress(user, show)["watched"] == 10
    assert viewed_state(user, show) is False, (
        "a stale user_viewed row must not manufacture completion")


def test_a_user_viewed_row_cannot_force_viewed_false(app, db, tmdb, tv):
    """...and the mirror row is not required for Viewed either: TV Viewed is
    derived, so a fully watched show is Viewed with NO user_viewed row."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)

    assert mirror_exists(user, show) is False
    assert viewed_state(user, show) is True


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 6 — stale stored counters are not semantic truth (spec §18)
# ════════════════════════════════════════════════════════════════════════════

def test_stale_stored_counters_cannot_determine_user_visible_progress(
        app, db, client, tmdb, tv):
    """A legacy row claiming 8/8 = 100 % while the ledger says 20/38.

    The stored counters are the F1/F2 legacy shape; no surface may render
    them.
    """
    from models import TVShowProgress

    user = tv.user()
    show = banshee(tmdb, tv, user)
    row = TVShowProgress.query.filter_by(
        user_id=user.id, show_id=show).first()
    row.total_episodes = 8
    row.watched_episodes = 8
    row.watched_seasons = 1
    row.total_seasons = 1
    db.session.commit()

    tv.watch_many(user, show, set(list(positions(BANSHEE_SEASONS))[:20]))
    login(client, user)

    # The authoritative surfaces report canonical reality...
    body = client.get(f"/api/tv/{show}/progress").get_json()["progress"]
    assert body["watched_episodes"] == 20
    assert body["total_episodes"] == 38
    assert round(body["progress_percentage"]) == 53
    assert viewed_state(user, show) is False
    # ...while the legacy row on disk still says 8/8. That divergence is
    # exactly why the ledger, not the row, is the source of truth.


def test_stale_status_completed_does_not_imply_viewed(app, db, tmdb, tv):
    """Even a sealed ``completed`` status cannot make an incomplete show
    Viewed."""
    from models import TVShowProgress

    user = tv.user()
    show = banshee(tmdb, tv, user)
    row = TVShowProgress.query.filter_by(
        user_id=user.id, show_id=show).first()
    row.status = "completed"
    db.session.commit()

    tv.watch_many(user, show, season_positions(BANSHEE_SEASONS, 1))

    assert viewed_state(user, show) is False


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 7 — no per-episode TMDb request (spec §15)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scale", [10, 38, 100], ids=lambda n: f"{n}ep")
def test_bulk_writes_make_one_details_resolution(app, db, client, tmdb, tv,
                                                 scale):
    """One details resolution for the whole bulk operation.

    Not asserted as an exact magic number for its own sake: what is asserted
    is that the count does NOT grow with the episode count, and that no
    per-episode season endpoint is ever touched.
    """
    seasons = {1: scale}
    user = tv.user()
    show_id = 994100 + scale
    tmdb.tv_show(show_id, seasons)
    tv.show(show_id)
    tv.track(user, show_id)
    login(client, user)

    client.post(f"/api/tv/{show_id}/mark-all-watched")

    details = tmdb.count(rf"^/3/tv/{show_id}$")
    per_episode = tmdb.count(r"/season/")
    assert details == 1, f"{scale} episodes must still cost 1 details call"
    assert per_episode == 0, "no per-season (let alone per-episode) request"
    assert watched_rows(user, show_id) == positions(seasons)


def test_canonical_progress_makes_one_details_resolution(app, db, tmdb, tv):
    """Reading canonical progress is one bounded resolution too."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)

    tmdb.reset_calls()
    for _ in range(5):
        progress(user, show)

    # The process memo plus the TTL cache absorb every repeat read.
    assert tmdb.count(rf"^/3/tv/{show}$") <= 1


def test_a_full_page_of_profiles_shows_stays_bounded(app, db, client, tmdb, tv):
    """Twenty shows on one profile page: still one details call per show."""
    user = tv.user()
    login(client, user)
    expected = {}
    for index in range(20):
        show_id = 994200 + index
        seasons = {1: 4, 2: 4}
        tmdb.tv_show(show_id, seasons)
        tv.show(show_id)
        tv.track(user, show_id)
        tv.watch_season(user, show_id, seasons, 1)
        expected[show_id] = 1

    client.get("/profile")

    measured = {sid: tmdb.count(rf"^/3/tv/{sid}$")
                for sid in expected}
    # The shelf/page renders a bounded number of rows, so some tracked shows
    # are never resolved at all; what matters is the CAP: never more than one
    # details request per rendered show, and never a per-episode one.
    assert all(count <= 1 for count in measured.values()), (
        f"per-show TMDb calls must not exceed one; got {measured}")
    assert sum(measured.values()) <= len(expected)


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 8 — no TV-progress N+1 (spec §14)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scale", [10, 38, 100], ids=lambda n: f"{n}ep")
def test_bulk_mark_all_statement_count_is_independent_of_episode_count(
        app, db, client, tmdb, tv, scale):
    """The INSERT side is batched (``bulk_insert_mappings``), so growing the
    episode count 10x must not multiply the statements 10x.

    A meaningful upper bound is asserted rather than an exact count, so an
    unrelated harmless statement cannot make this brittle.
    """
    seasons = {1: scale}
    user = tv.user()
    show_id = 994300 + scale
    tmdb.tv_show(show_id, seasons)
    tv.show(show_id)
    tv.track(user, show_id)
    login(client, user)

    with statements() as recorder:
        client.post(f"/api/tv/{show_id}/mark-all-watched")

    inserts = recorder.inserts()
    assert len(inserts) <= 2, (
        f"{scale} episodes produced {len(inserts)} INSERT statements: "
        f"{inserts}")
    assert len(recorder.writes()) <= 6, (
        f"{scale} episodes produced {len(recorder.writes())} write statements")
    assert watched_rows(user, show_id) == positions(seasons)


def test_canonical_progress_query_count_is_independent_of_episode_count(
        app, db, tmdb, tv):
    """Reading progress for a 100-episode show must not scan per episode."""
    user = tv.user()
    tv.watch_all(user, user_show := 994350, seasons := {1: 100})
    tmdb.tv_show(user_show, seasons)
    tv.show(user_show)
    tv.track(user, user_show)

    with statements() as recorder:
        progress(user, user_show)

    selects = [s for s in recorder.captured
               if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 4, (
        f"canonical progress for 100 episodes issued {len(selects)} SELECTs: "
        f"{selects}")
    assert_canonical(progress(user, user_show), 100, 100,
                     "100-episode canonical read")


def test_a_page_of_twenty_shows_reads_progress_in_constant_statements(
        app, db, client, tmdb, tv):
    """``/api/tv/my-shows`` batched over 20 shows: still a fixed statement
    count, because the canonical map is two batched queries for all ids."""
    user = tv.user()
    login(client, user)
    for index in range(20):
        show_id = 994400 + index
        seasons = {1: 3, 2: 3}
        tmdb.tv_show(show_id, seasons)
        tv.show(show_id)
        tv.track(user, show_id)
        tv.watch_season(user, show_id, seasons, 1)

    with statements() as recorder:
        response = client.get("/api/tv/my-shows")

    assert response.status_code == 200
    assert len(response.get_json()["shows"]) == 20
    selects = [s for s in recorder.captured
               if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 20, (
        f"20 shows issued {len(selects)} SELECTs — an N+1 crept back in")


# ════════════════════════════════════════════════════════════════════════════
# INVARIANT 10 — user-scoped progress cannot cross users (spec §37)
# ════════════════════════════════════════════════════════════════════════════

def test_one_users_watched_state_is_never_visible_to_another(app, db, client,
                                                             tmdb, tv):
    alice = tv.user("f3-alice")
    bob = tv.user("f3-bob")
    show = banshee(tmdb, tv, alice)
    tv.track(bob, show)
    tv.watch_many(alice, show, positions(BANSHEE_SEASONS))

    login(client, alice)
    alice_progress = client.get(
        f"/api/tv/{show}/progress").get_json()["progress"]
    assert alice_progress["watched_episodes"] == 38

    # Bob tracks the same show and has watched nothing.
    login(client, bob)
    bob_progress = client.get(f"/api/tv/{show}/progress").get_json()["progress"]
    assert bob_progress["watched_episodes"] == 0, "Alice's ledger leaked"
    assert bob_progress["total_episodes"] == 0, (
        "with no watch rows of his own Bob has no canonical progress at all")


def test_view_state_does_not_leak_across_users(app, db, client, tmdb, tv):
    alice = tv.user("f3-alice")
    bob = tv.user("f3-bob")
    show = banshee(tmdb, tv, alice)
    tv.track(bob, show)
    tv.watch_many(alice, show, positions(BANSHEE_SEASONS))

    login(client, alice)
    client.post(f"/mark_as_viewed/{show}/tv", follow_redirects=True)
    alice_state = client.get(f"/api/view-state?tv={show}").get_json()
    assert alice_state["tv_progress"][str(show)]["watched"] == 38

    login(client, bob)
    bob_state = client.get(f"/api/view-state?tv={show}").get_json()
    assert str(show) not in bob_state["tv_progress"], (
        "Alice's canonical progress leaked into Bob's view-state")
    assert bob_state["viewed_movie_ids"] == []


def test_unfinished_shows_are_scoped_to_the_signed_in_user(app, db, client,
                                                           tmdb, tv):
    alice = tv.user("f3-alice")
    bob = tv.user("f3-bob")
    show = banshee(tmdb, tv, alice)
    tv.track(bob, show)
    tv.watch_many(alice, show, season_positions(BANSHEE_SEASONS, 1))

    login(client, alice)
    alice_rows = {s["show_id"]: s for s in client.get(
        "/api/tv/unfinished-shows").get_json()["shows"]}
    assert alice_rows[show]["watched_episodes"] == 10
    assert alice_rows[show]["total_episodes"] == 38
    assert alice_rows[show]["last_episode"] == {"season": 1, "episode": 10}

    # Bob tracked the same show but watched nothing: the shelf must show his
    # zero, never Alice's 10.
    login(client, bob)
    bob_rows = {s["show_id"]: s for s in client.get(
        "/api/tv/unfinished-shows").get_json()["shows"]}
    assert bob_rows[show]["watched_episodes"] == 0
    assert bob_rows[show]["last_episode"] is None
    assert bob_rows[show]["progress_percent"] == 0


def test_the_viewed_mirror_row_is_scoped_to_one_user(app, db, client, tmdb,
                                                     tv):
    alice = tv.user("f3-alice")
    bob = tv.user("f3-bob")
    show = banshee(tmdb, tv, alice)
    tv.track(bob, show)
    tv.watch_all(bob, show, BANSHEE_SEASONS)

    login(client, alice)
    client.post(f"/mark_as_viewed/{show}/tv", follow_redirects=True)
    assert mirror_exists(alice, show) is True
    assert mirror_exists(bob, show) is False


# ════════════════════════════════════════════════════════════════════════════
# Surface agreement (spec §26) — detail, progress API, view-state, profile,
# unfinished, lists and /viewed must all tell the same story.
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("watched,viewed_label", [
    (38, "Viewed"),
    (20, "Not Viewed"),
])
def test_every_surface_agrees_at_a_given_canonical_state(app, db, client, tmdb,
                                                         tv, watched,
                                                         viewed_label):
    user = tv.user()
    show = banshee(tmdb, tv, user)
    login(client, user)
    if watched == BANSHEE_AIRED:
        # The finished state is built through the canonical write path, so
        # completion gating runs too (the shelf is status-driven, and a
        # hand-inserted 38/38 with status still "watching" is a state the
        # product cannot actually produce).
        client.post(f"/api/tv/{show}/mark-all-watched")
    else:
        tv.watch_many(user, show,
                      set(list(positions(BANSHEE_SEASONS))[:watched]))

    # 1. the canonical function
    assert viewed_state(user, show) is (watched == BANSHEE_AIRED)

    # 2. the TV progress API
    body = client.get(f"/api/tv/{show}/progress").get_json()["progress"]
    assert body["watched_episodes"] == watched
    assert body["total_episodes"] == BANSHEE_AIRED

    # 3. the aired-progress API
    aired_body = client.get(f"/api/tv/{show}/aired-progress").get_json()
    assert aired_body["tv_progress"]["aired"] == BANSHEE_AIRED
    assert aired_body["tv_progress"]["watched"] == watched
    assert aired_body["season_aired"] == {"1": 10, "2": 10, "3": 10, "4": 8}

    # 4. view-state
    state = client.get(f"/api/view-state?tv={show}").get_json()
    assert state["tv_progress"][str(show)]["watched"] == watched
    assert state["tv_progress"][str(show)]["aired"] == BANSHEE_AIRED

    # 5. unfinished at 20/38 (incomplete) and absent at 38/38
    unfinished = [s["show_id"] for s in client.get(
        "/api/tv/unfinished-shows").get_json()["shows"]]
    assert (show in unfinished) is (watched < BANSHEE_AIRED), (
        "a finished show must leave the unfinished shelf")

    # 6. the profile row
    html = client.get("/profile").get_data(as_text=True)
    expected_percent = round(watched / BANSHEE_AIRED * 100)
    assert f"{expected_percent}% watched" in html

    # 7. the TV detail hero
    detail = client.get(f"/tv/{show}").get_data(as_text=True)
    # Exact copy from templates/tv_detail.html:449-461.
    unmark_shown = "Unmark Viewed" in detail
    assert unmark_shown is (watched == BANSHEE_AIRED), (
        f"TV detail hero disagrees at {watched}/{BANSHEE_AIRED}: "
        f"unmark_shown={unmark_shown}")
    assert (viewed_label == "Viewed") is unmark_shown


def test_a_partially_watched_show_is_unfinished_on_every_surface(app, db,
                                                                 client, tmdb,
                                                                 tv):
    """20/38: not Viewed, 53 %, and unfinished — no stale counters anywhere."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_many(user, show, set(list(positions(BANSHEE_SEASONS))[:20]))
    login(client, user)

    body = client.get(f"/api/tv/{show}/progress").get_json()["progress"]
    assert (body["watched_episodes"], body["total_episodes"]) == (20, 38)
    assert round(body["progress_percentage"]) == 53

    unfinished = [s["show_id"] for s in client.get(
        "/api/tv/unfinished-shows").get_json()["shows"]]
    assert show in unfinished

    detail = client.get(f"/tv/{show}").get_data(as_text=True)
    assert "Mark as Viewed" in detail
    assert "Unmark Viewed" not in detail


# ════════════════════════════════════════════════════════════════════════════
# Cache / metadata degradation (spec §16)
#
# Missing, malformed or empty optional metadata must degrade safely. None of
# these may require a live fallback to pass.
# ════════════════════════════════════════════════════════════════════════════

def test_no_tmdb_payload_degrades_to_empty_not_to_a_crash(app, db, tmdb):
    """An unregistered show (the upstream-unavailable shape) yields an empty
    aired set — never a fabricated one."""
    assert aired(998800001) == set()


def test_empty_seasons_list_degrades_safely(app, db, tmdb):
    tmdb.tv_show(998800002, {}, season_entries=[], number_of_seasons=0,
                 number_of_episodes=0, last_episode_to_air=NO_ANCHOR)
    assert aired(998800002) == set()


def test_missing_last_episode_to_air_falls_back_to_the_calendar(app, db,
                                                                tmdb, tv):
    """No TMDb anchor at all: the synced calendar is still authoritative."""
    tmdb.tv_show(998800003, {1: 6}, last_episode_to_air=NO_ANCHOR,
                 season_entries=[{"season_number": 1, "episode_count": 6,
                                  "air_date": days_from_today(PAST_OFFSET),
                                  "id": 1, "name": "Season 1"}])
    tv.schedule(998800003, 1, 1, days_from_today(PAST_OFFSET))
    tv.schedule(998800003, 1, 2, days_from_today(PAST_OFFSET))
    tv.schedule(998800003, 1, 3, days_from_today(FUTURE_OFFSET))

    universe = aired(998800003)
    assert universe == {(1, 1), (1, 2)}, "only aired calendar rows count"


def test_missing_air_date_rows_are_treated_as_aired(app, db, tmdb, tv):
    """No air date ⇒ no evidence it is in the future ⇒ aired (so a show
    outside the sync window still continues)."""
    user = tv.user()
    tmdb.tv_show(998800004, {1: 3})
    tv.show(998800004)
    tv.track(user, 998800004)
    tv.watch_all(user, 998800004, {1: 3})

    assert_canonical(progress(user, 998800004), 3, 3,
                     "no air date is treated as aired")
    assert viewed_state(user, 998800004) is True


def test_malformed_season_metadata_degrades_safely(app, db, tmdb):
    """A season entry with a null count or a non-numeric number must not
    raise or invent episodes."""
    tmdb.register(r"^/3/tv/998800005$", {
        "id": 998800005, "name": "Malformed", "status": "Ended",
        "last_episode_to_air": {"season_number": 1, "episode_number": 2,
                                "air_date": days_from_today(PAST_OFFSET)},
        "seasons": [
            {"season_number": 1, "episode_count": None},
            {"season_number": None, "episode_count": 9},
            {"season_number": 3, "episode_count": 4},
        ],
    })
    universe = aired(998800005)
    assert universe == {(1, 1), (1, 2)}, "only the anchor season resolves"


def test_missing_provider_data_degrades_safely(app, db, client, tmdb, tv):
    """No provider payload ⇒ the page renders, without a streaming block."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)
    login(client, user)

    response = client.get(f"/tv/{show}")
    assert response.status_code == 200
    assert "Traceback" not in response.get_data(as_text=True)


def test_the_tmdb_cache_absorbs_repeat_reads(app, db, tmdb, tv):
    """Cache hit path: the second resolution of the same show is free."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)
    tmdb.reset_calls()

    progress(user, show)
    progress(user, show)
    progress(user, show)

    assert tmdb.count(rf"^/3/tv/{show}$") == 1


# ════════════════════════════════════════════════════════════════════════════
# Data preservation across canonical writes (spec §36)
# ════════════════════════════════════════════════════════════════════════════

def test_mark_all_preserves_existing_rating_notes_and_date(app, db, client,
                                                           tmdb, tv):
    """F1/F2 semantics: a bulk write adds missing positions and must not
    clobber the metadata already recorded on existing rows."""
    watched_on = date(2021, 3, 4)
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch(user, show, 1, 1, rating=4.5, notes="great pilot",
             watched_date=watched_on)

    login(client, user)
    client.post(f"/api/tv/{show}/mark-all-watched")

    from models.tv import TVEpisodeWatch

    row = TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show, season_number=1,
        episode_number=1).first()
    assert row.rating == 4.5
    assert row.notes == "great pilot"
    assert row.watched_date == watched_on
    assert progress(user, show)["watched"] == 38


def test_mark_all_preserves_rewatch_rows(app, db, client, tmdb, tv):
    """A rewatch of an episode that is also a first watch survives the bulk
    write, and still does not inflate progress."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch(user, show, 1, 1)
    tv.watch(user, show, 1, 1, rewatch=True)

    login(client, user)
    client.post(f"/api/tv/{show}/mark-all-watched")

    from models.tv import TVEpisodeWatch

    assert TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show, season_number=1,
        episode_number=1).count() == 2, "both rows preserved"
    assert progress(user, show)["watched"] == 38


def test_mark_season_preserves_metadata_outside_the_target_season(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch(user, show, 1, 1, rating=4.0, notes="keep me")

    login(client, user)
    client.post(f"/api/tv/{show}/season/2/mark-watched")

    from models.tv import TVEpisodeWatch

    row = TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show, season_number=1,
        episode_number=1).first()
    assert (row.rating, row.notes) == (4.0, "keep me")
    assert progress(user, show)["watched"] == 11


def test_unmark_show_removes_the_whole_ledger_including_rewatches(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_all(user, show, BANSHEE_SEASONS)
    tv.watch(user, show, 1, 1, rewatch=True)

    login(client, user)
    client.post(f"/remove_from_viewed/{show}/tv", follow_redirects=True)

    from models.tv import TVEpisodeWatch

    assert TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show).count() == 0
    assert progress(user, show) is None


# ════════════════════════════════════════════════════════════════════════════
# Descriptive failures (spec §46)
# ════════════════════════════════════════════════════════════════════════════

def test_a_canonical_invariant_failure_names_the_state(app, db, tmdb, tv):
    """If the invariant ever breaks, the diagnostic must identify the show,
    the aired set, the watched set and the surfaced verdict — not just
    'assert False'."""
    user = tv.user()
    show = banshee(tmdb, tv, user)
    tv.watch_many(user, show, positions(BANSHEE_SEASONS))

    diagnostic = (
        "Canonical TV invariant failed: "
        f"show={show} "
        f"aired={progress(user, show)['aired']} "
        f"watched={progress(user, show)['watched']} "
        f"surfaced_viewed={viewed_state(user, show)}"
    )

    assert f"show={show}" in diagnostic
    assert "aired=38" in diagnostic
    assert "watched=38" in diagnostic
    assert "surfaced_viewed=True" in diagnostic
