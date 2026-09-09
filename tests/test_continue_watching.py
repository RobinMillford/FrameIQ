"""Continue Watching — intent-based model tests.

The simplified contract: FrameIQ remembers WHAT the user started
(ContinueWatchingItem rows); the provider remembers WHERE they stopped.
No playback telemetry anywhere in the pipeline — the suite explicitly
verifies the feature works with zero player telemetry.

TMDb calls are mocked — no external services are required.
"""
import pytest
from datetime import datetime, date

from models import (ContinueWatchingItem, TVShowProgress, TVEpisodeWatch,
                    UpcomingEpisode, db)
import api.continue_watching as cw

SHOW_ID = 17287          # Party Down's TMDb id — the production bug case
SEASON1_EPISODES = 10    # S1 has 10 episodes; E11 does not exist

SHOW_DETAILS = {
    "id": SHOW_ID,
    "name": "Party Down",
    "poster_path": "/partydown.jpg",
    "number_of_seasons": 2,
    "number_of_episodes": 20,
    "seasons": [
        {"season_number": 1, "episode_count": SEASON1_EPISODES},
        {"season_number": 2, "episode_count": 10},
    ],
}

MOVIE_ID = 603
MOVIE_DETAILS = {
    "id": MOVIE_ID,
    "title": "The Matrix",
    "poster_path": "/matrix.jpg",
}


@pytest.fixture(autouse=True)
def _clean_cw_state(db, sample_user):
    """Clear CW rows + process memo around each test.

    DiaryEntry/MediaItem are purged unconditionally: rows created by the
    finish-flow tests (here) would otherwise break sample_user's ORM
    teardown (NOT NULL user_id on cascade-null) in the shared in-memory DB.
    """
    from models import DiaryEntry, MediaItem
    cw._memo.clear()
    yield
    DiaryEntry.query.delete(synchronize_session=False)
    MediaItem.query.delete(synchronize_session=False)
    ContinueWatchingItem.query.delete()
    TVEpisodeWatch.query.delete()
    TVShowProgress.query.delete()
    UpcomingEpisode.query.delete()
    db.session.commit()
    cw._memo.clear()


@pytest.fixture
def mock_tmdb(monkeypatch):
    """Mock every TMDb call site used by the flows under test:

    - api.tmdb_client.*            → call-time imports in api.continue_watching
    - routes.tv_tracking.fetch_tv_show_details → module-level binding used by
      mark_episode_watched_core, update_season_progress, and completion
      gating (all reached via cw.finish_tv_episode)

    Without the tv_tracking patch the tests would silently hit the real
    TMDb API when a valid key is present — the CI failure this fixes.
    """
    calls = {"movie": 0, "tv": 0}

    def fake_movie(mid, **kw):
        calls["movie"] += 1
        if mid == MOVIE_ID:
            return dict(MOVIE_DETAILS)
        return None

    def fake_show(sid, **kw):
        calls["tv"] += 1
        if sid == SHOW_ID:
            return dict(SHOW_DETAILS)
        return None

    monkeypatch.setattr("api.tmdb_client.fetch_movie_details", fake_movie)
    monkeypatch.setattr("api.tmdb_client.fetch_tv_show_details", fake_show)
    monkeypatch.setattr(
        "routes.tv_tracking.fetch_tv_show_details", fake_show)
    return calls


def _entries(user):
    return cw.continue_watching_entries(user.id)


# ── 1. Start flows ───────────────────────────────────────────────────────────

def test_start_movie_appears_in_continue_watching(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID,
                  title="The Matrix", poster_path="/matrix.jpg")
    entries = _entries(sample_user)
    assert len(entries) == 1
    e = entries[0]
    assert e["media_type"] == "movie"
    assert e["tmdb_id"] == MOVIE_ID
    assert e["watch_url"] == "/watch/movie/603"


def test_start_tv_episode_appears_with_exact_position(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4,
                  title="Party Down")
    entries = _entries(sample_user)
    assert len(entries) == 1
    e = entries[0]
    assert e["season"] == 2
    assert e["episode"] == 4
    assert e["watch_url"] == "/watch/tv/17287/2/4"
    assert "S2E4" in e["label"]


def test_duplicate_start_is_idempotent(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="The Matrix")
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="The Matrix")
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="The Matrix")
    rows = ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=MOVIE_ID).all()
    assert len(rows) == 1
    assert len(_entries(sample_user)) == 1


def test_duplicate_tv_start_is_idempotent(sample_user, mock_tmdb):
    for _ in range(3):
        cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    rows = ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=SHOW_ID).all()
    assert len(rows) == 1
    assert rows[0].season == 2 and rows[0].episode == 4


def test_distinct_episodes_are_distinct_items(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=5)
    rows = ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=SHOW_ID).all()
    assert len(rows) == 2


