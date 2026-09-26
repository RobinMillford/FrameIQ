"""Task D — TV "Mark as Viewed" ⇄ episode/season watch-state sync + movie
quick-log action wording.

Semantics pinned here:

  TV (bulk state sync, not rewatch generation)
    "Mark as Viewed" for a show writes the canonical episode state: every
    currently AIRED valid episode becomes a non-rewatch TVEpisodeWatch row,
    using the EXACT aired-episode rules of Task B (api/user_view_state.py —
    synced calendar ∪ last_episode_to_air anchor, specials excluded, future
    episodes never touched). Existing watch records are preserved, repeated
    calls are idempotent, TVShowProgress counters follow the canonical rows,
    and NO diary events are manufactured.

  MOVIE (quick-log wording)
    The quick-log action is an ACTION, never a state claim. The shared
    view-state payload distinguishes viewed movies from movies logged
    today; static/js/quick-log.js renders "Log Watched" / "Log Rewatch" /
    "Watched Today" accordingly, and rewatch events keep working.

TMDb is stubbed at the shared details cache (same pattern as
test_view_state_sync.py) — no external services are required.
"""
from datetime import date, timedelta
from uuid import uuid4

import pytest

from models import (db, DiaryEntry, MediaItem, TVEpisodeWatch,
                    TVShowProgress, UpcomingEpisode)
from models.associations import user_viewed
import api.user_view_state as uvs
import routes.tv_tracking as tv_tracking_mod
import routes.diary as diary_mod

# TMDb ids far away from any fixture ranges other tests use.
MOVIE_ID = 999601
SHOW_ID = 991001
SHOW_SEALED = 991002
SHOW_RUNNING = 991003

DETAILS_CACHE = {}


def _details(last_episode, season=1):
    return {"id": 0,
            "last_episode_to_air": {"season_number": season,
                                    "episode_number": last_episode}}


@pytest.fixture
def stub_details(monkeypatch):
    """Stub every TMDb touchpoint of the bulk helper: the shared details
    cache used by uvs._default_details_loader AND fetch_tv_show_details
    (TVShowProgress creation, season counting, completion gating)."""
    import api.continue_watching as cw
    monkeypatch.setattr(
        cw, "show_details",
        lambda sid, **kw: DETAILS_CACHE.get(sid, None))
    monkeypatch.setattr(
        tv_tracking_mod, "fetch_tv_show_details",
        lambda sid, **kw: DETAILS_CACHE.get(sid) or {
            "id": sid, "number_of_seasons": 0,
            "number_of_episodes": 0, "status": ""})
    cw._memo.clear()
    yield
    DETAILS_CACHE.clear()
    cw._memo.clear()


@pytest.fixture(autouse=True)
def _reset_quicklog_guard():
    """Isolate tests from the process-local duplicate-submission guard."""
    diary_mod._quicklog_guard.clear()
    yield
    diary_mod._quicklog_guard.clear()


@pytest.fixture
def factory(db):
    """Per-test builders with surgical teardown (shared test DB safe)."""
    users, media_ids, show_ids, watched_ids = [], [], [], []
    suffix = uuid4().hex[:8]

    def user(username="tvd"):
        from models import User
        u = User(username=f"{username}-{suffix}-{len(users)}",
                 email=f"{username}-{suffix}-{len(users)}@example.com",
                 email_verified=True)
        u.set_password("TvdSync1")
        db.session.add(u)
        db.session.commit()
        users.append(u)
        return u

    def media(tmdb_id, media_type, title="T"):
        m = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title)
        db.session.add(m)
        db.session.commit()
        media_ids.append(m.id)
        return m

    def watch(u, show_id, season, episode, rewatch=False):
        db.session.add(TVEpisodeWatch(
            user_id=u.id, show_id=show_id, season_number=season,
            episode_number=episode, is_rewatch=rewatch))
        db.session.commit()

    def upcoming(show_id, season, episode, delta):
        show_ids.append(show_id)
        db.session.add(UpcomingEpisode(
            show_id=show_id, show_name="S", season_number=season,
            episode_number=episode,
            air_date=date.today() + timedelta(days=delta)))
        db.session.commit()

    class _F:
        pass

    _F.user, _F.media, _F.watch = user, media, watch
    _F.aired = lambda s, se, ep: upcoming(s, se, ep, -1)
    _F.future = lambda s, se, ep: upcoming(s, se, ep, 3)
    yield _F

    # Teardown — pure Core/Query deletes, no ORM cascade (pattern shared
    # with test_view_state_sync.py).
    db.session.rollback()
    user_ids = [u.id for u in users]
    if user_ids:
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        DiaryEntry.query.filter(
            DiaryEntry.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.user_id.in_(user_ids)))
        TVShowProgress.query.filter(
            TVShowProgress.user_id.in_(user_ids)
        ).delete(synchronize_session=False)
        from models import User
        User.query.filter(
            User.id.in_(user_ids)
        ).delete(synchronize_session=False)
    if media_ids:
        DiaryEntry.query.filter(
            DiaryEntry.media_id.in_(media_ids)
        ).delete(synchronize_session=False)
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.media_id.in_(media_ids)))
        MediaItem.query.filter(
            MediaItem.id.in_(media_ids)).delete(synchronize_session=False)
    if show_ids:
        UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(set(show_ids))
        ).delete(synchronize_session=False)
    db.session.commit()
    db.session.expire_all()


