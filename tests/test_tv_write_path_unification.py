"""Task F1 — ONE canonical TV aired definition for every write/progress path.

Every TV operation that decides which episodes are eligible, inserted, or
counted must agree with ``api.user_view_state.aired_positions_for_show()``:

  * mark_episode_watched_core  → counters + gating via canonical sync
  * mark_season_watched        → mark_season_aired_watched (season-narrowed)
  * mark_all_watched           → mark_aired_positions_watched (bulk core)
  * mark_as_viewed (TV)        → mark_show_aired_watched_core (thin delegate)
  * update_season_progress     → season_aired_for_show (canonical projection)
  * to_dict / endpoints        → canonical payload, never a TMDb denominator

Semantics pinned here (spec §30 cases 1–21): historical seasons in, future
out, specials out, duplicates collapsed, rewatches never inflate progress,
existing rows never deleted or mutated, idempotent bulk ops, bounded queries,
single TMDb details resolution per operation, and cross-surface agreement.

TMDb is stubbed at the shared details cache (same pattern as
test_tv_mark_as_viewed_sync.py) — no external services are required.
"""
from uuid import uuid4

import pytest
from sqlalchemy import event

from models import (db, DiaryEntry, MediaItem, TVEpisodeWatch,
                    TVShowProgress, UpcomingEpisode)
import api.user_view_state as uvs
import api.continue_watching as cw
import routes.tv_tracking as tv_tracking_mod

# TMDb ids in a band no other TV suite uses (shared session DB).
BANSHEE_SHOW = 992001       # completed 10/10/10/8
SPECIALS_SHOW = 992002      # season 0 + one normal season
RUNNING_SHOW = 992003       # partial current season
FUTURE_SHOW = 992004        # aired + future boundary

DETAILS_CACHE = {}

BANSHEE = {1: 10, 2: 10, 3: 10, 4: 8}                      # 38 aired
BANSHEE_POSITIONS = {
    (sn, ep) for sn, total in BANSHEE.items() for ep in range(1, total + 1)}


def _details(last_episode, season=1, seasons=None, status=""):
    """TMDb-shaped details stub (append_to_response=seasons metadata)."""
    details = {"id": 0,
               "last_episode_to_air": {"season_number": season,
                                       "episode_number": last_episode}}
    if seasons is not None:
        details["seasons"] = [
            {"season_number": sn, "episode_count": ec,
             "air_date": "2010-01-01"}
            for sn, ec in sorted(seasons.items())]
        details["number_of_seasons"] = len([sn for sn in seasons if sn > 0])
        details["number_of_episodes"] = sum(
            ec for sn, ec in seasons.items() if sn > 0)
    if status:
        details["status"] = status
    return details


@pytest.fixture
def stub_details(monkeypatch):
    """Stub every TMDb touchpoint: the shared details cache used by
    uvs._default_details_loader AND fetch_tv_show_details."""
    monkeypatch.setattr(
        cw, "show_details", lambda sid, **kw: DETAILS_CACHE.get(sid, None))
    monkeypatch.setattr(
        tv_tracking_mod, "fetch_tv_show_details",
        lambda sid, **kw: DETAILS_CACHE.get(sid) or {
            "id": sid, "number_of_seasons": 0,
            "number_of_episodes": 0, "status": ""})
    cw._memo.clear()
    yield
    DETAILS_CACHE.clear()
    cw._memo.clear()