def test_recently_started_sorts_first(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="The Matrix")
    first = ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id).first()
    first.started_at = datetime(2020, 1, 1)
    db.session.commit()
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    entries = _entries(sample_user)
    assert entries[0]["media_type"] == "tv"   # newest first
    assert entries[1]["media_type"] == "movie"


# ── 2. Canonical metadata, never raw ids ─────────────────────────────────────

def test_canonical_movie_title_and_poster(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID)
    e = _entries(sample_user)[0]
    assert e["title"] == "The Matrix"
    assert e["poster"].endswith("/matrix.jpg")


def test_stored_hint_fills_when_tmdb_fails(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", 999999,
                  title="Stored Title", poster_path="/stored.jpg")
    e = _entries(sample_user)[0]
    assert e["title"] == "Stored Title"


def test_tv_title_from_tmdb_not_raw_id(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=3)
    e = _entries(sample_user)[0]
    assert e["title"] == "Party Down"
    assert "17287" not in e["title"]


def test_missing_metadata_card_omitted(sample_user):
    """No TMDb data AND no stored hint → card omitted, never 'Movie 603'."""
    cw.start_item(sample_user.id, "movie", 123456)
    assert _entries(sample_user) == []


# ── 3. TV episode validation — no invented episodes, ever ────────────────────

def test_invalid_episode_corrected_to_last_valid(sample_user, mock_tmdb):
    """Stale S1E11 (season ends at E10) is corrected to S1E10, not emitted."""
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=11)
    e = _entries(sample_user)[0]
    assert e["season"] == 1
    assert e["episode"] == SEASON1_EPISODES
    assert e["watch_url"] == f"/watch/tv/{SHOW_ID}/1/{SEASON1_EPISODES}"


def test_out_of_range_season_falls_back_to_first_valid(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=9, episode=1)
    e = _entries(sample_user)[0]
    assert (e["season"], e["episode"]) == (1, 1)


def test_show_omitted_when_no_tmdb_data(sample_user):
    """No season data → cannot validate → omitted, never a broken URL."""
    cw.start_item(sample_user.id, "tv", 424242, season=1, episode=1)
    assert _entries(sample_user) == []


def test_valid_episode_passes_through(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=5)
    e = _entries(sample_user)[0]
    assert (e["season"], e["episode"]) == (1, 5)


# ── 4. Movie finish flow (canonical watched state) ──────────────────────────

def test_movie_finish_removes_and_marks_watched(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID)
    result = cw.mark_movie_finished(sample_user.id, MOVIE_ID,
                                    title="The Matrix")
    assert result.get("success") is True
    # Removed from Continue Watching
    assert ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=MOVIE_ID).count() == 0
    # Canonical watched state recorded
    from models import DiaryEntry, MediaItem
    media = MediaItem.query.filter_by(tmdb_id=MOVIE_ID, media_type="movie").first()
    assert media is not None
    assert DiaryEntry.query.filter_by(
        media_id=media.id, media_type="movie").count() == 1


def test_movie_remove_hides_without_watched(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID)
    assert cw.remove_item(sample_user.id, "movie", MOVIE_ID) is True
    assert _entries(sample_user) == []
    from models import DiaryEntry, MediaItem
    assert DiaryEntry.query.filter_by(user_id=sample_user.id).count() == 0
    assert MediaItem.query.filter_by(tmdb_id=MOVIE_ID).count() == 0


# ── 5. TV finish flow: episode → watched → next episode promoted ────────────

def _air_all_s1(upto, show_id=SHOW_ID, season=1,
                day=date(2024, 1, 1)):
    """Sync UpcomingEpisode rows marking S1E1..upto as already aired."""
    for en in range(1, upto + 1):
        db.session.add(UpcomingEpisode(
            show_id=show_id, season_number=season, episode_number=en,
            show_name="Party Down", episode_name=f"S{season}E{en}",
            air_date=day))
    db.session.commit()


def test_tv_finish_marks_episode_and_promotes_next(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=4)
    _air_all_s1(upto=6)

    result = cw.finish_tv_episode(sample_user.id, SHOW_ID, 1, 4)
    assert result["finished"] is True
    assert result["next"]["season"] == 1
    assert result["next"]["episode"] == 5

    # Exact episode watched (canonical)
    assert TVEpisodeWatch.query.filter_by(
        user_id=sample_user.id, show_id=SHOW_ID,
        season_number=1, episode_number=4, is_rewatch=False).count() == 1
    # Finished episode leaves CW; next episode is the active item
    assert ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, season=1, episode=4).count() == 0
    nxt = ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, season=1, episode=5).first()
    assert nxt is not None
    assert nxt.title == "Party Down"