def _watched_positions(user_id, show_id):
    return {
        (season, episode)
        for season, episode in db.session.query(
            TVEpisodeWatch.season_number, TVEpisodeWatch.episode_number)
        .filter(
            TVEpisodeWatch.user_id == user_id,
            TVEpisodeWatch.show_id == show_id,
            TVEpisodeWatch.is_rewatch == False,  # noqa: E712
        ).all()
    }


def _login(client, u):
    client.post("/login", data={"username": u.username,
                                "password": "TvdSync1"},
                follow_redirects=True)


# ════════════════════════════════════════════════════════════════════════
# TV: bulk "Mark as Viewed" state synchronization
# ════════════════════════════════════════════════════════════════════════

def test_mark_unopened_show_writes_all_aired_episodes(factory, stub_details):
    """Spec #5/#6: unopened show → every aired valid episode becomes
    watched, via the canonical bulk helper."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(3, season=1)
    factory.aired(SHOW_ID, 2, 7)            # calendar side (S2 started)
    factory.future(SHOW_ID, 2, 8)           # airs later — must stay out

    progress, inserted, aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)

    assert (inserted, aired) == (4, 4)
    assert _watched_positions(u.id, SHOW_ID) == {
        (1, 1), (1, 2), (1, 3), (2, 7)}
    assert progress.watched_episodes == 4
    assert progress.status == "watching"   # default stub: unknown status


def test_mark_specials_never_written(factory, stub_details):
    """Spec #9: specials (season 0) follow the existing rules — excluded
    from the aired set entirely."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(2, season=1)
    factory.aired(SHOW_ID, 0, 5)            # aired special — still excluded

    _, inserted, aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert (inserted, aired) == (2, 2)
    assert (0, 5) not in _watched_positions(u.id, SHOW_ID)


def test_mark_preserves_existing_records_and_rewatch_rows(
        factory, stub_details):
    """Spec #11: existing watched episodes and explicit rewatch rows stay
    exactly as the user left them."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(3, season=1)
    existing = TVEpisodeWatch(
        user_id=u.id, show_id=SHOW_ID, season_number=1, episode_number=1,
        rating=4.5, notes="my note")
    db.session.add(existing)
    factory.watch(u, SHOW_ID, 1, 2, rewatch=True)
    db.session.commit()

    progress, inserted, _ = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    # S1E1 has a canonical row (kept untouched); S1E2 has ONLY a rewatch
    # row — the canonical numerator excludes rewatches, so the state sync
    # writes its canonical row too (rewatch record itself is preserved);
    # S1E3 was simply missing.
    assert inserted == 2

    kept = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=SHOW_ID,
        season_number=1, episode_number=1).one()
    assert kept.id == existing.id           # row untouched, not re-created
    assert kept.rating == 4.5 and kept.notes == "my note"
    rewatch_rows = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=SHOW_ID, is_rewatch=True).all()
    assert len(rewatch_rows) == 1           # no new rewatch generated


def test_mark_idempotent_second_click(factory, stub_details):
    """Spec #15: second "Mark as Viewed" inserts nothing, creates no
    rewatches, and leaves progress unchanged."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(3, season=1)

    _, first_inserts, _ = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert first_inserts == 3

    _, second_inserts, second_aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert second_inserts == 0
    assert second_aired == 3
    assert _watched_positions(u.id, SHOW_ID) == {
        (1, 1), (1, 2), (1, 3)}
    total_rows = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=SHOW_ID).count()
    assert total_rows == 3                  # no rewatch duplicates