@pytest.fixture
def factory(db):
    """Per-test builders with surgical teardown (shared test DB safe)."""
    users, media_ids, show_ids = [], [], []
    suffix = uuid4().hex[:8]

    def user(username="f1"):
        from models import User
        u = User(username=f"{username}-{suffix}-{len(users)}",
                 email=f"{username}-{suffix}-{len(users)}@example.com",
                 email_verified=True)
        u.set_password("F1Unify1")
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

    def watch(u, show_id, season, episode, rewatch=False, **kw):
        row = TVEpisodeWatch(user_id=u.id, show_id=show_id,
                             season_number=season, episode_number=episode,
                             is_rewatch=rewatch, **kw)
        db.session.add(row)
        db.session.commit()
        return row

    def upcoming(show_id, season, episode, delta):
        show_ids.append(show_id)
        from datetime import date, timedelta
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

    db.session.rollback()
    user_ids = [u.id for u in users]
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
        from models import User
        User.query.filter(User.id.in_(user_ids)).delete(
            synchronize_session=False)
    if media_ids:
        MediaItem.query.filter(
            MediaItem.id.in_(media_ids)).delete(synchronize_session=False)
    if show_ids:
        UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(set(show_ids))
        ).delete(synchronize_session=False)
    db.session.commit()
    db.session.expire_all()


def _watched_positions(user_id, show_id):
    """Canonical first-watch positions (rewatches excluded)."""
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


def _all_rows(user_id, show_id):
    return TVEpisodeWatch.query.filter_by(
        user_id=user_id, show_id=show_id).all()


def _login(client, u):
    client.post("/login", data={"username": u.username,
                                "password": "F1Unify1"},
                follow_redirects=True)


def _banshee_fixture():
    DETAILS_CACHE[BANSHEE_SHOW] = _details(
        8, season=4, seasons=BANSHEE, status="Ended")


# ════════════════════════════════════════════════════════════════════════
# Cases 1–4: Banshee fixture — canonical totals and season map
# ════════════════════════════════════════════════════════════════════════

def test_case1_4_banshee_canonical_totals(factory, stub_details):
    """Cases 1–4: completed four-season historical fixture; canonical aired
    total = 38; season map = {1:10, 2:10, 3:10, 4:8}."""
    _banshee_fixture()
    aired = uvs.aired_positions_for_show(BANSHEE_SHOW)
    assert len(aired) == 38                        # case 3
    assert aired == BANSHEE_POSITIONS
    assert uvs.season_aired_for_show(BANSHEE_SHOW) == BANSHEE   # case 4


# ════════════════════════════════════════════════════════════════════════
# Cases 5–7: every bulk path uses the canonical aired set only
# ════════════════════════════════════════════════════════════════════════

def test_case5_mark_season_uses_aired_only(factory, stub_details):
    """Case 5: mark season inserts exactly the aired positions of that
    season — future episodes of the same season stay out."""
    u = factory.user()
    DETAILS_CACHE[BANSHEE_SHOW] = _details(3, season=4, seasons=BANSHEE)
    # S4: E1–E3 aired; E4..E8 future. S1–S3 historical, fully aired.
    _, inserted, aired = uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 4)
    assert (inserted, aired) == (3, 3)
    assert _watched_positions(u.id, BANSHEE_SHOW) == {
        (4, 1), (4, 2), (4, 3)}
    assert all(row.season_number == 4 for row in _all_rows(u.id, BANSHEE_SHOW))


def test_case6_mark_all_uses_aired_only(factory, stub_details):
    """Case 6: mark-all inserts exactly the canonical aired set —
    historical seasons in full, nothing future, nothing special."""
    u = factory.user()
    _banshee_fixture()
    _, inserted, aired = uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    assert (inserted, aired) == (38, 38)
    assert _watched_positions(u.id, BANSHEE_SHOW) == BANSHEE_POSITIONS


def test_case7_mark_as_viewed_uses_same_core(factory, stub_details,
                                             monkeypatch):
    """Case 7: mark-as-viewed, mark-season and mark-all all funnel through
    the ONE canonical write core (uvs.mark_aired_positions_watched)."""
    u = factory.user()
    _banshee_fixture()

    calls = {"n": 0}
    real_uvs = uvs.mark_aired_positions_watched
    real_tt = tv_tracking_mod.mark_aired_positions_watched

    def spy_uvs(*a, **kw):
        calls["n"] += 1
        return real_uvs(*a, **kw)

    def spy_tt(*a, **kw):
        calls["n"] += 1
        return real_tt(*a, **kw)

    monkeypatch.setattr(uvs, "mark_aired_positions_watched", spy_uvs)
    monkeypatch.setattr(tv_tracking_mod,
                        "mark_aired_positions_watched", spy_tt)

    tv_tracking_mod.mark_show_aired_watched_core(u.id, BANSHEE_SHOW)
    uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 1)   # idempotent 2nd call
    tv_tracking_mod.update_season_progress(
        uvs.get_or_create_tv_progress(u.id, BANSHEE_SHOW), BANSHEE_SHOW)

    assert calls["n"] >= 2          # both entries hit the shared core


