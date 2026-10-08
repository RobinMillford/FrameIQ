"""Task F3 — the profile "up next" label uses the CANONICAL next-episode
resolver (F2 carry-over fix).

Before F3 the profile derived its label from independent
``max(season_number)`` / ``max(episode_number) + 1`` arithmetic over the
watched rows, which invents episodes that do not exist: finishing S1 and S2
of a 10-episode-per-season show produced "S2E11 up next" — there is no
S2E11, the real next episode is S3E1, the first episode of the following
season.

The fix reuses ``api.continue_watching`` — the one canonical next-episode
rule shared with the TV-tracking API and Continue Watching — through its
batched ``next_episode_map()``. Everything here runs fully offline against
deterministic TMDb fixtures (no key, no network).
"""
import re

from tests.tv_fixtures import (
    FUTURE_SHOW, PROFILE_SEASONS, PROFILE_SHOW, SPECIALS_SHOW, login,
    positions, season_positions,
)

_LABEL = re.compile(r">\s*S(\d+)E(\d+) up next\s*<")


def _label(html):
    """The rendered next-episode label as a ``(season, episode)`` tuple."""
    match = _LABEL.search(html)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _profile_html(client, user):
    login(client, user)
    response = client.get("/profile")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def _setup(tv, tmdb, show_id, seasons, watched_positions, *, last_episode=None,
           status="watching", schedule=(), rewatch_positions=()):
    """Register the TMDb payload + build the user's watched ledger."""
    tmdb.tv_show(show_id, seasons,
                 status="Returning Series" if schedule else "Ended",
                 last_episode_to_air=last_episode)
    user = tv.user()
    tv.show(show_id)
    tv.track(user, show_id, status=status)
    if watched_positions:
        tv.watch_many(user, show_id, set(watched_positions))
    if rewatch_positions:
        tv.watch_many(user, show_id, set(rewatch_positions), rewatch=True)
    for season, episode, air_date in schedule:
        tv.schedule(show_id, season, episode, air_date)
    return user


# ════════════════════════════════════════════════════════════════════════════
# The pinned regression: S2E11 must never appear; the answer is S3E1.
# ════════════════════════════════════════════════════════════════════════════

def test_completed_second_season_points_at_the_next_season_premiere(
        app, db, client, tmdb, tv):
    """S1 (10) + S2 (10) finished ⇒ "S3E1 up next", never "S2E11"."""
    watched = season_positions(PROFILE_SEASONS, 1) | \
        season_positions(PROFILE_SEASONS, 2)
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS, watched)

    html = _profile_html(client, user)

    assert _label(html) == (3, 1)
    assert "S2E11" not in html
    assert "up next" in html


def test_the_old_arithmetic_is_gone_from_the_source():
    """Guard the data source itself: no independent max(season)/max(episode)
    label arithmetic may come back to the profile."""
    import inspect

    import routes.auth as auth_mod

    source = inspect.getsource(auth_mod._build_tv_progress_rows)
    assert "next_episode_map" in source
    assert "episode + 1" not in source
    assert "max(TVEpisodeWatch.episode_number)" not in source


# ════════════════════════════════════════════════════════════════════════════
# Spec §28 cases A–F
# ════════════════════════════════════════════════════════════════════════════

def test_case_a_after_the_first_episode(app, db, client, tmdb, tv):
    """CASE A: S1E1 watched ⇒ S1E2."""
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS, {(1, 1)})
    assert _label(_profile_html(client, user)) == (1, 2)


def test_case_b_after_completing_season_one(app, db, client, tmdb, tv):
    """CASE B: complete S1 ⇒ S2E1."""
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS,
                  season_positions(PROFILE_SEASONS, 1))
    assert _label(_profile_html(client, user)) == (2, 1)


def test_case_c_after_completing_season_two(app, db, client, tmdb, tv):
    """CASE C: complete S2 ⇒ S3E1 (the S2E11 regression)."""
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS,
                  season_positions(PROFILE_SEASONS, 1) |
                  season_positions(PROFILE_SEASONS, 2))
    assert _label(_profile_html(client, user)) == (3, 1)


def test_case_d_future_episodes_in_the_current_season(app, db, client, tmdb,
                                                      tv):
    """CASE D: the current season has aired episodes and future ones; the
    next episode is the earliest eligible one, with its aired verdict taken
    from the synced calendar (never invented, never skipped)."""
    from tests.tv_fixtures import FUTURE_OFFSET, PAST_OFFSET, days_from_today

    watched = season_positions(PROFILE_SEASONS, 1) | {(2, 1), (2, 2)}
    last = {"season_number": 2, "episode_number": 2,
            "air_date": days_from_today(PAST_OFFSET)}
    schedule = [
        (2, 3, days_from_today(FUTURE_OFFSET)),
        (2, 4, days_from_today(FUTURE_OFFSET)),
    ]
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS, watched,
                  last_episode=last, schedule=schedule)

    assert _label(_profile_html(client, user)) == (2, 3)


def test_case_e_specials_never_become_the_next_episode(app, db, client, tmdb,
                                                       tv):
    """CASE E: season 0 exists but is never offered as next."""
    seasons = {0: 2, 1: 10, 2: 10}
    user = _setup(tv, tmdb, SPECIALS_SHOW, seasons, {(1, 1)})
    label = _label(_profile_html(client, user))
    assert label == (1, 2)
    assert label[0] != 0

    # A user with no watched episodes at all still never starts on a special.
    fresh = _setup(tv, tmdb, SPECIALS_SHOW, seasons, set())
    label = _label(_profile_html(client, fresh))
    if label is not None:
        assert label[0] != 0