def test_tv_finish_final_episode_removes_show(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=10)

    result = cw.finish_tv_episode(sample_user.id, SHOW_ID, 2, 10)
    assert result["finished"] is True
    assert result["next"] is None
    assert ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=SHOW_ID).count() == 0


def test_next_episode_never_invented_beyond_season(sample_user, mock_tmdb):
    """S1 has 10 episodes; finishing E10 with E2-10 unaired... next is E1 of
    S2 only when valid — never S1E11."""
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=9)
    _air_all_s1(upto=10)

    result = cw.finish_tv_episode(sample_user.id, SHOW_ID, 1, 9)
    assert result["next"]["season"] == 1
    assert result["next"]["episode"] == 10


def test_future_episode_not_promoted(sample_user, mock_tmdb):
    """Next episode exists but hasn't aired → it is never promoted; the show
    leaves Continue Watching (no future episode becomes the active item)."""
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=9)
    # S1E10 is the next episode but airs in the future
    db.session.add(UpcomingEpisode(
        show_id=SHOW_ID, season_number=1, episode_number=10,
        show_name="Party Down", episode_name="Finale",
        air_date=date(2099, 1, 1)))
    db.session.commit()

    result = cw.finish_tv_episode(sample_user.id, SHOW_ID, 1, 9)
    assert result["finished"] is True
    assert result["next"] is None
    # No unaired episode was started
    assert ContinueWatchingItem.query.filter_by(
        user_id=sample_user.id, tmdb_id=SHOW_ID, season=1,
        episode=10).count() == 0


def test_tv_remove_hides_without_watched(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    assert cw.remove_item(sample_user.id, "tv", SHOW_ID,
                          season=2, episode=4) is True
    assert _entries(sample_user) == []
    assert TVEpisodeWatch.query.filter_by(user_id=sample_user.id).count() == 0


# ── 6. Zero-telemetry requirements ───────────────────────────────────────────

def test_works_with_zero_player_telemetry(sample_user, mock_tmdb):
    """No postMessage, no currentTime/duration/progress — CW is fully
    functional from intent rows alone."""
    cw.start_item(sample_user.id, "movie", MOVIE_ID)
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    entries = _entries(sample_user)
    assert len(entries) == 2
    for e in entries:
        assert "progress" not in e          # no fake percentages
        assert "percent" not in e
        assert e["title"]                   # canonical title always present


def test_no_polling_or_timer_machinery_in_source():
    """Guard: the CW module must not contain timers/polling/telemetry."""
    import inspect
    src = inspect.getsource(cw)
    for banned in ("setInterval", "setTimeout", "postMessage", "currentTime",
                   "duration", "while True"):
        assert banned not in src, f"banned telemetry/loop token: {banned}"


# ── 7. Homepage integration ──────────────────────────────────────────────────

def test_homepage_ok_when_tmdb_unreachable(auth_client, sample_user, mock_tmdb):
    """TMDb failure must not 500 the homepage; rows without hints are omitted."""
    cw.start_item(sample_user.id, "movie", 987654)   # mock returns None
    cw.start_item(sample_user.id, "movie", MOVIE_ID,
                  title="The Matrix", poster_path="/matrix.jpg")
    r = auth_client.get('/')
    assert r.status_code == 200
    assert b"The Matrix" in r.data


def test_homepage_ok_when_resolver_raises(auth_client, sample_user, monkeypatch):
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="X")

    def boom(mid, **kw):
        raise RuntimeError("tmdb down")
    monkeypatch.setattr("api.tmdb_client.fetch_movie_details", boom)
    r = auth_client.get('/')
    assert r.status_code == 200


def test_mixed_movie_and_tv_coexist(sample_user, mock_tmdb):
    cw.start_item(sample_user.id, "movie", MOVIE_ID, title="The Matrix")
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=2, episode=4)
    entries = _entries(sample_user)
    types = {e["media_type"] for e in entries}
    assert types == {"movie", "tv"}


def test_deduplicated_details_fetches(sample_user, mock_tmdb):
    """Same show twice → one TMDb fetch (no N+1 per card)."""
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=1)
    cw.start_item(sample_user.id, "tv", SHOW_ID, season=1, episode=2)
    _entries(sample_user)
    assert mock_tmdb["tv"] == 1


def test_stale_hint_corrected_by_canonical_tmdb(sample_user, mock_tmdb):
    """Stored hints may be stale — canonical TMDb metadata always wins."""
    cw.start_item(sample_user.id, "movie", MOVIE_ID,
                  title="Stale Title", poster_path="/stale.jpg")
    e = _entries(sample_user)[0]
    assert e["title"] == "The Matrix"
    assert e["poster"].endswith("/matrix.jpg")
    assert mock_tmdb["movie"] == 1   # resolved exactly once (deduped)