# ════════════════════════════════════════════════════════════════════════
# Cases 8–9: model percentage + season progress agree with canonical math
# ════════════════════════════════════════════════════════════════════════

def test_case8_calculate_progress_percentage_agrees(factory, stub_details):
    """Case 8: TVShowProgress.calculate_progress_percentage() equals the
    canonical percent at partial AND complete states."""
    u = factory.user()
    _banshee_fixture()
    for ep in range(1, 11):
        factory.watch(u, BANSHEE_SHOW, 1, ep)
    for ep in range(1, 11):
        factory.watch(u, BANSHEE_SHOW, 2, ep)
    for ep in range(1, 11):
        factory.watch(u, BANSHEE_SHOW, 3, ep)
    # 30/38 watched → both must say 78.9
    progress = uvs.get_or_create_tv_progress(u.id, BANSHEE_SHOW)
    uvs.sync_tv_progress_counters(progress, u.id, BANSHEE_SHOW)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical["watched"] == 30 and canonical["aired"] == 38
    assert progress.calculate_progress_percentage() == canonical["percent"]
    assert canonical["percent"] == 78.9

    uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical == {"watched": 38, "aired": 38, "percent": 100.0}
    assert progress.calculate_progress_percentage() == 100.0


def test_case9_update_season_progress_agrees(factory, stub_details):
    """Case 9: update_season_progress counts complete seasons against the
    canonical AIRED season denominators (never TMDb episode_count)."""
    u = factory.user()
    _banshee_fixture()
    progress = uvs.get_or_create_tv_progress(u.id, BANSHEE_SHOW)

    # S1–S3 complete, S4 partial (7/8) → 3 completed seasons.
    for sn in (1, 2, 3):
        for ep in range(1, 11):
            factory.watch(u, BANSHEE_SHOW, sn, ep)
    for ep in range(1, 8):
        factory.watch(u, BANSHEE_SHOW, 4, ep)
    uvs.sync_tv_progress_counters(progress, u.id, BANSHEE_SHOW)
    tv_tracking_mod.update_season_progress(progress, BANSHEE_SHOW)
    assert progress.watched_seasons == 3

    # Watch S4E8 → all four seasons complete.
    factory.watch(u, BANSHEE_SHOW, 4, 8)
    tv_tracking_mod.update_season_progress(progress, BANSHEE_SHOW)
    assert progress.watched_seasons == 4


# ════════════════════════════════════════════════════════════════════════
# Cases 10–11: future + specials safety on every bulk path
# ════════════════════════════════════════════════════════════════════════

def test_case10_future_excluded_from_every_bulk_path(factory, stub_details):
    """Case 10: 10 aired + 2 future → season mark inserts 10, mark-all
    inserts 10 (per season scope), never 12."""
    u = factory.user()
    # Anchor says S1E10 is the LAST AIRED episode; E11–E12 exist only as
    # future calendar rows (air_date > today).
    DETAILS_CACHE[FUTURE_SHOW] = _details(10, season=1, seasons={1: 12})
    for ep in range(1, 11):
        factory.aired(FUTURE_SHOW, 1, ep)
    factory.future(FUTURE_SHOW, 1, 11)
    factory.future(FUTURE_SHOW, 1, 12)

    _, season_inserted, season_aired = uvs.mark_season_aired_watched(
        u, FUTURE_SHOW, 1)
    assert (season_inserted, season_aired) == (10, 10)
    assert not any(ep > 10 for _s, ep in _watched_positions(
        u.id, FUTURE_SHOW))

    _, all_inserted, all_aired = uvs.mark_aired_positions_watched(
        u, FUTURE_SHOW)
    assert (all_inserted, all_aired) == (0, 10)    # nothing new to insert
    assert _watched_positions(u.id, FUTURE_SHOW) == {
        (1, ep) for ep in range(1, 11)}


