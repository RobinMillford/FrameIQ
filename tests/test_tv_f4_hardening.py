"""Task F4 — TV write correctness, canonical-state hardening and security.

F1 unified the TV write paths, F2 made TV Viewed a derived state, F3 made the
suite deterministic and offline. F4 closes the nine defects that audit found
in the remaining TV surface. One rule is under test throughout:

    every TV write  →  the canonical aired universe  →  canonical progress
                    →  canonical Viewed

Nothing above the HTTP boundary is mocked: TMDb payloads come from
``tests/tmdb_offline.py`` and the real cache, fetchers, parsers, aired rules,
resolvers, routes and templates run against them. Only the network is fake.

The nine defects, in the order the sections below cover them:

  1  update_episode_watch's create branch wrote a nonexistent ``watched_at``
     column and answered 500
  2  Continue Watching "Finished" wiped rating / notes / is_rewatch
  3  the single-episode writer had no aired gate (future episodes, specials)
  4  unmark-season / unmark-episode published stored, non-canonical counters
  5  update_season_progress compared max(episode) instead of intersecting
     aired positions, so gapped seasons completed early
  6  start_tracking_show fetched TMDb details and discarded the result
  7  the collection mutations were state-changing GETs with no CSRF
     protection, an unvalidated media_type and no rate limiting
  8  media_card / collection_card derived the TV Viewed badge from the
     non-canonical user_viewed mirror
  9  sync_upcoming_episodes purged aired rows the day they aired, so the
     calendar could only ever contribute ``air_date == today`` to aired
     reality
"""
import os
import re
from datetime import date

import pytest

from tests.tmdb_offline import NO_ANCHOR
from tests.tv_fixtures import (
    FUTURE_OFFSET, PAST_OFFSET, SPECIALS_AIRED, SPECIALS_SEASONS,
    SPECIALS_SHOW, TODAY_OFFSET, days_from_today, login, positions,
)

# Fixed show ids in the 995xxx band, disjoint from the F3 fixtures.
WRITE_SHOW = 995001        # plain aired show (S1 1..6, S2 1..6)
WRITE_SEASONS = {1: 6, 2: 6}
GAPPED_SHOW = 995002       # aired season with a GAP: E1, E2, E4 aired; E3/E5 do not
GAPPED_AIRED = {(1, 1), (1, 2), (1, 4)}
LEGACY_SHOW = 995003       # start_tracking / no-watched-state fixture
GROWING_BADGE_SHOW = 995005  # card-badges-off-when-it-grows fixture


def aired(show_id):
    from api.user_view_state import aired_positions_for_show
    return aired_positions_for_show(show_id)


def progress(user, show_id):
    from api.user_view_state import canonical_tv_progress
    return canonical_tv_progress(user, show_id)


def viewed(user, show_id):
    from api.user_view_state import tv_viewed_from_progress
    return tv_viewed_from_progress(progress(user, show_id))


def watched_rows(user, show_id):
    from api.user_view_state import _watched_rows_for_show
    _, first_watch = _watched_rows_for_show(user.id, show_id)
    return first_watch


def stored_row(user, show_id):
    from models import TVShowProgress
    return TVShowProgress.query.filter_by(
        user_id=user.id, show_id=show_id).first()


def _forget_cached_details():
    """Drop BOTH detail caches so a mid-test payload change is observed."""
    from api.continue_watching import _memo
    from api.tmdb.cache import tmdb_cache

    _memo.clear()
    tmdb_cache._store.clear()


def watch_row(user, show_id, season, episode):
    from models.tv import TVEpisodeWatch
    return TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show_id, season_number=season,
        episode_number=episode).order_by(TVEpisodeWatch.id).first()


def write_row_count(user, show_id):
    from models.tv import TVEpisodeWatch
    return TVEpisodeWatch.query.filter_by(
        user_id=user.id, show_id=show_id).count()


def register(tmdb, tv, user, show_id, seasons=None, *, seasons_map=None,
             anchor=None, status="Ended", **kwargs):
    """Register a deterministic TMDb payload and the local MediaItem row."""
    if seasons_map is not None:
        tmdb.register(rf"^/3/tv/{show_id}$", {
            "id": show_id,
            "name": f"F4 Show {show_id}",
            "status": status,
            "last_episode_to_air": anchor,
            "seasons": seasons_map,
        })
    else:
        tmdb.tv_show(show_id, seasons, status=status,
                     last_episode_to_air=anchor, **kwargs)  # anchor may be None
    tv.show(show_id)
    return show_id


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 1 — update_episode_watch's create branch
# ════════════════════════════════════════════════════════════════════════════

def test_update_watch_create_branch_no_longer_500s(app, db, client, tmdb, tv):
    """Before F4: ``TVEpisodeWatch(watched_at=...)`` — no such column, so
    adding metadata to an unwatched episode raised TypeError and answered
    500. It must create the row and answer 200."""
    user = tv.user()
    register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    response = client.post(
        f"/api/tv/{WRITE_SHOW}/episode/1/1/update-watch",
        json={"rating": 4.0, "notes": "strong opener"})

    assert response.status_code == 200, response.get_data(as_text=True)
    row = watch_row(user, WRITE_SHOW, 1, 1)
    assert row is not None, "the create branch must persist a row"
    assert row.rating == 4.0
    assert row.notes == "strong opener"
    # The real schema's date column is populated (it never was before — the
    # create branch raised before reaching it).
    assert row.watched_date is not None


def test_update_watch_uses_the_existing_date_column(app, db, client, tmdb, tv):
    """The create branch must not reintroduce any nonexistent column."""
    from models.tv import TVEpisodeWatch
    user = tv.user()
    register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    client.post(f"/api/tv/{WRITE_SHOW}/episode/1/1/update-watch",
                json={"rating": 3.5})

    assert "watched_at" not in TVEpisodeWatch.__table__.columns
    row = watch_row(user, WRITE_SHOW, 1, 1)
    assert isinstance(row.watched_date, date)


def test_update_watch_preserves_fields_it_does_not_send(app, db, client, tmdb,
                                                        tv):
    """Only the fields present in the payload are written."""
    user = tv.user()
    register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)
    client.post(f"/api/tv/{WRITE_SHOW}/episode/1/1/update-watch",
                json={"rating": 4.5, "notes": "keep me", "is_rewatch": True})

    client.post(f"/api/tv/{WRITE_SHOW}/episode/1/1/update-watch",
                json={"notes": "edited"})

    row = watch_row(user, WRITE_SHOW, 1, 1)
    assert row.notes == "edited"
    assert row.rating == 4.5, "an omitted field must not be cleared"
    assert row.is_rewatch is True, "an omitted rewatch flag must survive"


