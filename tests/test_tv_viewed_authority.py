"""Task F2 — TV Viewed is a DERIVED verdict of canonical aired progress.

One semantic authority for the whole application:

    TV Viewed  ⇔  watched == aired > 0        (canonical percent == 100)

``user_viewed`` is a compatibility mirror written by ``/mark_as_viewed``;
it can never create a TV Viewed badge the canonical episode ledger does
not support, and unmarking is symmetric: GET ``/remove_from_viewed/<id>/tv``
clears every TVEpisodeWatch row (rewatches included — spec §43 option A,
the same explicit-removal contract as unmark-season/unmark-episode) plus
the ``user_viewed`` row, so Viewed-OFF can never coexist with 100%.

Spec coverage (§33 cases 1–36 + §34 invariant):
  * authority            — percent==100 ⇔ Viewed, both directions
  * unmark symmetry      — ledger emptied, mirror row gone, idempotent
  * legacy counters      — stale stored 8/8 rows never leak to any surface
  * cross-surface        — detail page, profile, unfinished shelf, lists,
                           viewed page, my-shows, next-episode, view-state
  * preservation         — mark-as-viewed keeps ratings/notes/dates;
                           unmark deletes (documented §43-A semantics)
  * privacy              — user-scoped reads and deletes
  * budgets              — bounded episode SQL, single TMDb resolution

TMDb is stubbed at the shared details cache AND every ``fetch_tv_show_details``
binding (same pattern as test_tv_write_path_unification.py) — no external
services are required. Users/ids live in the 993000+ band no other suite
uses (shared session DB).
"""
import re
from uuid import uuid4

import pytest
from sqlalchemy import event

from models import (db, DiaryEntry, MediaItem, TVShowProgress,
                    UpcomingEpisode, User, user_viewed)
from models.tv import TVEpisodeWatch
import api.user_view_state as uvs
import api.continue_watching as cw
import routes.details as details_mod
import routes.tv_tracking as tv_tracking_mod

# TMDb ids in a band no other TV suite uses (shared session DB).
BANSHEE_SHOW = 993001        # completed 10/10/10/8 = 38 aired
LEGACY_SHOW = 993002         # stale stored-counter fixture
RUNNING_SHOW = 993003        # partial current season
SPECIALS_SHOW = 993004       # specials + future boundary fixture

DETAILS_CACHE = {}

BANSHEE = {1: 10, 2: 10, 3: 10, 4: 8}                      # 38 aired
BANSHEE_POSITIONS = {
    (sn, ep) for sn, total in BANSHEE.items() for ep in range(1, total + 1)}
S1 = {(1, ep) for ep in range(1, 11)}

# The hero badge span: text sits on its own line between the icon and
# </span>, so match it whitespace-tolerantly (the two action links end
# in </a>, never </span>, and cannot false-positive).
_VIEWED_BADGE = re.compile(r">\s*Viewed\s*</span>")


def _has_badge(html):
    return bool(_VIEWED_BADGE.search(html))


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


def _page_payload(show_id, **extra):
    """Everything tv_detail.html touches, minus what DETAILS_CACHE adds."""
    payload = {
        "id": show_id,
        "name": f"Fixture {show_id}",
        "genres": ["Drama"],
        "cast": [], "reviews": [], "origin_country": [], "created_by": [],
        "poster_path": None, "backdrop_path": None, "overview": "o",
        "first_air_date": "2010-01-01", "last_air_date": "2010-02-01",
        "number_of_seasons": 1, "number_of_episodes": 10, "status": "Ended",
        "vote_average": 7.5, "vote_count": 10, "tagline": "t",
        "original_language": "en", "certification": None,
        "trailer_key": None,
    }
    payload.update(extra)
    return payload


def _seed_show(show_id, seasons=BANSHEE, status="Ended"):
    """Register the shared TMDb details + page payload for a fixture show
    (one cache entry serves the loader, tv_tracking and the detail page)."""
    payload = _page_payload(
        show_id,
        seasons=[{"season_number": sn, "episode_count": ec,
                  "name": f"Season {sn}", "air_date": "2010-01-01",
                  "poster_path": None}
                 for sn, ec in sorted(seasons.items())],
        number_of_seasons=len([sn for sn in seasons if sn > 0]),
        number_of_episodes=sum(ec for sn, ec in seasons.items() if sn > 0),
        status=status)
    DETAILS_CACHE[show_id] = _details(
        seasons[max(seasons)], season=max(seasons),
        seasons=seasons, status=status)
    DETAILS_CACHE[show_id].update(payload)
    return DETAILS_CACHE[show_id]


def _banshee(page_extra=None):
    entry = _seed_show(BANSHEE_SHOW)
    if page_extra:
        entry.update(page_extra)
    return entry