def test_case11_specials_never_enter_progress(factory, stub_details):
    """Case 11: season 0 specials are excluded from inserts AND from the
    progress denominator on every path."""
    u = factory.user()
    DETAILS_CACHE[SPECIALS_SHOW] = _details(
        10, season=1, seasons={0: 3, 1: 10})
    factory.aired(SPECIALS_SHOW, 0, 1)             # even an aired special

    _, inserted, aired = uvs.mark_aired_positions_watched(
        u, SPECIALS_SHOW)
    assert (inserted, aired) == (10, 10)
    assert not any(season == 0 for season, _ep in _watched_positions(
        u.id, SPECIALS_SHOW))
    progress = uvs.get_or_create_tv_progress(u.id, SPECIALS_SHOW)
    assert progress.total_episodes == 10
    assert uvs.season_aired_for_show(SPECIALS_SHOW) == {1: 10}
    assert progress.calculate_progress_percentage() == 100.0


# ════════════════════════════════════════════════════════════════════════
# Cases 12–16: idempotency, preservation, rewatch, duplicates
# ════════════════════════════════════════════════════════════════════════

def test_case12_mark_season_idempotent(factory, stub_details):
    """Case 12: repeated Mark Season Watched inserts nothing the 2nd time
    and never duplicates rows."""
    u = factory.user()
    DETAILS_CACHE[BANSHEE_SHOW] = _details(8, season=4, seasons=BANSHEE)
    _, first_inserted, _ = uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 4)
    assert first_inserted == 8
    _, second_inserted, second_aired = uvs.mark_season_aired_watched(
        u, BANSHEE_SHOW, 4)
    assert second_inserted == 0
    assert second_aired == 8
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 8


def test_case13_mark_all_idempotent(factory, stub_details):
    """Case 13: repeated Mark All Watched inserts nothing and mutates
    nothing."""
    u = factory.user()
    _banshee_fixture()
    _, first_inserted, _ = uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    assert first_inserted == 38
    before = [(row.id, row.rating, row.notes, row.watched_date)
              for row in _all_rows(u.id, BANSHEE_SHOW)]
    _, second_inserted, second_aired = uvs.mark_aired_positions_watched(
        u, BANSHEE_SHOW)
    assert (second_inserted, second_aired) == (0, 38)
    after = [(row.id, row.rating, row.notes, row.watched_date)
             for row in _all_rows(u.id, BANSHEE_SHOW)]
    assert before == after
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 38


def test_case14_existing_metadata_preserved(factory, stub_details):
    """Case 14: a pre-existing first-watch row (rating, notes, watched
    date) survives Mark Season Watched byte-for-byte; no DELETE."""
    u = factory.user()
    DETAILS_CACHE[BANSHEE_SHOW] = _details(8, season=4, seasons=BANSHEE)
    from datetime import date
    existing = factory.watch(u, BANSHEE_SHOW, 4, 1, rating=4.5,
                             notes="great", watched_date=date(2026, 1, 15))
    _, inserted, _ = uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 4)
    assert inserted == 7                            # only the missing ones

    kept = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW,
        season_number=4, episode_number=1).one()
    assert kept.id == existing.id
    assert kept.rating == 4.5
    assert kept.notes == "great"
    assert kept.watched_date == date(2026, 1, 15)
    assert kept.is_rewatch is False
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 8  # no duplicates