def test_update_watch_rejects_an_out_of_range_rating(app, db, client, tmdb, tv):
    """The model's CHECK is surfaced as a 4xx, not a 500 from the constraint."""
    user = tv.user()
    register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    for bad in (9, -1, "not-a-number"):
        response = client.post(
            f"/api/tv/{WRITE_SHOW}/episode/1/1/update-watch",
            json={"rating": bad})
        assert response.status_code == 400, bad


def test_update_watch_maintains_canonical_counters(app, db, client, tmdb, tv):
    """It is now the SAME write path as mark-watched, so the canonical
    counters and completion gating must move with it."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    client.post(f"/api/tv/{show}/episode/1/1/update-watch", json={"rating": 4})

    assert progress(user, show) == {"watched": 1, "aired": 12, "percent": 8.3}
    row = stored_row(user, show)
    assert row.total_episodes == 12
    assert row.watched_episodes == 1
    assert row.watched_seasons == 0


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 2 — Continue Watching "Finished" must not erase metadata
# ════════════════════════════════════════════════════════════════════════════

def test_finish_preserves_rating_notes_and_rewatch(app, db, client, tmdb, tv):
    """The exact regression: an episode carrying the user's own metadata,
    finished from Continue Watching, used to come back with rating=None,
    notes=None, is_rewatch=False."""
    from api import continue_watching as cw

    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    watched_on = date(2021, 3, 4)
    tv.watch(user, show, 1, 1, rating=4.5, notes="great pilot",
             watched_date=watched_on)
    cw.start_item(user.id, "tv", show, season=1, episode=1)

    result = cw.finish_tv_episode(user.id, show, 1, 1)
    assert result["finished"] is True

    row = watch_row(user, show, 1, 1)
    assert row.rating == 4.5, "rating must survive a finish"
    assert row.notes == "great pilot", "notes must survive a finish"
    assert row.watched_date == watched_on, (
        "finish means 'now watched', not 'forget when'")


def test_finish_preserves_the_rewatch_flag(app, db, client, tmdb, tv):
    from api import continue_watching as cw

    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.watch(user, show, 1, 1)
    tv.watch(user, show, 1, 1, rewatch=True)
    # The finish action is re-pointed at the same position.
    row = watch_row(user, show, 1, 1)
    row.is_rewatch = True
    db.session.commit()

    cw.finish_tv_episode(user.id, show, 1, 1)

    assert watch_row(user, show, 1, 1).is_rewatch is True


def test_finish_with_no_prior_metadata_still_works(app, db, client, tmdb, tv):
    """The no-metadata case must keep working, not just the preserve case."""
    from api import continue_watching as cw

    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    cw.start_item(user.id, "tv", show, season=1, episode=1)

    result = cw.finish_tv_episode(user.id, show, 1, 1)

    assert result["finished"] is True
    assert watched_rows(user, show) == {(1, 1)}


def test_re_marking_without_a_payload_preserves_metadata(app, db, client, tmdb,
                                                         tv):
    """The same clobbering reached the plain mark route (no body ⇒ no keys)."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.watch(user, show, 1, 1, rating=4.0, notes="keep")
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/1/1/mark-watched", json={})
    assert response.status_code == 200

    row = watch_row(user, show, 1, 1)
    assert (row.rating, row.notes) == (4.0, "keep")


def test_explicitly_clearing_a_field_still_works(app, db, client, tmdb, tv):
    """Preserve-on-absence must not become 'impossible to clear': sending the
    key with an explicit null/false still writes it."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.watch(user, show, 1, 1, rating=4.0, notes="remove me")
    login(client, user)

    client.post(f"/api/tv/{show}/episode/1/1/mark-watched",
                json={"rating": None, "notes": None})

    row = watch_row(user, show, 1, 1)
    assert row.rating is None and row.notes is None


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 3 — the single-episode write gate
# ════════════════════════════════════════════════════════════════════════════

def test_marking_a_future_episode_is_rejected(app, db, client, tmdb, tv):
    """A verified aired universe makes the gate binding: S1E9 of a 6-episode
    season is not eligible."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/1/9/mark-watched", json={})

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert watched_rows(user, show) == set(), "nothing may be written"
    row = stored_row(user, show)
    assert row.watched_episodes == 0, "no counter may move"


def test_marking_a_special_is_rejected(app, db, client, tmdb, tv):
    """Season 0 is rejected before the aired set is even consulted."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/0/1/mark-watched", json={})

    assert response.status_code == 400
    assert "special" in response.get_json()["reason"].lower()
    assert watched_rows(user, show) == set()


def test_marking_a_nonexistent_season_is_rejected(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    assert client.post(
        f"/api/tv/{show}/episode/9/1/mark-watched", json={}).status_code == 400
    assert watched_rows(user, show) == set()


def test_marking_an_aired_episode_still_succeeds(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/1/1/mark-watched", json={})

    assert response.status_code == 200
    assert watched_rows(user, show) == {(1, 1)}
    assert progress(user, show)["watched"] == 1


def test_a_rejected_write_leaves_no_trace_at_all(app, db, client, tmdb, tv):
    """The gate must be a true no-op: no row, no counter, no status seal."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    before = (stored_row(user, show).watched_episodes,
              stored_row(user, show).total_episodes,
              stored_row(user, show).status)
    client.post(f"/api/tv/{show}/episode/1/9/mark-watched", json={})

    assert write_row_count(user, show) == 0
    row = stored_row(user, show)
    assert (row.watched_episodes, row.total_episodes, row.status) == before, (
        "a rejected write must leave the tracking row byte-for-byte as it was")
    assert row.status != "completed"
    assert row.completed_at is None
    assert progress(user, show) is None


def test_repeated_marking_is_idempotent(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    for _ in range(3):
        assert client.post(
            f"/api/tv/{show}/episode/1/1/mark-watched", json={}).status_code == 200

    assert write_row_count(user, show) == 1, "no duplicate episode rows"
    assert progress(user, show)["watched"] == 1


def test_canonical_progress_never_exceeds_the_aired_denominator(
        app, db, client, tmdb, tv):
    """After any sequence of writes, watched ⊆ aired — the invariant the gate
    exists to protect."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    refused = []
    for season in (1, 2):
        for episode in range(1, 9):
            response = client.post(
                f"/api/tv/{show}/episode/{season}/{episode}/mark-watched",
                json={})
            if response.status_code == 400:
                refused.append((season, episode))

    universe = aired(show)
    ledger = watched_rows(user, show)
    assert refused == [(1, 7), (1, 8), (2, 7), (2, 8)], (
        f"only the episodes outside the aired universe may be refused: {refused}")
    assert ledger == universe, (
        f"the ledger must equal the aired universe: {ledger ^ universe}")
    result = progress(user, show)
    assert result["watched"] == result["aired"] == 12
    assert result["percent"] <= 100.0
    assert viewed(user, show) is True, (
        "12 valid writes may complete a 12-episode show")


def test_finishing_a_future_episode_over_http_is_a_400(app, db, client, tmdb, tv):
    """The Continue Watching route surfaces the rejection, not a fake success."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(
        f"/api/continue-watching/tv/{show}/1/9/finish")

    assert response.status_code == 400
    assert response.get_json()["finished"] is False
    assert watched_rows(user, show) == set()