@pytest.fixture
def stub_details(monkeypatch):
    """Stub every TMDb touchpoint: the shared details cache used by
    uvs._default_details_loader AND every fetch_tv_show_details binding
    (routes.tv_tracking, routes.details — the page itself)."""
    monkeypatch.setattr(
        cw, "show_details", lambda sid, **kw: DETAILS_CACHE.get(sid, None))
    monkeypatch.setattr(
        tv_tracking_mod, "fetch_tv_show_details",
        lambda sid, **kw: DETAILS_CACHE.get(sid) or {
            "id": sid, "number_of_seasons": 0,
            "number_of_episodes": 0, "status": ""})
    monkeypatch.setattr(
        details_mod, "fetch_tv_show_details",
        lambda sid, **kw: DETAILS_CACHE.get(sid) or _page_payload(sid))
    cw._memo.clear()
    yield
    DETAILS_CACHE.clear()
    cw._memo.clear()


@pytest.fixture
def factory(db):
    """Per-test builders with surgical teardown (shared test DB safe)."""
    users, media_ids, show_ids = [], [], []
    suffix = uuid4().hex[:8]

    def user(username="f2"):
        u = User(username=f"{username}-{suffix}-{len(users)}",
                 email=f"{username}-{suffix}-{len(users)}@example.com",
                 email_verified=True)
        u.set_password("F2Viewed2")
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

    def progress(u, show_id, status="watching", total=0, watched=0,
                 watched_seasons=0, total_seasons=0):
        row = TVShowProgress(user_id=u.id, show_id=show_id, status=status,
                             total_episodes=total, watched_episodes=watched,
                             watched_seasons=watched_seasons,
                             total_seasons=total_seasons)
        db.session.add(row)
        db.session.commit()
        return row

    def viewed_mirror(u, show_id):
        """user_viewed row via the internal MediaItem id (like the route)."""
        m = MediaItem.query.filter_by(
            tmdb_id=show_id, media_type="tv").first()
        if m is None:
            m = media(show_id, "tv")
        db.session.execute(user_viewed.insert().values(
            user_id=u.id, media_id=m.id, media_type="tv"))
        db.session.commit()

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
    _F.progress, _F.viewed_mirror = progress, viewed_mirror
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
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.user_id.in_(user_ids)))
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


def _all_rows(user_id, show_id):
    return TVEpisodeWatch.query.filter_by(
        user_id=user_id, show_id=show_id).all()


def _mirror_exists(user_id, show_id):
    m = MediaItem.query.filter_by(
        tmdb_id=show_id, media_type="tv").first()
    if m is None:
        return False
    return db.session.execute(
        user_viewed.select().where(
            user_viewed.c.user_id == user_id,
            user_viewed.c.media_id == m.id,
            user_viewed.c.media_type == "tv")
    ).fetchone() is not None


def _login(client, u):
    client.post("/login", data={"username": u.username,
                                "password": "F2Viewed2"},
                follow_redirects=True)


# ════════════════════════════════════════════════════════════════════════
# Cases 1–6: the authority rule — percent==100 ⇔ Viewed
# ════════════════════════════════════════════════════════════════════════

def test_case1_full_aired_watch_is_viewed(factory, stub_details):
    """Case 1: watching every aired episode yields canonical 100% and a
    True verdict."""
    u = factory.user()
    _banshee()
    for (sn, ep) in BANSHEE_POSITIONS:
        factory.watch(u, BANSHEE_SHOW, sn, ep)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical == {"watched": 38, "aired": 38, "percent": 100.0}
    assert uvs.tv_viewed_from_progress(canonical) is True


def test_case2_partial_watch_is_not_viewed(factory, stub_details):
    """Case 2: 37/38 aired watched → 97.4%, not Viewed."""
    u = factory.user()
    _banshee()
    for (sn, ep) in BANSHEE_POSITIONS - {(4, 8)}:
        factory.watch(u, BANSHEE_SHOW, sn, ep)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical["percent"] == 97.4
    assert uvs.tv_viewed_from_progress(canonical) is False


def test_case3_no_progress_is_not_viewed(factory, stub_details):
    """Case 3: a fresh user (no watch rows) → None → not Viewed."""
    u = factory.user()
    _banshee()
    assert uvs.canonical_tv_progress(u, BANSHEE_SHOW) is None
    assert uvs.tv_viewed_from_progress(None) is False


def test_case4_season_only_watch_never_viewed(factory, stub_details):
    """Case 4: one complete season (10/38) is far from Viewed."""
    u = factory.user()
    _banshee()
    for ep in range(1, 11):
        factory.watch(u, BANSHEE_SHOW, 1, ep)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert (canonical["watched"], canonical["aired"],
            canonical["percent"]) == (10, 38, 26.3)
    assert uvs.tv_viewed_from_progress(canonical) is False