def test_case_f_all_aired_watched_with_a_known_future_episode(app, db, client,
                                                              tmdb, tv):
    """CASE F: every aired episode is watched and the next one is a real,
    not-yet-aired episode — the label points at it (Continue Watching
    semantics), it is not silently dropped."""
    from tests.tv_fixtures import FUTURE_OFFSET, PAST_OFFSET, days_from_today

    watched = season_positions(PROFILE_SEASONS, 1) | {(2, 1), (2, 2)}
    last = {"season_number": 2, "episode_number": 2,
            "air_date": days_from_today(PAST_OFFSET)}
    user = _setup(tv, tmdb, FUTURE_SHOW, PROFILE_SEASONS, watched,
                  last_episode=last,
                  schedule=[(2, 3, days_from_today(FUTURE_OFFSET))])

    html = _profile_html(client, user)
    assert _label(html) == (2, 3)
    assert "100%" in html          # canonical aired set fully watched


# ════════════════════════════════════════════════════════════════════════════
# Safety properties of the surface
# ════════════════════════════════════════════════════════════════════════════

def test_no_next_episode_renders_no_label_and_does_not_crash(
        app, db, client, tmdb, tv):
    """A fully finished show has nothing up next: no label, no bogus S+1."""
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS,
                  positions(PROFILE_SEASONS))
    html = _profile_html(client, user)
    assert _label(html) is None
    assert "up next" not in html
    assert "100%" in html


def test_label_never_emits_an_episode_beyond_the_known_season(
        app, db, client, tmdb, tv):
    """Every emitted label must address an episode TMDb says exists."""
    watched = season_positions(PROFILE_SEASONS, 1) | \
        season_positions(PROFILE_SEASONS, 2)
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS, watched)

    html = _profile_html(client, user)
    label = _label(html)

    assert label is not None
    season, episode = label
    assert season in PROFILE_SEASONS
    assert 1 <= episode <= PROFILE_SEASONS[season]
    # The old bug: episode_number == max(episode) + 1 of a completed season.
    assert episode != PROFILE_SEASONS[2] + 1


def test_rewatch_rows_do_not_advance_the_next_episode(app, db, client, tmdb,
                                                      tv):
    """A rewatch of an old episode is not progress towards the next one."""
    user = _setup(
        tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS,
        season_positions(PROFILE_SEASONS, 1) | {(2, 1)},
        rewatch_positions={(1, 1), (1, 2), (1, 3)})

    assert _label(_profile_html(client, user)) == (2, 2)


def test_next_label_is_not_shared_between_users(app, db, client, tmdb, tv):
    """User B's ledger can never drive the label on user A's profile."""
    tmdb.tv_show(PROFILE_SHOW, PROFILE_SEASONS)
    mine = tv.user("f3-a")
    tv.show(PROFILE_SHOW)
    tv.track(mine, PROFILE_SHOW)

    theirs = tv.user("f3-b")
    tv.track(theirs, PROFILE_SHOW)
    tv.watch_many(theirs, PROFILE_SHOW, season_positions(PROFILE_SEASONS, 1))

    # My own state only: I have watched nothing, so the show points at its
    # first episode — never at THEIR next episode (S2E1).
    assert _label(_profile_html(client, mine)) == (1, 1)


# ════════════════════════════════════════════════════════════════════════════
# Agreement with the TV-tracking API + network budgets
# ════════════════════════════════════════════════════════════════════════════

def test_profile_label_agrees_with_the_next_episode_api(app, db, client, tmdb,
                                                        tv):
    """One canonical answer: the profile label and
    ``GET /api/tv/<id>/next-episode`` cannot disagree."""
    watched = season_positions(PROFILE_SEASONS, 1) | \
        season_positions(PROFILE_SEASONS, 2)
    user = _setup(tv, tmdb, PROFILE_SHOW, PROFILE_SEASONS, watched,
                  status="watching")

    html = _profile_html(client, user)
    payload = client.get(f"/api/tv/{PROFILE_SHOW}/next-episode").get_json()

    assert payload["tracked"] is True
    assert payload["next_episode"] is not None
    assert _label(html) == (payload["next_episode"]["season"],
                            payload["next_episode"]["episode"])


def test_profile_page_makes_one_tmdb_request_per_show(app, db, client, tmdb,
                                                      tv):
    """No N+1 on the network: the label is batched, and repeated page
    renders stay bounded by the shared TMDb cache."""
    user = tv.user()
    show_ids = []
    for offset, seasons in enumerate((PROFILE_SEASONS, {1: 6, 2: 6, 3: 6})):
        show_id = PROFILE_SHOW + 100 + offset
        show_ids.append(show_id)
        tmdb.tv_show(show_id, seasons)
        tv.show(show_id)
        tv.track(user, show_id)
        tv.watch_many(user, show_id, season_positions(seasons, 1))

    html = _profile_html(client, user)
    assert html.count("up next") == 2

    first_render = dict((sid, tmdb.count(rf"^/3/tv/{sid}$"))
                        for sid in show_ids)
    _profile_html(client, user)
    second_render = dict((sid, tmdb.count(rf"^/3/tv/{sid}$"))
                         for sid in show_ids)

    # One details request per show, absorbed by the cache on re-render.
    assert first_render == {sid: 1 for sid in show_ids}
    assert second_render == first_render