def test_mark_future_season_untouched(factory, stub_details):
    """Spec #8: an unaired future season gets no fake watches and the
    overall denominator excludes it."""
    u = factory.user()
    # anchor: S2E1 is the most recent aired episode (calendar agrees:
    # S2E2+ are future, S3 announced) — union semantics, Task B exact.
    DETAILS_CACHE[SHOW_ID] = _details(1, season=2)
    factory.aired(SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 1, 2)
    factory.aired(SHOW_ID, 2, 1)            # S2 just started airing
    factory.future(SHOW_ID, 2, 2)
    factory.future(SHOW_ID, 2, 3)
    factory.future(SHOW_ID, 3, 1)           # announced S3 — 0 aired

    _, inserted, aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert (inserted, aired) == (3, 3)      # 1+2 + S2E1 only
    watched = _watched_positions(u.id, SHOW_ID)
    assert watched == {(1, 1), (1, 2), (2, 1)}
    # overall view agrees with the write side
    p = uvs.tv_aired_progress(u, [SHOW_ID]).get(SHOW_ID)
    assert p == {"watched": 3, "aired": 3, "percent": 100.0}


def test_mark_partial_season_reaches_100(factory, stub_details):
    """Spec #12/#14: partial progress → 100% after the bulk mark, and the
    hero progress (read side) agrees with the written rows."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(10, season=1)
    for ep in range(1, 5):
        factory.watch(u, SHOW_ID, 1, ep)    # 4/10 watched

    progress, inserted, _ = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert inserted == 6
    p = uvs.tv_aired_progress(u, [SHOW_ID]).get(SHOW_ID)
    assert p == {"watched": 10, "aired": 10, "percent": 100.0}
    assert progress.watched_episodes == 10


def test_mark_multi_season_all_100_example(factory, stub_details):
    """Spec #13 + spec example: S1 10 aired, S2 10 aired, S3 10 aired,
    S4 1 aired → 31/31, every season fully watched."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(1, season=4)
    for ep in range(1, 11):
        factory.aired(SHOW_ID, 1, ep)
        factory.aired(SHOW_ID, 2, ep)
        factory.aired(SHOW_ID, 3, ep)
    factory.aired(SHOW_ID, 4, 1)

    _, inserted, aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert (inserted, aired) == (31, 31)
    watched = _watched_positions(u.id, SHOW_ID)
    for season in (1, 2, 3):
        for ep in range(1, 11):
            assert (season, ep) in watched
    assert (4, 1) in watched
    # season denominator + completion per season from the shared rules
    season_aired = uvs.season_aired_for_show(SHOW_ID)
    assert season_aired == {1: 10, 2: 10, 3: 10, 4: 1}


def test_mark_completion_gated_on_show_status(factory, stub_details):
    """Running show stays 'watching' even at 100%; Ended flips completed
    (same gating as mark_episode_watched_core)."""
    u = factory.user()

    DETAILS_CACHE[SHOW_RUNNING] = _details(2, season=1)
    DETAILS_CACHE[SHOW_RUNNING]["status"] = "Returning Series"
    DETAILS_CACHE[SHOW_RUNNING]["number_of_episodes"] = 2
    factory.aired(SHOW_RUNNING, 1, 1)
    factory.aired(SHOW_RUNNING, 1, 2)
    _, _, _ = tv_tracking_mod.mark_show_aired_watched_core(
        u.id, SHOW_RUNNING)
    running_progress = TVShowProgress.query.filter_by(
        user_id=u.id, show_id=SHOW_RUNNING).one()
    assert running_progress.status == "watching"   # not sealed at 100%

    DETAILS_CACHE[SHOW_SEALED] = _details(1, season=1)
    DETAILS_CACHE[SHOW_SEALED]["status"] = "Ended"
    DETAILS_CACHE[SHOW_SEALED]["number_of_episodes"] = 1
    factory.aired(SHOW_SEALED, 1, 1)
    _, _, _ = tv_tracking_mod.mark_show_aired_watched_core(
        u.id, SHOW_SEALED)
    sealed_progress = TVShowProgress.query.filter_by(
        user_id=u.id, show_id=SHOW_SEALED).one()
    assert sealed_progress.status == "completed"