def test_case5_zero_denominator_never_viewed(factory, stub_details):
    """Case 5: watch rows with nothing verifiably aired → no canonical
    progress (None) → not Viewed; the helper also rejects a literal
    zero-denominator payload defensively."""
    u = factory.user()
    _banshee()
    factory.watch(u, SPECIALS_SHOW, 1, 1)
    # Nothing aired for this show (no details payload, no calendar rows).
    assert uvs.canonical_tv_progress(u, SPECIALS_SHOW) is None
    assert uvs.tv_viewed_from_progress(
        {"watched": 1, "aired": 0, "percent": 0.0}) is False


def test_case6_mirror_row_alone_cannot_create_viewed(factory, stub_details):
    """Case 6: a user_viewed mirror row with NO watch rows (or partial
    watches) never produces a Viewed verdict — the mirror is not truth."""
    u = factory.user()
    _banshee()
    factory.viewed_mirror(u, BANSHEE_SHOW)
    assert _mirror_exists(u.id, BANSHEE_SHOW)
    assert uvs.canonical_tv_progress(u, BANSHEE_SHOW) is None
    assert uvs.tv_viewed_from_progress(
        uvs.canonical_tv_progress(u, BANSHEE_SHOW)) is False
    # Partial watches: still not Viewed despite the mirror.
    for ep in range(1, 6):
        factory.watch(u, BANSHEE_SHOW, 1, ep)
    assert uvs.tv_viewed_from_progress(
        uvs.canonical_tv_progress(u, BANSHEE_SHOW)) is False


# ════════════════════════════════════════════════════════════════════════
# Cases 7–12: detail-page hero badge + Mark/Unmark action pair
# ════════════════════════════════════════════════════════════════════════