def test_case15_rewatch_rows_preserved_not_counted(factory, stub_details):
    """Case 15: rewatch rows are preserved untouched, never mistaken for
    the first watch, and never inflate progress."""
    u = factory.user()
    DETAILS_CACHE[BANSHEE_SHOW] = _details(8, season=4, seasons=BANSHEE)
    factory.watch(u, BANSHEE_SHOW, 4, 1, rewatch=True)
    _, inserted, _ = uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 4)

    # The rewatch-only position still received its canonical first watch…
    assert inserted == 8
    # …the rewatch row itself is untouched, and exactly one canonical row
    # exists for the position.
    rewatches = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW, is_rewatch=True).all()
    assert len(rewatches) == 1
    first_watch = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW, season_number=4,
        episode_number=1, is_rewatch=False).one()
    assert first_watch.id != rewatches[0].id
    # Progress counts the position once, not twice (only S4 was marked).
    progress = uvs.get_or_create_tv_progress(u.id, BANSHEE_SHOW)
    assert progress.watched_episodes == 8
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 9   # 8 canonical + 1 rewatch


def test_case16_no_duplicate_watch_rows(factory, stub_details):
    """Case 16: one row per (user, show, season, episode) identity across
    every bulk path — ever."""
    u = factory.user()
    _banshee_fixture()
    uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 1)
    uvs.mark_season_aired_watched(u, BANSHEE_SHOW, 4)
    uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)

    identities = db.session.query(
        TVEpisodeWatch.user_id, TVEpisodeWatch.show_id,
        TVEpisodeWatch.season_number, TVEpisodeWatch.episode_number,
    ).filter(
        TVEpisodeWatch.user_id == u.id,
        TVEpisodeWatch.show_id == BANSHEE_SHOW,
        TVEpisodeWatch.is_rewatch == False,  # noqa: E712
    ).all()
    assert len(identities) == len(set(identities)) == 38


# ════════════════════════════════════════════════════════════════════════
# Cases 17, 21 (+ TMDb budget): bounded queries, one details resolution
# ════════════════════════════════════════════════════════════════════════

def test_case17_no_nplus1_and_single_bulk_insert(factory, stub_details):
    """Case 17: mark-all on the 38-episode Banshee fixture issues a bounded
    number of episode statements — never one SELECT per episode — and
    exactly ONE bulk INSERT."""
    u = factory.user()
    _banshee_fixture()

    episode_statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if "tv_episode_watch" in statement:
            episode_statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _before)
    try:
        _, inserted, _ = uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    finally:
        event.remove(db.engine, "before_cursor_execute", _before)

    assert inserted == 38
    inserts = [s for s in episode_statements if s.startswith("INSERT")]
    assert len(inserts) == 1
    selects = [s for s in episode_statements if s.startswith("SELECT")]
    assert len(selects) <= 3        # existing-watch + counter recompute
    assert len(selects) < 38


def test_case21_tmdb_details_resolved_once(factory, stub_details,
                                           monkeypatch):
    """Case 21 (TMDb budget): one bulk operation resolves the cached show
    details payload exactly once — aired set, counters and gating share
    the same resolution."""
    u = factory.user()
    _banshee_fixture()

    calls = {"n": 0}
    real = cw.show_details

    def counting(sid, **kw):
        calls["n"] += 1
        return real(sid, **kw)

    monkeypatch.setattr(cw, "show_details", counting)
    uvs.mark_aired_positions_watched(u, BANSHEE_SHOW)
    assert calls["n"] == 1


# ════════════════════════════════════════════════════════════════════════
# Cases 18–20: API denominator consistency + running-show lifecycle
# ════════════════════════════════════════════════════════════════════════

def _mark_all(client, u, show_id=BANSHEE_SHOW):
    _login(client, u)
    r = client.post(f"/api/tv/{show_id}/mark-all-watched", json={})
    assert r.status_code == 200
    return r.get_json()