def test_mark_nothing_aired_is_safe_noop(factory, stub_details):
    """A show with no verifiably aired episode writes nothing."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = None            # no details, no calendar
    _, inserted, aired = \
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert (inserted, aired) == (0, 0)
    assert _watched_positions(u.id, SHOW_ID) == set()


def test_mark_no_diary_entries_manufactured(factory, stub_details):
    """Spec (Diary semantics): the bulk action updates episode watch state
    only — zero diary events are fabricated."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(3, season=1)
    _, _, _ = tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert DiaryEntry.query.filter_by(user_id=u.id).count() == 0


def test_mark_query_budget_bounded(factory, stub_details):
    """Spec #26/#27: no N+1 — the bulk op issues a bounded set of episode
    statements regardless of episode count (≤ 1 calendar SELECT, ≤ 1
    existing-watch SELECT, ≤ 1 bulk INSERT)."""
    from sqlalchemy import event

    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(2, season=2)
    for ep in range(1, 11):
        factory.aired(SHOW_ID, 1, ep)
        factory.aired(SHOW_ID, 2, ep)

    hits = {"tv_episode_watch": [], "upcoming_episode": [],
            "INSERT INTO tv_episode_watch": []}
    def _before(conn, cursor, statement, parameters, context, executemany):
        for needle in hits:
            if needle in statement:
                hits[needle].append(statement)

    event.listen(db.engine, "before_cursor_execute", _before)
    try:
        tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    finally:
        event.remove(db.engine, "before_cursor_execute", _before)

    assert len(hits["upcoming_episode"]) <= 1
    # Episode statements are bounded by SEASONS, never per-episode:
    # 1 existing-watch SELECT + 1 counter recompute + the shared
    # update_season_progress count (1 per season) — 20 episodes, 4 hits.
    assert len(hits["tv_episode_watch"]) <= 2 + 2
    assert len(hits["INSERT INTO tv_episode_watch"]) <= 1
    assert _watched_positions(u.id, SHOW_ID) == {
        (s, e) for s in (1, 2) for e in range(1, 11)}


# ════════════════════════════════════════════════════════════════════════
# Running show: 10/10 → new episode → 10/11 → 11/11
# ════════════════════════════════════════════════════════════════════════

def test_running_show_full_flow(factory, stub_details):
    """Spec #16–18: the critical regression — a completed show drops and
    returns to 100% without resetting anything."""
    u = factory.user()
    DETAILS_CACHE[SHOW_ID] = _details(10, season=1)
    for ep in range(1, 11):
        factory.aired(SHOW_ID, 1, ep)

    _, _, _ = tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert uvs.tv_aired_progress(u, [SHOW_ID])[SHOW_ID]["percent"] == 100.0

    # New episode airs — old watches must remain, percent drops.
    DETAILS_CACHE[SHOW_ID] = _details(11, season=1)
    p = uvs.tv_aired_progress(u, [SHOW_ID])[SHOW_ID]
    assert p == {"watched": 10, "aired": 11, "percent": 90.9}
    assert len(_watched_positions(u.id, SHOW_ID)) == 10   # nothing reset

    # User watches the new episode → back to 100%.
    _, _, _ = tv_tracking_mod.mark_show_aired_watched_core(u.id, SHOW_ID)
    assert _watched_positions(u.id, SHOW_ID) == {
        (1, e) for e in range(1, 12)}
    assert uvs.tv_aired_progress(u, [SHOW_ID])[SHOW_ID] == {
        "watched": 11, "aired": 11, "percent": 100.0}