def test_case7_viewed_page_full_state(factory, stub_details, client):
    """Case 7: 38/38 → hero shows the Viewed badge, the Unmark action, and
    NO Mark-as-Viewed button."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    for (sn, ep) in BANSHEE_POSITIONS:
        factory.watch(u, BANSHEE_SHOW, sn, ep)
    _login(client, u)
    html = client.get(f"/tv/{BANSHEE_SHOW}").get_data(as_text=True)
    assert _has_badge(html)
    assert "Unmark Viewed" in html
    assert "Mark as Viewed" not in html
    assert 'data-aired="38"' in html and 'data-watched="38"' in html


def test_case8_viewed_page_partial_state(factory, stub_details, client):
    """Case 8: 5/38 → NO Viewed badge, NO Unmark action, Mark-as-Viewed
    offered."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    for ep in range(1, 6):
        factory.watch(u, BANSHEE_SHOW, 1, ep)
    _login(client, u)
    html = client.get(f"/tv/{BANSHEE_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    assert "Unmark Viewed" not in html
    assert "Mark as Viewed" in html
    assert 'data-watched="5"' in html and 'data-aired="38"' in html


def test_case9_viewed_page_anonymous(factory, stub_details, client):
    """Case 9: anonymous visitor → no badge, no mark button, no progress
    line."""
    _banshee()
    resp = client.get(f"/tv/{BANSHEE_SHOW}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Mark as Viewed" not in html
    assert "data-tv-progress" not in html
    assert not _has_badge(html)


def test_case10_not_started_neutral_page(factory, stub_details, client):
    """Case 10: the neutral, not-started page — a logged-in user with zero
    watch rows gets NO Viewed badge, NO Unmark action, and no personalized
    progress line; Mark-as-Viewed is the only offered action."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _login(client, u)
    resp = client.get(f"/tv/{BANSHEE_SHOW}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert not _has_badge(html)
    assert "Unmark Viewed" not in html
    assert "Mark as Viewed" in html
    assert "data-tv-progress" not in html


def test_case11_page_recomputes_per_request(factory, stub_details, client):
    """Case 11: after the new episode airs, a page reload drops the Viewed
    badge (25→26 denominator) without any manual reset."""
    u = factory.user()
    factory.media(RUNNING_SHOW, "tv", "Running Fixture")
    running = {1: 10, 2: 10, 3: 5}
    DETAILS_CACHE[RUNNING_SHOW] = _details(
        5, season=3, seasons=running, status="Returning Series")
    DETAILS_CACHE[RUNNING_SHOW].update(_page_payload(
        RUNNING_SHOW, seasons=[
            {"season_number": sn, "episode_count": hi,
             "name": f"Season {sn}", "air_date": "2010-01-01",
             "poster_path": None}
            for sn, hi in sorted(running.items())],
        number_of_seasons=3, number_of_episodes=25, status="Returning Series"))
    for (sn, ep) in {(sn, ep) for sn, hi in running.items()
                     for ep in range(1, hi + 1)}:
        factory.watch(u, RUNNING_SHOW, sn, ep)
    _login(client, u)
    assert _has_badge(client.get(
        f"/tv/{RUNNING_SHOW}").get_data(as_text=True))

    DETAILS_CACHE[RUNNING_SHOW] = _details(
        6, season=3, seasons=running, status="Returning Series")
    DETAILS_CACHE[RUNNING_SHOW].update(_page_payload(
        RUNNING_SHOW, seasons=[
            {"season_number": sn, "episode_count": hi,
             "name": f"Season {sn}", "air_date": "2010-01-01",
             "poster_path": None}
            for sn, hi in sorted(running.items())],
        number_of_seasons=3, number_of_episodes=26, status="Returning Series"))
    html = client.get(f"/tv/{RUNNING_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    assert "Mark as Viewed" in html
    assert 'data-watched="25"' in html and 'data-aired="26"' in html


def test_case12_badge_reads_canonical_not_mirror(factory, stub_details,
                                                 client):
    """Case 12: the badge ignores the user_viewed mirror — a mirror row on
    a partially watched show renders NO badge."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    for ep in range(1, 6):
        factory.watch(u, BANSHEE_SHOW, 1, ep)
    factory.viewed_mirror(u, BANSHEE_SHOW)
    _login(client, u)
    html = client.get(f"/tv/{BANSHEE_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    assert "Mark as Viewed" in html


# ════════════════════════════════════════════════════════════════════════
# Cases 13–20: symmetric unmark (route) — ledger, mirror, gating, budgets
# ════════════════════════════════════════════════════════════════════════

def _mark_and_mirror(client, u, show_id):
    _login(client, u)
    r = client.get(f"/mark_as_viewed/{show_id}/tv", follow_redirects=True)
    assert r.status_code == 200
    return r


def test_case13_unmark_clears_ledger_and_mirror(factory, stub_details,
                                                client):
    """Case 13: after Mark as Viewed, GET /remove_from_viewed/<id>/tv
    empties the ledger (all 38 rows) and removes the mirror row."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 38
    assert _mirror_exists(u.id, BANSHEE_SHOW)

    r = client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
                   follow_redirects=True)
    assert r.status_code == 200
    assert _all_rows(u.id, BANSHEE_SHOW) == []
    assert not _mirror_exists(u.id, BANSHEE_SHOW)
    assert uvs.canonical_tv_progress(u, BANSHEE_SHOW) is None


def test_case14_unmark_clears_rewatches(factory, stub_details, client):
    """Case 14 (§43-A): rewatch rows are cleared too — an unmark is an
    explicit removal of the show's watched state."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    factory.watch(u, BANSHEE_SHOW, 1, 1, rewatch=True)
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 39
    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    assert _all_rows(u.id, BANSHEE_SHOW) == []


def test_case15_unmark_zeroes_progress_and_unseals(factory, stub_details,
                                                   client):
    """Case 15: after unmark, progress reads zero and a sealed 'completed'
    status is un-sealed back to 'watching' (a tracked row survives)."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    progress = TVShowProgress.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW).one()
    assert progress.status == "completed"

    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    progress = TVShowProgress.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW).one()
    assert progress.watched_episodes == 0
    assert progress.total_episodes == 38      # aired reality kept
    assert progress.status == "watching"
    assert progress.completed_at is None
    assert progress.calculate_progress_percentage() == 0.0


def test_case16_unmark_idempotent(factory, stub_details, client):
    """Case 16: a second unmark is a harmless no-op (no error, no new
    rows)."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    for _ in range(2):
        r = client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
                       follow_redirects=True)
        assert r.status_code == 200
    assert _all_rows(u.id, BANSHEE_SHOW) == []
    assert not _mirror_exists(u.id, BANSHEE_SHOW)


def test_case17_unmark_then_remark_restores_viewed(factory, stub_details,
                                                   client):
    """Case 17: unmark → re-mark returns to 38/38 = Viewed with the same
    canonical ledger (idempotent core, no duplicates)."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    client.get(f"/mark_as_viewed/{BANSHEE_SHOW}/tv", follow_redirects=True)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical == {"watched": 38, "aired": 38, "percent": 100.0}
    assert uvs.tv_viewed_from_progress(canonical) is True
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 38
    assert _mirror_exists(u.id, BANSHEE_SHOW)


def test_case18_no_viewed_off_with_100_percent(factory, stub_details,
                                               client):
    """Case 18: THE invariant — Viewed OFF can never coexist with 100%
    canonical progress after unmark (nor Viewed ON with partial before
    marking)."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _login(client, u)
    # Before marking: no badge (no progress).
    html = client.get(f"/tv/{BANSHEE_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    canonical = uvs.canonical_tv_progress(u, BANSHEE_SHOW)
    assert canonical is None
    assert uvs.tv_viewed_from_progress(canonical) is False
    html = client.get(f"/tv/{BANSHEE_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    assert "Mark as Viewed" in html


def test_case19_unmark_single_user_scoped(factory, stub_details, client):
    """Case 19 (privacy): another user's rows and mirror are untouched by
    an unmark."""
    u1, u2 = factory.user("f2a"), factory.user("f2b")
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u1, BANSHEE_SHOW)
    for (sn, ep) in BANSHEE_POSITIONS:
        factory.watch(u2, BANSHEE_SHOW, sn, ep)
    factory.viewed_mirror(u2, BANSHEE_SHOW)
    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    assert _all_rows(u1.id, BANSHEE_SHOW) == []
    assert not _mirror_exists(u1.id, BANSHEE_SHOW)
    assert {(r.season_number, r.episode_number)
            for r in _all_rows(u2.id, BANSHEE_SHOW)} == BANSHEE_POSITIONS
    assert _mirror_exists(u2.id, BANSHEE_SHOW)


def test_case20_unmark_sql_budget(factory, stub_details, client):
    """Case 20: unmark is bounded — exactly one episode-ledger DELETE, no
    per-episode statements."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)

    episode_statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if "tv_episode_watch" in statement:
            episode_statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _before)
    try:
        r = client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
                       follow_redirects=True)
    finally:
        event.remove(db.engine, "before_cursor_execute", _before)
    assert r.status_code == 200
    deletes = [s for s in episode_statements if s.startswith("DELETE")]
    assert len(deletes) == 1
    selects = [s for s in episode_statements if s.startswith("SELECT")]
    assert len(selects) <= 4


# ════════════════════════════════════════════════════════════════════════
# Cases 21–25: preservation & specials on the mark side
# ════════════════════════════════════════════════════════════════════════

def test_case21_mark_preserves_existing_rows(factory, stub_details, client):
    """Case 21: mark-as-viewed never deletes/mutates pre-existing rows —
    ratings, notes, dates survive byte-for-byte."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    from datetime import date
    existing = factory.watch(u, BANSHEE_SHOW, 4, 1, rating=4.5,
                             notes="great", watched_date=date(2026, 1, 15))
    _login(client, u)
    client.get(f"/mark_as_viewed/{BANSHEE_SHOW}/tv", follow_redirects=True)
    kept = TVEpisodeWatch.query.filter_by(
        user_id=u.id, show_id=BANSHEE_SHOW,
        season_number=4, episode_number=1, is_rewatch=False).one()
    assert kept.id == existing.id
    assert kept.rating == 4.5
    assert kept.notes == "great"
    assert kept.watched_date == date(2026, 1, 15)
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 38


def test_case22_mark_idempotent(factory, stub_details, client):
    """Case 22: double mark-as-viewed inserts nothing the second time and
    manufactures no rewatch rows."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    _mark_and_mirror(client, u, BANSHEE_SHOW)
    assert len(_all_rows(u.id, BANSHEE_SHOW)) == 38
    assert not any(r.is_rewatch for r in _all_rows(u.id, BANSHEE_SHOW))


def test_case23_mark_future_never_manufactured(factory, stub_details,
                                               client):
    """Case 23: with future episodes in the calendar, mark-as-viewed
    inserts only the aired set — never E11/E12."""
    u = factory.user()
    DETAILS_CACHE[SPECIALS_SHOW] = _details(10, season=1, seasons={1: 12})
    for ep in range(1, 11):
        factory.aired(SPECIALS_SHOW, 1, ep)
    factory.future(SPECIALS_SHOW, 1, 11)
    factory.future(SPECIALS_SHOW, 1, 12)
    _login(client, u)
    r = client.get(f"/mark_as_viewed/{SPECIALS_SHOW}/tv",
                   follow_redirects=True)
    assert r.status_code == 200
    positions = {(row.season_number, row.episode_number)
                 for row in _all_rows(u.id, SPECIALS_SHOW)}
    assert positions == S1
    canonical = uvs.canonical_tv_progress(u, SPECIALS_SHOW)
    assert canonical == {"watched": 10, "aired": 10, "percent": 100.0}


def test_case24_specials_excluded_from_viewed(factory, stub_details, client):
    """Case 24: an unwatched-but-aired special (season 0) never blocks
    Viewed — the denominator excludes specials."""
    u = factory.user()
    DETAILS_CACHE[SPECIALS_SHOW] = _details(
        10, season=1, seasons={0: 3, 1: 10})
    factory.aired(SPECIALS_SHOW, 0, 1)
    _login(client, u)
    client.get(f"/mark_as_viewed/{SPECIALS_SHOW}/tv", follow_redirects=True)
    positions = {(row.season_number, row.episode_number)
                 for row in _all_rows(u.id, SPECIALS_SHOW)}
    assert (0, 1) not in positions
    canonical = uvs.canonical_tv_progress(u, SPECIALS_SHOW)
    assert canonical == {"watched": 10, "aired": 10, "percent": 100.0}
    assert uvs.tv_viewed_from_progress(canonical) is True


def test_case25_mark_details_resolved_once(factory, stub_details,
                                           monkeypatch):
    """Case 25 (TMDb budget): one mark-as-viewed resolves the cached show
    details exactly once."""
    u = factory.user()
    _banshee()
    calls = {"n": 0}
    real = cw.show_details

    def counting(sid, **kw):
        calls["n"] += 1
        return real(sid, **kw)

    monkeypatch.setattr(cw, "show_details", counting)
    tv_tracking_mod.mark_show_aired_watched_core(u.id, BANSHEE_SHOW)
    assert calls["n"] == 1


# ════════════════════════════════════════════════════════════════════════
# Cases 26–31: legacy stored-counter reconciliation on every surface
# ════════════════════════════════════════════════════════════════════════

def _legacy_fixture(u, show_id=LEGACY_SHOW, status="watching"):
    """Pre-F1 row claiming 8/8 watched; canonical reality is the
    38-episode Banshee-shaped show with only S1E1–E5 in the ledger."""
    _seed_show(show_id)
    row = TVShowProgress(user_id=u.id, show_id=show_id, status=status,
                         total_episodes=8, watched_episodes=8,
                         watched_seasons=1, total_seasons=1)
    db.session.add(row)
    for ep in range(1, 6):
        db.session.add(TVEpisodeWatch(user_id=u.id, show_id=show_id,
                                      season_number=1, episode_number=ep))
    db.session.commit()
    return row


def test_case26_legacy_unfinished_shows_reads_canonical(factory,
                                                        stub_details,
                                                        client):
    """Case 26: /api/tv/unfinished-shows publishes canonical 5/38 — never
    the stale stored 8/8."""
    u = factory.user()
    factory.media(LEGACY_SHOW, "tv", "Legacy Fixture")
    _banshee()
    row = _legacy_fixture(u)
    assert row.watched_episodes == 8 and row.total_episodes == 8
    _login(client, u)
    body = client.get("/api/tv/unfinished-shows").get_json()
    card = next(s for s in body["shows"] if s["show_id"] == LEGACY_SHOW)
    assert card["watched_episodes"] == 5
    assert card["total_episodes"] == 38
    assert card["progress_percent"] == 13.2


def test_case27_legacy_my_shows_reads_canonical(factory, stub_details,
                                                client):
    """Case 27: /api/tv/my-shows serializes through the canonical payload
    — stale 8/8 never leaks; total_episodes means AIRED."""
    u = factory.user()
    _banshee()
    _legacy_fixture(u)
    _login(client, u)
    body = client.get("/api/tv/my-shows").get_json()
    assert body["total"] >= 1
    show = next(s for s in body["shows"] if s["show_id"] == LEGACY_SHOW)
    assert show["watched_episodes"] == 5
    assert show["total_episodes"] == 38
    assert show["aired_episodes"] == 38
    assert show["progress_percentage"] == 13.2


def test_case28_legacy_next_episode_reads_canonical(factory, stub_details,
                                                    client):
    """Case 28: /api/tv/<id>/next-episode progress comes from canonical
    aired reality, not stored counters."""
    u = factory.user()
    _banshee()
    _legacy_fixture(u)
    _login(client, u)
    body = client.get(f"/api/tv/{LEGACY_SHOW}/next-episode").get_json()
    assert body["tracked"] is True
    assert body["progress"] == {"watched": 5, "total": 38, "percent": 13.2}


def test_case29_legacy_profile_percent_is_canonical(factory, stub_details,
                                                    client):
    """Case 29: profile TV-progress rows show the canonical percent
    (5/38 → 13%), never the stale stored 8/8 = 100%."""
    u = factory.user()
    factory.media(LEGACY_SHOW, "tv", "Legacy Fixture")
    _banshee()
    _legacy_fixture(u)
    _login(client, u)
    resp = client.get("/profile")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "13%" in html
    # Scoped to the row label ("{{ percent }}% watched"): the whole-page
    # check would false-positive on CSS gradient stops ("... 100%");").
    assert "100% watched" not in html


def test_case30_legacy_list_card_reads_canonical(factory, stub_details,
                                                 db, client):
    """Case 30: list watched-state enrichment stamps watch_progress from
    canonical aired reality (5/38 = 13%), never the stale 8/8=100%."""
    from models import UserList, UserListItem
    u = factory.user()
    factory.media(LEGACY_SHOW, "tv", "Legacy Fixture")
    _banshee()
    _legacy_fixture(u)
    lst = UserList(user_id=u.id, title="L", slug=f"f2-{uuid4().hex[:10]}",
                   is_public=True)
    db.session.add(lst)
    db.session.flush()
    db.session.add(UserListItem(list_id=lst.id,
                                media_id=MediaItem.query.filter_by(
                                    tmdb_id=LEGACY_SHOW,
                                    media_type="tv").first().id,
                                media_type="tv", position=1))
    db.session.commit()
    _login(client, u)
    body = client.get(f"/api/lists/{lst.id}/watched-state").get_json()
    assert body["items"], "list must contain the TV item"
    assert body["items"][0].get("watch_progress") == {
        "watched": 5, "total": 38, "percent": 13}


def test_case31_legacy_not_started_never_leaks(factory, stub_details,
                                               client):
    """Case 31: a tracked-but-not-started legacy row (8/8 stored, zero
    watch rows) reads as NO progress — stored counters cannot fabricate a
    percentage on any surface."""
    u = factory.user()
    factory.media(LEGACY_SHOW, "tv", "Legacy Fixture")
    _banshee()
    db.session.add(TVShowProgress(user_id=u.id, show_id=LEGACY_SHOW,
                                  status="watching", total_episodes=8,
                                  watched_episodes=8))
    db.session.commit()
    _login(client, u)
    body = client.get("/api/tv/unfinished-shows").get_json()
    card = next(s for s in body["shows"] if s["show_id"] == LEGACY_SHOW)
    assert (card["watched_episodes"], card["total_episodes"],
            card["progress_percent"]) == (0, 0, 0)
    html = client.get(f"/tv/{LEGACY_SHOW}").get_data(as_text=True)
    assert "data-tv-progress" not in html


# ════════════════════════════════════════════════════════════════════════
# Cases 32–35: running show lifecycle, /progress padding, core API budget
# ════════════════════════════════════════════════════════════════════════

def test_case32_running_show_lifecycle(factory, stub_details, client):
    """Case 32: 10/10 Viewed → new episode airs → badge off → watch it →
    badge on again (11/11)."""
    u = factory.user()
    factory.media(RUNNING_SHOW, "tv", "Running Fixture")
    seasons_payload = [
        {"season_number": 1, "episode_count": 10, "name": "Season 1",
         "air_date": "2010-01-01", "poster_path": None}]
    DETAILS_CACHE[RUNNING_SHOW] = _details(
        10, season=1, seasons={1: 10}, status="Returning Series")
    DETAILS_CACHE[RUNNING_SHOW].update(_page_payload(
        RUNNING_SHOW, seasons=seasons_payload, status="Returning Series"))
    _login(client, u)
    client.get(f"/mark_as_viewed/{RUNNING_SHOW}/tv", follow_redirects=True)
    canonical = uvs.canonical_tv_progress(u, RUNNING_SHOW)
    assert canonical == {"watched": 10, "aired": 10, "percent": 100.0}
    assert _has_badge(client.get(
        f"/tv/{RUNNING_SHOW}").get_data(as_text=True))

    DETAILS_CACHE[RUNNING_SHOW] = _details(
        11, season=1, seasons={1: 10}, status="Returning Series")
    DETAILS_CACHE[RUNNING_SHOW].update(_page_payload(
        RUNNING_SHOW, seasons=seasons_payload, status="Returning Series"))
    canonical = uvs.canonical_tv_progress(u, RUNNING_SHOW)
    assert canonical == {"watched": 10, "aired": 11, "percent": 90.9}
    html = client.get(f"/tv/{RUNNING_SHOW}").get_data(as_text=True)
    assert not _has_badge(html)
    assert "Mark as Viewed" in html

    factory.watch(u, RUNNING_SHOW, 1, 11)
    canonical = uvs.canonical_tv_progress(u, RUNNING_SHOW)
    assert canonical == {"watched": 11, "aired": 11, "percent": 100.0}
    assert _has_badge(client.get(
        f"/tv/{RUNNING_SHOW}").get_data(as_text=True))


def test_case33_progress_endpoint_no_stale_denominator(factory,
                                                       stub_details,
                                                       client):
    """Case 33: /api/tv/<id>/progress zero-pads (never legacy) when no
    canonical aired evidence exists — stale counters never publish."""
    u = factory.user()
    _banshee()
    _legacy_fixture(u)
    _login(client, u)
    # Canonical exists (5 watch rows): values are canonical.
    p = client.get(f"/api/tv/{LEGACY_SHOW}/progress").get_json()["progress"]
    assert (p["watched_episodes"], p["total_episodes"],
            p["progress_percentage"]) == (5, 38, 13.2)

    for row in TVEpisodeWatch.query.filter_by(
            user_id=u.id, show_id=LEGACY_SHOW).all():
        db.session.delete(row)
    db.session.commit()
    p = client.get(f"/api/tv/{LEGACY_SHOW}/progress").get_json()["progress"]
    assert (p["watched_episodes"], p["total_episodes"],
            p["progress_percentage"]) == (0, 0, 0)


def test_case34_unmark_no_tmdb_calls(factory, stub_details, client,
                                     monkeypatch):
    """Case 34 (TMDb budget): the unmark route resolves NO TMDb details —
    the counter sync reads only the calendar plus the existing cache."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Fixture")
    _banshee()
    _mark_and_mirror(client, u, BANSHEE_SHOW)

    calls = {"n": 0}

    def cache_only(sid, **kw):
        """Serve the existing cache; count anything that would have been
        a TMDb resolution (a cache miss)."""
        if sid in DETAILS_CACHE:
            return DETAILS_CACHE[sid]
        calls["n"] += 1
        return None

    monkeypatch.setattr(cw, "show_details", cache_only)
    client.get(f"/remove_from_viewed/{BANSHEE_SHOW}/tv",
               follow_redirects=True)
    assert calls["n"] == 0


def test_case35_unmark_core_contract(factory, stub_details):
    """Case 35: unmark_show_watched_core returns None for a non-tracked
    show (no error), and zeroes the ledger of a tracked one."""
    u = factory.user()
    _banshee()
    assert tv_tracking_mod.unmark_show_watched_core(
        u.id, BANSHEE_SHOW) is None
    factory.watch(u, BANSHEE_SHOW, 1, 1)
    factory.progress(u, BANSHEE_SHOW)  # tracking row = "tracked"
    progress = tv_tracking_mod.unmark_show_watched_core(u.id, BANSHEE_SHOW)
    assert progress is not None
    assert progress.watched_episodes == 0
    assert _all_rows(u.id, BANSHEE_SHOW) == []


# ════════════════════════════════════════════════════════════════════════
# Case 36 + §34: the cross-surface invariant
# ════════════════════════════════════════════════════════════════════════

def _surface_states(client, u, show_id):
    """Collect the Viewed verdict from every surface for one show."""
    canonical = uvs.canonical_tv_progress(u, show_id) or {
        "watched": 0, "aired": 0, "percent": 0}
    verdicts = {"canonical": uvs.tv_viewed_from_progress(canonical)}

    # detail page hero badge
    html = client.get(f"/tv/{show_id}").get_data(as_text=True)
    verdicts["detail_badge"] = _has_badge(html)

    # view-state payload (homepage/browse/CineBot source)
    payload = client.get(f"/api/view-state?tv={show_id}").get_json()
    progress = payload["tv_progress"].get(str(show_id))
    verdicts["view_state"] = uvs.tv_viewed_from_progress(progress)

    # unfinished shelf card (show must be watching/paused to appear)
    shelf = client.get("/api/tv/unfinished-shows").get_json()["shows"]
    card = next((s for s in shelf if s["show_id"] == show_id), None)
    verdicts["unfinished_percent"] = (
        card["progress_percent"] == 100 if card else None)

    # my-shows canonical payload
    shows = client.get("/api/tv/my-shows").get_json()["shows"]
    row = next((s for s in shows if s["show_id"] == show_id), None)
    verdicts["my_shows_percent"] = (
        row["progress_percentage"] == 100.0 if row else None)

    # next-episode progress
    body = client.get(f"/api/tv/{show_id}/next-episode").get_json()
    verdicts["next_episode_percent"] = (
        body["progress"]["percent"] == 100.0 if body.get("tracked")
        else None)
    return verdicts, canonical


def test_case36_invariant_full_and_partial_fixture(factory, stub_details,
                                                   client):
    """Case 36 (§34 invariant): every surface agrees with the canonical
    Viewed verdict for BOTH fixtures — 38/38 (Viewed) and 20/38 (not)."""
    u = factory.user()
    factory.media(BANSHEE_SHOW, "tv", "Banshee Invariant")
    _banshee()
    _login(client, u)

    # Fixture A: full 38/38 → Viewed everywhere.
    for (sn, ep) in BANSHEE_POSITIONS:
        factory.watch(u, BANSHEE_SHOW, sn, ep)
    db.session.add(TVShowProgress(user_id=u.id, show_id=BANSHEE_SHOW,
                                  status="watching"))
    db.session.commit()
    verdicts, canonical = _surface_states(client, u, BANSHEE_SHOW)
    assert verdicts["canonical"] is True
    assert verdicts["detail_badge"] is True
    assert verdicts["view_state"] is True
    assert verdicts["unfinished_percent"] is True
    assert verdicts["my_shows_percent"] is True
    assert verdicts["next_episode_percent"] is True
    assert canonical == {"watched": 38, "aired": 38, "percent": 100.0}

    # Fixture B: drop S3+S4 (20/38 = 52.6%) → NOT Viewed everywhere.
    for row in TVEpisodeWatch.query.filter_by(
            user_id=u.id, show_id=BANSHEE_SHOW).filter(
            TVEpisodeWatch.season_number.in_([3, 4])).all():
        db.session.delete(row)
    db.session.commit()
    verdicts, canonical = _surface_states(client, u, BANSHEE_SHOW)
    assert verdicts["canonical"] is False
    assert verdicts["detail_badge"] is False
    assert verdicts["view_state"] is False
    assert verdicts["unfinished_percent"] is False
    assert verdicts["my_shows_percent"] is False
    assert verdicts["next_episode_percent"] is False
    assert canonical == {"watched": 20, "aired": 38, "percent": 52.6}