def test_eligibility_is_pure_and_coerces_its_inputs(app, db, tmdb, tv):
    """The rule itself, called directly — no route, no request."""
    from api.user_view_state import episode_eligibility

    aired_set = {(1, 1), (1, 2)}
    assert episode_eligibility(1, 1, 1, aired=aired_set) is None
    assert episode_eligibility(1, 1, 2, aired=aired_set) is None
    assert episode_eligibility(1, 0, 1, aired=aired_set) is not None
    assert episode_eligibility(1, 1, 0, aired=aired_set) is not None
    assert episode_eligibility(1, 1, 3, aired=aired_set) is not None
    assert episode_eligibility(1, "x", 1, aired=aired_set) is not None
    # An empty universe is "no evidence", not "nothing is eligible".
    assert episode_eligibility(1, 4, 4, aired=set()) is None


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 4 — unmark endpoints must publish the canonical payload
# ════════════════════════════════════════════════════════════════════════════

def test_unmark_season_returns_canonical_progress(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    response = client.post(f"/api/tv/{show}/season/1/unmark-watched")
    body = response.get_json()

    assert response.status_code == 200
    canonical = progress(user, show)
    assert body["progress"]["watched_episodes"] == canonical["watched"]
    assert body["progress"]["total_episodes"] == canonical["aired"]
    assert body["progress"]["watched_episodes"] == 6
    assert body["viewed"] is False


def test_unmark_episode_returns_canonical_progress(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    response = client.post(f"/api/tv/{show}/episode/1/1/unmark-watched")
    body = response.get_json()

    assert response.status_code == 200
    canonical = progress(user, show)
    assert body["progress"]["watched_episodes"] == canonical["watched"] == 11
    assert body["progress"]["total_episodes"] == canonical["aired"] == 12
    assert body["viewed"] is False


def test_unmark_ignores_stale_stored_counters(app, db, client, tmdb, tv):
    """The legacy row claims 12/12; the ledger says 11/12. The published
    payload must follow the ledger."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show, total=12, watched=12, watched_seasons=2,
             total_seasons=2)
    tv.watch_all(user, show, WRITE_SEASONS)
    db.session.delete(watch_row(user, show, 1, 1))
    db.session.commit()
    login(client, user)

    body = client.post(
        f"/api/tv/{show}/episode/1/1/unmark-watched").get_json()

    assert body["progress"]["watched_episodes"] == 11
    assert body["progress"]["total_episodes"] == 12
    assert body["viewed"] is False


def test_unmark_on_a_show_with_future_episodes_keeps_aired_denominator(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS,
                    anchor={"season_number": 2, "episode_number": 6,
                            "air_date": days_from_today(PAST_OFFSET)})
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    body = client.post(
        f"/api/tv/{show}/season/1/unmark-watched").get_json()

    assert body["progress"]["total_episodes"] == 12, (
        "unaired episodes must not enter the denominator on unmark either")
    assert body["viewed"] is False


def test_repeated_unmark_is_stable(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    first = client.post(f"/api/tv/{show}/season/1/unmark-watched").get_json()
    second = client.post(f"/api/tv/{show}/season/1/unmark-watched").get_json()

    assert first == second, "a second unmark must not move anything"
    assert second["progress"]["watched_episodes"] == 6


def test_unmark_a_specials_season_does_not_touch_real_episodes(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, SPECIALS_SHOW, SPECIALS_SEASONS,
                    anchor={"season_number": 1, "episode_number": 10,
                            "air_date": days_from_today(PAST_OFFSET)})
    tv.track(user, show)
    tv.watch_all(user, show, SPECIALS_SEASONS)
    login(client, user)

    body = client.post(f"/api/tv/{show}/season/0/unmark-watched").get_json()

    assert body["progress"]["total_episodes"] == SPECIALS_AIRED
    assert body["progress"]["watched_episodes"] == SPECIALS_AIRED


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 5 — gapped seasons must use aired set membership
# ════════════════════════════════════════════════════════════════════════════

def _gapped(tmdb, tv, user):
    """A season whose AIRED positions are E1, E2, E4 — E3 and E5 never aired.

    The gap is expressed through the SYNCED CALENDAR with no TMDb anchor,
    because the anchor rule is contiguous by construction (everything up to
    ``last_episode_to_air``). This is the realistic shape for a show whose
    TMDb payload lacks the anchor — precisely the case the retained aired
    history (defect 9) exists to cover — and it is where the old
    ``max(episode_number)`` rule gives the wrong answer: watched E1, E2 and a
    phantom E9 satisfies ``9 >= 4`` without E4 ever being watched.
    """
    tmdb.tv_show(GAPPED_SHOW, {1: 6},
                 last_episode_to_air=NO_ANCHOR,
                 season_entries=[{"season_number": 1, "episode_count": 6,
                                  "air_date": days_from_today(PAST_OFFSET),
                                  "id": 1, "name": "Season 1"}])
    tv.show(GAPPED_SHOW)
    aired_on = days_from_today(PAST_OFFSET)
    for episode in (1, 2, 4):
        tv.schedule(GAPPED_SHOW, 1, episode, aired_on)
    assert aired(GAPPED_SHOW) == GAPPED_AIRED, (
        f"fixture must present a real gap, got {aired(GAPPED_SHOW)}")
    return GAPPED_SHOW


def test_a_gapped_season_completes_only_on_membership(app, db, tmdb, tv):
    """Aired = {E1, E2, E4}. Watching all three completes the season; the
    highest aired episode number (4) is NOT the count (3)."""
    from api.user_view_state import complete_seasons_from_positions

    assert complete_seasons_from_positions(
        GAPPED_AIRED, {(1, 1), (1, 2), (1, 4)}) == 1
    assert complete_seasons_from_positions(
        GAPPED_AIRED, {(1, 1), (1, 2)}) == 0, "a gap keeps it incomplete"
    assert complete_seasons_from_positions(
        GAPPED_AIRED, {(1, 1), (1, 2), (1, 4), (1, 9)}) == 1, (
        "a row outside the aired universe cannot complete a season either")


def test_gapped_season_watched_seasons_through_a_write_path(app, db, client,
                                                            tmdb, tv):
    user = tv.user()
    show = _gapped(tmdb, tv, user)
    tv.track(user, show)
    login(client, user)

    tv.watch_many(user, show, {(1, 1), (1, 2)})
    client.post(f"/api/tv/{show}/episode/1/4/unmark-watched")

    row = stored_row(user, show)
    assert row.watched_seasons == 0, (
        "E1+E2 watched with E4 outstanding is not a finished season")

    client.post(f"/api/tv/{show}/episode/1/4/mark-watched")
    assert stored_row(user, show).watched_seasons == 1


def test_a_stray_row_outside_the_aired_set_cannot_complete_a_gapped_season(
        app, db, client, tmdb, tv):
    """The old max-number bug, reproduced: watched E1, E2 and a phantom E9
    while aired is {E1, E2, E4}. ``9 >= 4`` used to score it complete."""
    user = tv.user()
    show = _gapped(tmdb, tv, user)
    tv.track(user, show)

    # Pre-gate legacy residue: rows written before F4 exist and must not be
    # deleted — they must simply stop driving the counters.
    tv.watch_many(user, show, {(1, 1), (1, 2), (1, 9)})
    login(client, user)

    # Before E4 is watched: E1, E2 and a phantom E9 are the ledger. The old
    # rule scored the season complete (max watched 9 >= max aired 4); set
    # membership does not.
    client.post(f"/api/tv/{show}/episode/1/1/update-watch", json={"notes": "x"})
    assert stored_row(user, show).watched_seasons == 0, (
        "a row outside the aired universe cannot complete a season")

    client.post(f"/api/tv/{show}/episode/1/4/mark-watched")
    row = stored_row(user, show)
    assert row.watched_seasons == 1, (
        "now every aired position (E1, E2, E4) is watched")
    assert write_row_count(user, show) == 4, "history is never deleted"
    result = progress(user, show)
    assert result["watched"] == 3 and result["aired"] == 3


def test_gapped_and_contiguous_seasons_agree_with_canonical_progress(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = _gapped(tmdb, tv, user)
    tv.track(user, show)
    login(client, user)
    client.post(f"/api/tv/{show}/season/1/mark-watched")

    row = stored_row(user, show)
    canonical = progress(user, show)
    assert row.watched_episodes == canonical["watched"] == 3
    assert row.total_episodes == canonical["aired"] == 3
    assert row.watched_seasons == 1
    assert viewed(user, show) is True


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 6 — start_tracking_show wasted a TMDb request
# ════════════════════════════════════════════════════════════════════════════

def test_start_tracking_makes_no_wasted_details_request(app, db, client, tmdb, tv):
    """The defect was a SECOND, discarded resolution: ``start_tracking_show``
    fetched show details and threw them away, on top of the one
    ``sync_tv_progress_counters`` legitimately needs to derive the canonical
    denominators. So exactly one resolution may happen, not two."""
    # Log in BEFORE registering the payload, then reset the call record: the
    # login landing page resolves show metadata of its own, and the budget
    # under test is the one this route spends, not the session's.
    user = tv.user()
    login(client, user)
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tmdb.reset_calls()

    response = client.post(f"/api/tv/{show}/start-tracking")

    assert response.status_code == 201
    assert tmdb.count(rf"^/3/tv/{show}$") == 1, (
        "start-tracking must resolve details exactly once (for the canonical "
        "counters), never twice and never for a discarded result")
    row = stored_row(user, show)
    assert row.status == "watching" and row.total_episodes == 12


def test_start_tracking_survives_an_unavailable_tmdb_payload(app, db, client, tmdb, tv):
    """If the metadata fetch is what the removed dead call used to do, a show
    with no payload must still track: the counters degrade to zero."""
    user = tv.user()
    tv.show(LEGACY_SHOW)          # local cache only; no TMDb payload at all
    login(client, user)

    response = client.post(f"/api/tv/{LEGACY_SHOW}/start-tracking")

    assert response.status_code == 201
    row = stored_row(user, LEGACY_SHOW)
    assert row is not None and row.status == "watching"
    assert (row.watched_episodes, row.total_episodes) == (0, 0)


def test_start_tracking_creates_no_false_watches(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    client.post(f"/api/tv/{show}/start-tracking")

    assert watched_rows(user, show) == set()
    row = stored_row(user, show)
    assert row.watched_episodes == 0
    assert row.watched_seasons == 0
    assert viewed(user, show) is False


def test_start_tracking_is_still_single_and_user_scoped(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    login(client, user)

    assert client.post(f"/api/tv/{show}/start-tracking").status_code == 201
    assert client.post(f"/api/tv/{show}/start-tracking").status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 7 — HTTP semantics, CSRF, validation, rate limiting, user scoping
# ════════════════════════════════════════════════════════════════════════════

COLLECTION_MUTATIONS = (
    ("/mark_as_viewed/{sid}/tv", "POST"),
    ("/remove_from_viewed/{sid}/tv", "POST"),
    ("/add_to_watchlist/{sid}/tv", "POST"),
    ("/remove_from_watchlist/{sid}/tv", "POST"),
)


@pytest.mark.parametrize("template,method", COLLECTION_MUTATIONS)
def test_collection_mutations_reject_get(app, db, client, tmdb, tv, template, method):
    """Task F4: GET must be read-only. The old GET performed the write, and
    because Flask-WTF only guards unsafe methods, any page could trigger it
    cross-site. GET now answers 405."""
    user = tv.user()
    login(client, user)
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tmdb.reset_calls()

    response = client.get(template.format(sid=show))

    assert response.status_code == 405, (
        "a state-changing GET must not exist")
    assert write_row_count(user, show) == 0, "GET must write nothing"
    assert tmdb.count(rf"^/3/tv/{show}$") == 0, (
        "the 405 must be raised during routing, before any work: "
        f"{tmdb.paths()}")


@pytest.mark.parametrize("template,method", COLLECTION_MUTATIONS)
def test_collection_mutations_require_authentication(app, db, client, tmdb, tv,
                                                     template, method):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)

    response = client.post(template.format(sid=show), follow_redirects=False)

    assert response.status_code in (302, 401)
    assert write_row_count(user, show) == 0


@pytest.mark.parametrize("template,method", COLLECTION_MUTATIONS)
def test_collection_mutations_require_a_csrf_token(app, db, client, tmdb, tv,
                                                   template, method):
    """The repository's own CSRF mechanism (CSRFProtect) guards the new POST
    routes — no second CSRF implementation is introduced."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)
    previous = app.config["WTF_CSRF_ENABLED"]
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        missing = client.post(template.format(sid=show), follow_redirects=False)
        wrong = client.post(
            template.format(sid=show), data={"csrf_token": "not-a-token"},
            follow_redirects=False)
    finally:
        app.config["WTF_CSRF_ENABLED"] = previous

    assert missing.status_code == 400, "a missing CSRF token must be refused"
    assert wrong.status_code == 400, "an invalid CSRF token must be refused"
    assert write_row_count(user, show) == 0


def test_a_valid_csrf_token_is_accepted(app, db, client, tmdb, tv):
    """The positive control: with CSRF on, a real token succeeds."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)
    previous = app.config["WTF_CSRF_ENABLED"]
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        # generate_csrf stores the raw token in the session and returns the
        # signed value the client must echo back.
        with client.session_transaction() as session:
            session["csrf_token"] = _raw_csrf(app)
            token = session["csrf_token"]
        signed = _sign_csrf(app, token)
        response = client.post(
            f"/mark_as_viewed/{show}/tv",
            data={"csrf_token": signed}, follow_redirects=True)
    finally:
        app.config["WTF_CSRF_ENABLED"] = previous

    assert response.status_code == 200
    assert watched_rows(user, show) == positions(WRITE_SEASONS)


@pytest.mark.parametrize("bad_type", ["foo", "TV", "movies", "tv;drop"])
def test_invalid_media_type_is_rejected(app, db, client, tmdb, tv, bad_type):
    """``media_type`` selects whether canonical TV state is synchronised, so
    an unvalidated value must never reach a code branch or a row.

    The answer must be a 4xx: an unknown media type names a resource this app
    does not model, so it is refused the way routes/main.py refuses a missing
    show. A flash-and-redirect would answer 200/302 and read as a successful
    write, which is the failure mode this test exists to prevent.
    """
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(f"/mark_as_viewed/{show}/{bad_type}",
                           follow_redirects=False)

    assert response.status_code == 404, (
        f"an unvalidated media_type must answer 4xx, got "
        f"{response.status_code}")
    from models import MediaItem, db as _db, user_viewed
    rows = _db.session.execute(user_viewed.select()).fetchall()
    assert all(row.media_type in ("movie", "tv") for row in rows), (
        f"an unvalidated media_type was persisted: {rows}")
    assert watched_rows(user, show) == set(), (
        "an unknown media_type must not trigger the TV write path")
    assert MediaItem.query.filter_by(
        tmdb_id=show, media_type=bad_type).first() is None


@pytest.mark.parametrize("route", [
    "/add_to_watchlist", "/remove_from_watchlist",
    "/mark_as_viewed", "/remove_from_viewed",
])
@pytest.mark.parametrize("bad_type", ["foo", "MOVIE", "tv extra"])
def test_every_collection_mutation_rejects_an_unknown_media_type_with_4xx(
        app, db, client, tmdb, tv, route, bad_type):
    """All four POST mutations validate identically — one had a redirect left
    in it, so assert the whole family rather than the one under test."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    response = client.post(f"{route}/{show}/{bad_type}",
                           follow_redirects=False)

    assert response.status_code == 404, (
        f"{route} answered {response.status_code} for media_type={bad_type!r}")
    assert watched_rows(user, show) == set()


def test_non_integer_media_id_never_reaches_a_write(app, db, client, tmdb, tv):
    user = tv.user()
    login(client, user)

    response = client.post("/mark_as_viewed/not-a-number/tv",
                           follow_redirects=False)
    assert response.status_code == 404


def test_negative_season_and_episode_are_rejected(app, db, client, tmdb, tv):
    """A negative position cannot satisfy the aired gate, but it must not be
    turned into a database write on the way there either."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    for season, episode in ((0, 1), (1, 0), (-1, 2)):
        response = client.post(
            f"/api/tv/{show}/episode/{season}/{episode}/mark-watched", json={})
        assert response.status_code in (400, 404), (season, episode)
    assert write_row_count(user, show) == 0


def test_tv_write_routes_declare_a_rate_limit():
    """Every mutating TV route carries an explicit limit on the repository's
    shared limiter.

    Declared limits are not introspectable in Flask-Limiter 4 (the decorator
    leaves no attribute on the view), so the contract is asserted two ways:
    a source check that each mutating route is decorated, and a functional
    429 test below that the limit string actually enforces.
    """
    import inspect

    from routes import collections as collections_mod
    from routes import tv_tracking as tv_tracking_mod

    tracked = ("mark_episode_watched", "mark_season_watched",
               "mark_all_watched", "update_episode_watch", "start_tracking_show",
               "unmark_season_watched", "unmark_single_episode",
               "mark_as_viewed", "remove_from_viewed", "add_to_watchlist",
               "remove_from_watchlist")
    for name in tracked:
        for module in (tv_tracking_mod, collections_mod):
            view = getattr(module, name, None)
            if view is None:
                continue
            lines = inspect.getsource(module).splitlines()
            index = next(i for i, ln in enumerate(lines)
                         if ln.startswith(f"def {name}("))
            decorators, cursor = [], index - 1
            while cursor >= 0 and lines[cursor].lstrip().startswith("@"):
                decorators.append(lines[cursor].strip())
                cursor -= 1
            assert any(d.startswith("@limiter.limit(") for d in decorators), (
                f"{name} carries {decorators} — no @limiter.limit decorator")


def test_rate_limit_enforced_and_returns_retry_after():
    """The declared limit is real: it answers 429 with the conventional
    Retry-After header once the window is exhausted.

    Built on its own Flask app + Limiter, the same construction
    tests/test_shared_limiter.py uses — the suite-wide app runs with
    RATELIMIT_ENABLED=False, so its limiter is never initialised.
    """
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    from routes.tv_tracking import TV_WRITE_LIMIT

    probe = Flask(__name__)
    probe.config["RATELIMIT_ENABLED"] = True
    # FrameIQ itself leaves limit headers off, so Retry-After is not part of
    # the application's current response contract. Enabling them on this
    # probe only proves the library still emits the conventional header, so
    # enabling it later cannot silently lose it.
    probe.config["RATELIMIT_HEADERS_ENABLED"] = True
    limiter = Limiter(get_remote_address, storage_uri="memory://",
                      default_limits=[])
    limiter.init_app(probe)

    @probe.route("/api/tv/write", methods=["POST"])
    @limiter.limit(TV_WRITE_LIMIT)
    def _write():  # noqa: F811
        return "ok"

    per_minute = int(re.search(r"(\d+)\s+per\s+minute",
                               TV_WRITE_LIMIT).group(1))
    statuses = [probe.test_client().post("/api/tv/write").status_code
                for _ in range(per_minute + 5)]

    assert statuses[:per_minute] == [200] * per_minute, (
        f"normal usage must not be throttled: {statuses[:5]}")
    assert 429 in statuses, f"abuse was never refused: {statuses}"
    limited = probe.test_client().post("/api/tv/write")
    assert limited.status_code == 429
    assert limited.headers.get("Retry-After"), (
        "the conventional Retry-After header must be preserved")


def test_valkey_shared_storage_path_is_untouched_by_f4():
    """The production Valkey wiring is a settings concern, not an F4 change:
    the module still selects shared storage from the env var and still arms
    the native in-memory fallback. Guarded so F4 cannot quietly regress it."""
    import importlib

    import extensions as extensions_mod

    original_uri = extensions_mod._URI
    original_env = os.environ.get("RATELIMIT_STORAGE_URI")
    os.environ["RATELIMIT_STORAGE_URI"] = "redis://:pw@valkey:6379/0"
    try:
        importlib.reload(extensions_mod)
        assert extensions_mod._URI.startswith("redis://")
        assert extensions_mod.limiter._storage_uri.startswith("redis://")
        assert extensions_mod.limiter._in_memory_fallback_enabled is True, (
            "the fallback is what keeps the app serving when Valkey is down")
    finally:
        if original_env is None:
            os.environ.pop("RATELIMIT_STORAGE_URI", None)
        else:
            os.environ["RATELIMIT_STORAGE_URI"] = original_env
        importlib.reload(extensions_mod)
        assert extensions_mod._URI == original_uri


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 7 (cont.) — user scoping / horizontal privilege escalation
# ════════════════════════════════════════════════════════════════════════════

def test_one_user_cannot_unmark_another_users_episodes(app, db, client, tmdb,
                                                       tv):
    alice = tv.user("f4-alice")
    bob = tv.user("f4-bob")
    show = register(tmdb, tv, alice, WRITE_SHOW, WRITE_SEASONS)
    tv.track(bob, show)
    tv.watch_all(alice, show, WRITE_SEASONS)
    login(client, bob)

    client.post(f"/api/tv/{show}/episode/1/1/unmark-watched")

    assert watched_rows(alice, show) == positions(WRITE_SEASONS), (
        "bob must not be able to delete alice's watch rows")


def test_one_user_cannot_remove_another_users_viewed_state(app, db, client, tmdb, tv):
    alice = tv.user("f4-alice")
    bob = tv.user("f4-bob")
    show = register(tmdb, tv, alice, WRITE_SHOW, WRITE_SEASONS)
    tv.track(bob, show)
    tv.watch_all(alice, show, WRITE_SEASONS)
    login(client, alice)
    client.post(f"/mark_as_viewed/{show}/tv", follow_redirects=True)
    from tests.tv_fixtures import login as _login

    _login(client, bob)
    client.post(f"/remove_from_viewed/{show}/tv", follow_redirects=True)

    assert watched_rows(alice, show) == positions(WRITE_SEASONS)
    assert progress(alice, show)["watched"] == 12


def test_one_user_cannot_mark_episodes_for_another(app, db, client, tmdb, tv):
    alice = tv.user("f4-alice")
    bob = tv.user("f4-bob")
    show = register(tmdb, tv, alice, WRITE_SHOW, WRITE_SEASONS)
    tv.track(bob, show)
    login(client, bob)

    client.post(f"/api/tv/{show}/episode/1/1/mark-watched", json={})

    assert watched_rows(alice, show) == set()
    assert watched_rows(bob, show) == {(1, 1)}


def test_tv_episode_routes_reject_a_user_id_override(app, db, client, tmdb,
                                                     tv):
    """There is no user_id parameter to override: the write is always
    ``current_user``."""
    alice = tv.user("f4-alice")
    show = register(tmdb, tv, alice, WRITE_SHOW, WRITE_SEASONS)
    tv.track(alice, show)
    login(client, alice)

    for attempt in (
        {"user_id": 999999},
        {"user_id": 999999, "rating": 4},
    ):
        response = client.post(
            f"/api/tv/{show}/episode/1/1/update-watch?user_id=999999",
            json=attempt)
        assert response.status_code in (200, 400)

    from models.tv import TVEpisodeWatch
    owners = {row.user_id for row in TVEpisodeWatch.query.filter_by(
        show_id=show).all()}
    assert owners == {alice.id}, f"write landed on {owners}"


def test_canonical_state_never_crosses_users(app, db, client, tmdb, tv):
    alice = tv.user("f4-alice")
    bob = tv.user("f4-bob")
    show = register(tmdb, tv, alice, WRITE_SHOW, WRITE_SEASONS)
    tv.track(bob, show)
    tv.watch_all(alice, show, WRITE_SEASONS)

    assert viewed(alice, show) is True
    assert viewed(bob, show) is False
    assert progress(bob, show) is None


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 8 — card Viewed badges are canonical
# ════════════════════════════════════════════════════════════════════════════

def _card_badges(html, show_id):
    """(viewed badge present, mark control present, unmark control present)."""
    return (
        ">Viewed<" in html,
        bool(re.search(r"/mark_as_viewed/%d/tv" % show_id, html)),
        bool(re.search(r"/remove_from_viewed/%d/tv" % show_id, html)),
    )


def _csrf_helpers():
    """(raw-token generator, signer) using flask_wtf's own primitives."""
    from itsdangerous import URLSafeTimedSerializer
    from flask import current_app

    def raw():
        from flask_wtf.csrf import generate_csrf
        with current_app.test_request_context():
            return generate_csrf()

    def sign(value):
        return URLSafeTimedSerializer(
            current_app.config["SECRET_KEY"],
            salt="wtf-csrf-token").dumps(value)

    return raw, sign


def _raw_csrf(app):
    from flask_wtf.csrf import generate_csrf
    with app.test_request_context():
        return generate_csrf()


def _sign_csrf(app, value):
    from itsdangerous import URLSafeTimedSerializer
    serializer = URLSafeTimedSerializer(
        app.config["SECRET_KEY"], salt="wtf-csrf-token")
    return serializer.dumps(value)


def test_card_viewed_badge_is_canonical_at_completion(app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    html = client.get("/viewed").get_data(as_text=True)
    viewed_badge, _mark, unmark = _card_badges(html, show)

    assert viewed_badge is True
    assert unmark is True, (
        "a canonically viewed card must offer the unmark control")


def test_card_viewed_badge_is_off_when_partially_watched(app, db, client,
                                                         tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_season(user, show, WRITE_SEASONS, 1)
    login(client, user)

    html = client.get("/viewed").get_data(as_text=True)
    viewed_badge, _mark, unmark = _card_badges(html, show)

    assert viewed_badge is False, "6/12 is not Viewed"
    assert unmark is False, (
        "an unviewed card must not offer the unmark control")

    # The watchlist surface carries the Mark control, and it must agree.
    wl_html = client.get("/watchlist").get_data(as_text=True)
    tv_id = 995400
    assert bool(re.search(r"/mark_as_viewed/%d/tv" % tv_id, wl_html)) is False


def test_a_stale_user_viewed_row_cannot_badge_a_partial_show(app, db, client, tmdb, tv):
    """The defect: the badge used to read the user_viewed mirror directly, so
    a stale row badged a show the ledger says is 6/12."""
    from models import MediaItem, db as _db, user_viewed

    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_season(user, show, WRITE_SEASONS, 1)
    item = MediaItem.query.filter_by(tmdb_id=show, media_type="tv").first()
    _db.session.execute(user_viewed.insert().values(
        user_id=user.id, media_id=item.id, media_type="tv"))
    _db.session.commit()
    login(client, user)

    html = client.get("/viewed").get_data(as_text=True)

    assert viewed(user, show) is False
    assert _card_badges(html, show)[0] is False, (
        "a stale mirror row must not badge the card")


def test_a_completed_show_is_badged_without_any_mirror_row(app, db, client, tmdb, tv):
    """The other direction: no mirror row, but the ledger is complete."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_all(user, show, WRITE_SEASONS)
    login(client, user)

    html = client.get("/viewed").get_data(as_text=True)

    assert _card_badges(html, show)[0] is True


def test_a_newly_aired_episode_turns_the_badge_off(app, db, client, tmdb, tv):
    """Completion is not latched, on cards either."""
    # A show whose season 3 has ONE aired episode, then a second.
    grown = {1: 6, 2: 6, 3: 1}
    user = tv.user()
    register(tmdb, tv, user, GROWING_BADGE_SHOW, grown,
             anchor={"season_number": 3, "episode_number": 1,
                     "air_date": days_from_today(PAST_OFFSET)})
    tv.track(user, GROWING_BADGE_SHOW)
    tv.watch_season(user, GROWING_BADGE_SHOW, grown, 1)
    tv.watch_season(user, GROWING_BADGE_SHOW, grown, 2)
    login(client, user)

    html = client.get("/viewed").get_data(as_text=True)
    assert _card_badges(html, GROWING_BADGE_SHOW)[0] is False, (
        "12 of 13 aired episodes is not Viewed")
    assert viewed(user, GROWING_BADGE_SHOW) is False
    assert progress(user, GROWING_BADGE_SHOW)["aired"] == 13

    # Catch up: the badge comes on, from the SAME canonical source.
    client.post(f"/api/tv/{GROWING_BADGE_SHOW}/episode/3/1/mark-watched",
                json={})
    assert viewed(user, GROWING_BADGE_SHOW) is True
    assert _card_badges(client.get("/viewed").get_data(as_text=True),
                        GROWING_BADGE_SHOW)[0] is True

    # ...and a NEW episode airs: the badge must go off again, unprompted.
    tmdb.tv_show(GROWING_BADGE_SHOW, {1: 6, 2: 6, 3: 2},
                 last_episode_to_air={
                     "season_number": 3, "episode_number": 2,
                     "air_date": days_from_today(PAST_OFFSET)})
    _forget_cached_details()

    assert progress(user, GROWING_BADGE_SHOW)["aired"] == 14
    assert viewed(user, GROWING_BADGE_SHOW) is False
    assert _card_badges(client.get("/viewed").get_data(as_text=True),
                        GROWING_BADGE_SHOW)[0] is False


def test_card_badge_agrees_with_the_detail_hero_and_view_state(
        app, db, client, tmdb, tv):
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.watch_season(user, show, WRITE_SEASONS, 1)
    login(client, user)

    card = client.get("/viewed").get_data(as_text=True)
    hero = client.get(f"/tv/{show}").get_data(as_text=True)
    state = client.get(f"/api/view-state?tv={show}").get_json()

    assert _card_badges(card, show)[0] is False
    assert "Unmark Viewed" not in hero
    assert state["tv_progress"][str(show)]["watched"] == 6
    assert viewed(user, show) is False


def test_canonical_tv_viewed_keys_is_batched_not_per_card(app, db, client, tmdb, tv):
    """No card N+1: 20 TV cards on one page cost a constant statement count,
    and never more than one TMDb resolution per show."""
    from utils.collections import canonical_tv_viewed_keys

    user = tv.user()
    ids = []
    for index in range(20):
        show_id = 995100 + index
        register(tmdb, tv, user, show_id, WRITE_SEASONS)
        tv.track(user, show_id)
        tv.watch_season(user, show_id, WRITE_SEASONS, 1)
        ids.append(show_id)
    login(client, user)

    with statements() as recorder:
        keys = canonical_tv_viewed_keys(user, ids)

    assert keys == set(), "6 of 12 episodes is not Viewed for any of them"
    selects = [s for s in recorder.captured
               if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 4, (
        f"20 cards issued {len(selects)} SELECTs — an N+1 crept back in")


def test_canonical_tv_viewed_keys_reports_only_viewed_shows(app, db, tmdb, tv):
    from utils.collections import canonical_tv_viewed_keys

    user = tv.user()
    done = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    partial = register(tmdb, tv, user, 995200, WRITE_SEASONS)
    tv.track(user, done)
    tv.watch_all(user, done, WRITE_SEASONS)
    tv.track(user, partial)
    tv.watch_season(user, partial, WRITE_SEASONS, 1)

    keys = canonical_tv_viewed_keys(user, [done, partial])

    assert keys == {(done, "tv")}


def test_canonical_tv_viewed_keys_is_empty_for_anonymous(app, db):
    from types import SimpleNamespace

    from utils.collections import canonical_tv_viewed_keys

    anonymous = SimpleNamespace(is_authenticated=False)
    assert canonical_tv_viewed_keys(anonymous, [1, 2, 3]) == set()
    assert canonical_tv_viewed_keys(None, [1, 2, 3]) == set()


class statements:
    """Record every SQL statement executed inside the block."""

    def __enter__(self):
        from sqlalchemy import event as sa_event

        from models import db as _db
        self.captured = []
        self._fn = (lambda conn, cursor, statement, *a, **k:
                    self.captured.append(" ".join(statement.split())))
        sa_event.listen(_db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event as sa_event

        from models import db as _db
        sa_event.remove(_db.engine, "before_cursor_execute", self._fn)
        return False


# ════════════════════════════════════════════════════════════════════════════
# DEFECT 9 — historical aired evidence survives the sync
# ════════════════════════════════════════════════════════════════════════════

def test_sync_retains_a_bounded_window_of_aired_episodes(app, db):
    """The purge used to cut at ``air_date < today``, so the calendar could
    only ever contribute ``air_date == today``. It must now keep the same
    look-back window it fetches."""
    import importlib.util
    from pathlib import Path

    path = Path("scripts/sync_upcoming_episodes.py")
    source = path.read_text()

    assert "AIRED_RETENTION_DAYS" in source
    assert "UPCOMING_HORIZON_DAYS" in source
    assert "UpcomingEpisode.air_date < today)" not in source, (
        "the old purge boundary must be gone")
    assert "retention_cutoff" in source

    # and the retention window is a real, positive, bounded number
    spec = importlib.util.spec_from_file_location("sync_under_test", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pytest.skip("sync script requires TMDB_API_KEY; source checks stand")
    assert module.AIRED_RETENTION_DAYS > 0
    assert module.UPCOMING_HORIZON_DAYS == 60


def test_historical_aired_positions_stay_reconstructable(app, db, tmdb, tv):
    """S1 and S2 are old and fully aired; the current season has a recent and
    a future episode. The aired universe must contain all of the old ones."""
    show_id = 995300
    tv.schedule(show_id, 1, 1, days_from_today(PAST_OFFSET))
    tv.schedule(show_id, 1, 2, days_from_today(PAST_OFFSET))
    tv.schedule(show_id, 2, 1, days_from_today(PAST_OFFSET))
    tmdb.tv_show(show_id, {1: 2, 2: 1, 3: 2}, last_episode_to_air={
        "season_number": 3, "episode_number": 1,
        "air_date": days_from_today(PAST_OFFSET)})

    universe = aired(show_id)

    assert {(1, 1), (1, 2), (2, 1), (3, 1)} <= universe, (
        "historical aired episodes must remain in the universe")


def test_retained_history_still_excludes_the_future(app, db, tmdb, tv):
    show_id = 995301
    tv.schedule(show_id, 1, 1, days_from_today(PAST_OFFSET))
    tv.schedule(show_id, 1, 2, days_from_today(TODAY_OFFSET))
    tv.schedule(show_id, 1, 3, days_from_today(FUTURE_OFFSET))
    tmdb.tv_show(show_id, {1: 3}, last_episode_to_air={
        "season_number": 1, "episode_number": 2,
        "air_date": days_from_today(PAST_OFFSET)})

    universe = aired(show_id)

    assert (1, 1) in universe and (1, 2) in universe
    assert (1, 3) not in universe, "a future episode is still not aired"


def test_upcoming_endpoints_expose_only_future_episodes(app, db, client, tmdb, tv):
    """Retaining aired rows must not change what "upcoming" means."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.schedule(show, 3, 1, days_from_today(PAST_OFFSET))       # retained history
    tv.schedule(show, 3, 2, days_from_today(3))                  # inside the 7-day window
    login(client, user)

    body = client.get("/api/tv/upcoming-episodes").get_json()
    positions_seen = {(e["season_number"], e["episode_number"])
                      for e in body.get("episodes", [])}

    assert (3, 1) not in positions_seen, (
        "a retained aired episode must not appear as upcoming")
    assert (3, 2) in positions_seen


def test_notification_fan_out_stays_idempotent_with_retention(
        app, db, client, tmdb, tv):
    """A wider scan window must not re-notify: the unique constraint, not the
    purge, is what makes the fan-out idempotent."""
    from api.notifications import notify_newly_aired_episodes

    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    tv.schedule(show, 1, 1, days_from_today(PAST_OFFSET))
    tv.schedule(show, 1, 2, days_from_today(PAST_OFFSET))

    first = notify_newly_aired_episodes()
    second = notify_newly_aired_episodes()

    assert first >= 1, "the fresh transition must still notify"
    assert second == 0, "a repeat run must create nothing new"


# ════════════════════════════════════════════════════════════════════════════
# The invariant, stated once: no forbidden state is reachable
# ════════════════════════════════════════════════════════════════════════════

def test_no_forbidden_state_after_a_full_mutation_sequence(app, db, client, tmdb, tv):
    """One pass over every reachable mutation, then the canonical model is
    re-read. Forbidden: a future/special watch row, Viewed below 100%,
    progress above 100%, stored counters above aired reality, or a card badge
    that disagrees with the canonical verdict."""
    user = tv.user()
    show = register(tmdb, tv, user, WRITE_SHOW, WRITE_SEASONS)
    tv.track(user, show)
    login(client, user)

    # every write the API exposes, including the ones that must be refused
    calls = [
        ("post", f"/api/tv/{show}/episode/1/1/mark-watched", {}),
        ("post", f"/api/tv/{show}/episode/1/1/update-watch", {"rating": 4}),
        ("post", f"/api/tv/{show}/episode/1/9/mark-watched", {}),      # future
        ("post", f"/api/tv/{show}/episode/0/1/mark-watched", {}),      # special
        ("post", f"/api/tv/{show}/episode/1/2/unmark-watched", {}),
        ("post", f"/api/tv/{show}/season/1/mark-watched", {}),
        ("post", f"/api/tv/{show}/season/1/unmark-watched", {}),
        ("post", f"/api/tv/{show}/mark-all-watched", {}),
        ("post", f"/mark_as_viewed/{show}/tv", {}),
        ("post", f"/remove_from_viewed/{show}/tv", {}),
    ]
    statuses = [getattr(client, verb)(url, json=body).status_code
                for verb, url, body in calls]
    assert statuses[2] == 400 and statuses[3] == 400, (
        f"the gate must refuse the ineligible writes: {statuses}")

    def assert_invariant(stage):
        universe = aired(show)
        ledger = watched_rows(user, show)
        canonical = progress(user, show)
        row = stored_row(user, show)

        assert ledger <= universe, (
            f"[{stage}] a watch row escaped the aired universe: "
            f"{ledger - universe}")
        assert all(season > 0 for season, _ in ledger), (
            f"[{stage}] a special was recorded")
        assert row.watched_episodes <= row.total_episodes, (
            f"[{stage}] stored counters above aired reality: "
            f"{row.watched_episodes}/{row.total_episodes}")
        if canonical is not None:
            assert canonical["watched"] <= canonical["aired"], (
                f"[{stage}] progress above 100%: {canonical}")
            assert canonical["percent"] <= 100.0, f"[{stage}] {canonical}"
            assert row.total_episodes == canonical["aired"], (
                f"[{stage}] stored denominator diverged from canonical: "
                f"{row.total_episodes} vs {canonical['aired']}")
            assert viewed(user, show) is (
                canonical["watched"] == canonical["aired"] > 0)
        else:
            assert ledger == set(), (
                f"[{stage}] rows exist but no canonical progress was produced")

        html = client.get("/viewed").get_data(as_text=True)
        assert _card_badges(html, show)[0] is viewed(user, show), (
            f"[{stage}] card badge disagrees with the canonical verdict")

    # The sequence ends with a full unmark, so the final state is the
    # zero-progress one; the populated state is checked just after Mark All.
    assert_invariant("final (after unmark)")
    client.post(f"/api/tv/{show}/mark-all-watched")
    assert_invariant("after mark-all")