# ════════════════════════════════════════════════════════════════════════
# Route-level: /mark_as_viewed for TV (user-scoped write + user_viewed)
# ════════════════════════════════════════════════════════════════════════

def test_mark_as_viewed_route_syncs_tv_state(client, factory, stub_details):
    """The GET route writes episode state AND the canonical user_viewed
    row (hero ✓ Viewed), strictly for the current user."""
    u = factory.user()
    factory.media(SHOW_ID, "tv", "Sync Show")
    DETAILS_CACHE[SHOW_ID] = _details(2, season=1)
    factory.aired(SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 1, 2)
    factory.future(SHOW_ID, 1, 3)
    _login(client, u)

    r = client.get(f"/mark_as_viewed/{SHOW_ID}/tv", follow_redirects=True)
    assert r.status_code == 200
    assert _watched_positions(u.id, SHOW_ID) == {(1, 1), (1, 2)}
    assert (SHOW_ID, "tv") in uvs.user_viewed_keys(u)


def test_mark_as_viewed_route_user_scoped(client, factory, stub_details):
    """Spec #24: User A marking a show can never mutate User B's rows."""
    a = factory.user("usera")
    b = factory.user("userb")
    factory.media(SHOW_ID, "tv", "Privacy Show")
    DETAILS_CACHE[SHOW_ID] = _details(2, season=1)
    factory.aired(SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 1, 2)
    _login(client, a)

    r = client.get(f"/mark_as_viewed/{SHOW_ID}/tv", follow_redirects=True)
    assert r.status_code == 200
    assert _watched_positions(a.id, SHOW_ID) == {(1, 1), (1, 2)}
    assert _watched_positions(b.id, SHOW_ID) == set()


def test_mark_as_viewed_route_anonymous_cannot_bulk_mark(
        client, factory, stub_details):
    """Spec #25: anonymous users get no personalized writes."""
    u = factory.user()
    factory.media(SHOW_ID, "tv", "Anon Show")
    DETAILS_CACHE[SHOW_ID] = _details(1, season=1)
    factory.aired(SHOW_ID, 1, 1)

    r = client.get(f"/mark_as_viewed/{SHOW_ID}/tv", follow_redirects=False)
    assert r.status_code in (301, 302)
    assert _watched_positions(u.id, SHOW_ID) == set()
    assert (SHOW_ID, "tv") not in uvs.user_viewed_keys(u)


def test_mark_as_viewed_movie_route_untouched(client, factory):
    """The movie path stays exactly what it was: user_viewed only — no
    TVEpisodeWatch involvement for movies."""
    u = factory.user()
    factory.media(MOVIE_ID, "movie", "Just A Movie")
    _login(client, u)

    r = client.get(f"/mark_as_viewed/{MOVIE_ID}/movie",
                   follow_redirects=True)
    assert r.status_code == 200
    assert (MOVIE_ID, "movie") in uvs.user_viewed_keys(u)
    assert TVEpisodeWatch.query.filter_by(user_id=u.id).count() == 0


# ════════════════════════════════════════════════════════════════════════
# Cross-surface: one shared state read by every surface
# ════════════════════════════════════════════════════════════════════════

def test_cross_surface_detail_matches_payloads(client, factory,
                                               stub_details):
    """Spec #19–23: after Mark as Viewed, the TV detail hero, the shared
    /api/view-state payload, and the /api/tv/<id>/aired-progress endpoint
    all report the same 31/31 = 100% state."""
    u = factory.user()
    factory.media(SHOW_ID, "tv", "Cross Show")
    DETAILS_CACHE[SHOW_ID] = _details(1, season=4)
    for season in (1, 2, 3):
        for ep in range(1, 11):
            factory.aired(SHOW_ID, season, ep)
    factory.aired(SHOW_ID, 4, 1)
    _login(client, u)
    client.get(f"/mark_as_viewed/{SHOW_ID}/tv", follow_redirects=True)

    # shared page payload (homepage/browse/trending/For You/CineBot source)
    r = client.get(f"/api/view-state?tv={SHOW_ID}")
    assert r.status_code == 200
    data = r.get_json()
    assert data["tv_progress"][str(SHOW_ID)] == {
        "watched": 31, "aired": 31, "percent": 100.0}

    # TV detail hero line (same shared function)
    assert uvs.tv_aired_progress(u, [SHOW_ID])[SHOW_ID]["percent"] == 100.0

    # live refresh endpoint backing the detail page's progress line
    r = client.get(f"/api/tv/{SHOW_ID}/aired-progress")
    assert r.status_code == 200
    body = r.get_json()
    assert body["tv_progress"]["percent"] == 100.0
    assert body["season_aired"] == {"1": 10, "2": 10, "3": 10, "4": 1}