def test_case18_api_response_denominator_consistent(client, factory,
                                                    stub_details):
    """Case 18: the mark-all response, /progress endpoint and stored
    counters all publish the SAME canonical denominator (38 aired), never
    a TMDb count."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee_fixture()

    body = _mark_all(client, u)
    assert body["success"] is True
    assert body["marked_episodes"] == 38
    assert body["aired_episodes"] == 38
    progress = body["progress"]
    assert progress["total_episodes"] == 38
    assert progress["watched_episodes"] == 38
    assert progress["aired_episodes"] == 38
    assert progress["progress_percentage"] == 100.0

    r = client.get(f"/api/tv/{BANSHEE_SHOW}/progress")
    assert r.status_code == 200
    p = r.get_json()["progress"]
    assert p["total_episodes"] == 38
    assert p["watched_episodes"] == 38
    assert p["progress_percentage"] == 100.0


def test_case19_running_show_new_air_drops_percent(client, factory,
                                                   stub_details):
    """Case 19: a fully watched running show drops to 25/26 (96.2%) the
    moment a new episode airs — no stored percentage, no reset."""
    u = factory.user()
    running = {1: 10, 2: 10, 3: 5}
    DETAILS_CACHE[RUNNING_SHOW] = _details(
        5, season=3, seasons=running, status="Returning Series")
    _mark_all(client, u, RUNNING_SHOW)

    DETAILS_CACHE[RUNNING_SHOW] = _details(
        6, season=3, seasons=running, status="Returning Series")
    r = client.get(f"/api/tv/{RUNNING_SHOW}/progress")
    p = r.get_json()["progress"]
    assert p["watched_episodes"] == 25
    assert p["total_episodes"] == 26
    assert p["progress_percentage"] == 96.2


def test_case20_watching_new_episode_restores_100(client, factory,
                                                  stub_details):
    """Case 20: after the new episode airs, watching it restores 26/26 =
    100% without changing any stored percentage semantics."""
    u = factory.user()
    running = {1: 10, 2: 10, 3: 5}
    DETAILS_CACHE[RUNNING_SHOW] = _details(
        5, season=3, seasons=running, status="Returning Series")
    _mark_all(client, u, RUNNING_SHOW)

    DETAILS_CACHE[RUNNING_SHOW] = _details(
        6, season=3, seasons=running, status="Returning Series")
    _, inserted, aired = uvs.mark_aired_positions_watched(u, RUNNING_SHOW)
    assert (inserted, aired) == (1, 26)

    r = client.get(f"/api/tv/{RUNNING_SHOW}/progress")
    p = r.get_json()["progress"]
    assert (p["watched_episodes"], p["total_episodes"],
            p["progress_percentage"]) == (26, 26, 100.0)


def test_case21b_cross_surface_progress_agreement(client, factory,
                                                  stub_details):
    """Case 21: after Mark as Viewed, the shared view-state payload, the
    aired-progress endpoint, the /progress endpoint and the per-season
    map all report the same 38/38 = 100% Banshee state."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Cross")
    _banshee_fixture()
    _login(client, u)

    r = client.get(f"/mark_as_viewed/{BANSHEE_SHOW}/tv",
                   follow_redirects=True)
    assert r.status_code == 200
    assert _watched_positions(u.id, BANSHEE_SHOW) == BANSHEE_POSITIONS

    # shared page payload (homepage/browse/trending/For You/CineBot source)
    payload = client.get(f"/api/view-state?tv={BANSHEE_SHOW}").get_json()
    assert payload["tv_progress"][str(BANSHEE_SHOW)] == {
        "watched": 38, "aired": 38, "percent": 100.0}

    # TV detail hero line endpoint
    body = client.get(f"/api/tv/{BANSHEE_SHOW}/aired-progress").get_json()
    assert body["tv_progress"]["percent"] == 100.0
    assert body["season_aired"] == {"1": 10, "2": 10, "3": 10, "4": 8}

    # tracking progress endpoint (canonical payload)
    p = client.get(f"/api/tv/{BANSHEE_SHOW}/progress").get_json()["progress"]
    assert (p["watched_episodes"], p["total_episodes"],
            p["progress_percentage"]) == (38, 38, 100.0)

    # model-level percentage agrees with every surface above
    progress = TVShowProgress.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW).one()
    assert progress.calculate_progress_percentage() == 100.0
    assert progress.watched_seasons == 4