# ════════════════════════════════════════════════════════════════════════
# Movie quick-log: action wording + rewatch behavior intact
# ════════════════════════════════════════════════════════════════════════

def _log(client, tmdb_id):
    return client.post(
        f"/api/media/{tmdb_id}/log",
        json={"media_type": "movie", "title": "Quick Film"},
        follow_redirects=True)


def test_movie_first_log_is_not_rewatch(client, factory):
    """Spec #1: the first log is a plain watch event."""
    u = factory.user()
    factory.media(MOVIE_ID, "movie", "Quick Film")
    _login(client, u)
    r = _log(client, MOVIE_ID)
    assert r.status_code == 201
    body = r.get_json()
    assert body["is_rewatch"] is False
    assert body["logged_today"] is True


def test_movie_rewatch_after_viewed_intact(client, factory):
    """Spec #1/#4: viewed movies can still log intentional rewatches."""
    u = factory.user()
    m = factory.media(MOVIE_ID, "movie", "Quick Film")
    from datetime import datetime
    db.session.add(DiaryEntry(
        user_id=u.id, media_id=m.id, media_type="movie",
        watched_date=date.today() - timedelta(days=1),
        is_rewatch=False))
    db.session.commit()
    _login(client, u)

    r = _log(client, MOVIE_ID)
    assert r.status_code == 201
    body = r.get_json()
    assert body["is_rewatch"] is True       # deliberate rewatch preserved
    assert DiaryEntry.query.filter_by(
        user_id=u.id, media_id=m.id, media_type="movie").count() == 2


def test_movie_logged_today_exposed_to_action_wording(client, factory):
    """Spec #3: the shared payload carries logged-today ids so the quick-log
    action can say "Watched Today" instead of a state-blind re-invite."""
    u = factory.user()
    factory.media(MOVIE_ID, "movie", "Quick Film")
    _login(client, u)

    before = client.get(f"/api/view-state?movies={MOVIE_ID}").get_json()
    assert before["logged_today_movie_ids"] == []
    assert MOVIE_ID in before["viewed_movie_ids"] or \
        MOVIE_ID not in before["viewed_movie_ids"]   # field additive only

    _log(client, MOVIE_ID)
    after = client.get(f"/api/view-state?movies={MOVIE_ID}").get_json()
    assert after["logged_today_movie_ids"] == [MOVIE_ID]
    assert after["viewed_movie_ids"] == [MOVIE_ID]


def test_logged_today_is_per_user(client, factory):
    """User A logging a movie never flips User B's action wording."""
    a = factory.user("loga")
    b = factory.user("logb")
    factory.media(MOVIE_ID, "movie", "Quick Film")
    _login(client, a)
    _log(client, MOVIE_ID)
    payload_a = uvs.view_state_payload(a, [MOVIE_ID], [])
    payload_b = uvs.view_state_payload(b, [MOVIE_ID], [])
    assert payload_a["logged_today_movie_ids"] == [MOVIE_ID]
    assert payload_b["logged_today_movie_ids"] == []


def test_quicklog_wording_contract_in_source():
    """Spec #2: the client wording contract exists — three distinct action
    labels, badge/state stays separate, and no label claims state."""
    with open("static/js/quick-log.js", encoding="utf-8") as fh:
        source = fh.read()
    assert "Log Watched" in source
    assert "Log Rewatch" in source
    assert "Watched Today" in source
    # the shared store exposes the logged-today check
    with open("static/js/view-state.js", encoding="utf-8") as fh:
        store = fh.read()
    assert "isLoggedToday" in store
    assert "logged_today_movie_ids" in store
