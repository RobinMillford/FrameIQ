"""Continue Watching correctness — canonical metadata + validated episodes.

Regression coverage for the production bugs where the home Continue Watching
rail rendered raw TMDb ids ("Show 17287") and emitted nonexistent TV episodes
(S1E11 when the season ends at E10).

TMDb calls are mocked — no external services are required.
"""
import pytest
from datetime import date

from models import TVShowProgress, TVEpisodeWatch, WatchProgress, UpcomingEpisode, db
import api.continue_watching as cw

SHOW_ID = 17287          # Party Down's TMDb id — the production bug case
SEASON1_EPISODES = 10    # S1 has 10 episodes; E11 does not exist

SHOW_DETAILS = {
    "id": SHOW_ID,
    "name": "Party Down",
    "poster_path": "/partydown.jpg",
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
    """Clear rows + process memo around each test.

    DiaryEntry/MediaItem are purged unconditionally: rows created by the
    completion cross-check tests (here) or auto-log tests (test_watch.py)
    would otherwise break sample_user's ORM teardown (NOT NULL user_id on
    cascade-null) in the shared in-memory database.
    """
    from models import DiaryEntry, MediaItem
    cw._memo.clear()
    yield
    DiaryEntry.query.delete(synchronize_session=False)
    MediaItem.query.delete(synchronize_session=False)
    WatchProgress.query.delete()
    TVEpisodeWatch.query.delete()
    TVShowProgress.query.delete()
    UpcomingEpisode.query.delete()
    db.session.commit()
    cw._memo.clear()


@pytest.fixture
def mock_tmdb(monkeypatch):
    """Mock both call sites: routes.tv_tracking (module binding) and
    api.continue_watching (call-time import from api.tmdb_client)."""
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
    return calls


def _entries(user):
    return cw.continue_watching_entries(user.id)


# ── 1. Movie: canonical title/poster, never a raw id ─────────────────────────

def test_movie_canonical_title_and_poster(sample_user, mock_tmdb):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=MOVIE_ID, media_type="movie",
        current_time=600, duration=1800))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert e["media_type"] == "movie"
    assert e["title"] == "The Matrix"
    assert e["poster"] == "https://image.tmdb.org/t/p/w500/matrix.jpg"
    assert str(MOVIE_ID) not in e["title"]
    assert e["watch_url"] == f"/watch/movie/{MOVIE_ID}"
    assert e["progress"] == 33.3


def test_movie_stored_metadata_still_fills_when_tmdb_fails(sample_user, mock_tmdb):
    """Stored title/poster are honored when TMDb cannot resolve (graceful)."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=999999, media_type="movie",
        current_time=100, duration=1000, title="Stored Title",
        poster_path="/stored.jpg"))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert e["title"] == "Stored Title"
    assert e["poster"] == "https://image.tmdb.org/t/p/w500/stored.jpg"
    assert e["watch_url"] == "/watch/movie/999999"


# ── 2. TV: valid episode → canonical card with valid URL ────────────────────

def test_tv_valid_episode_canonical(sample_user, mock_tmdb):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=5, current_time=600, duration=1800,
        title="Old Title", poster_path="/old.jpg"))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert e["title"] == "Party Down"           # canonical wins over stored
    assert e["poster"] == "https://image.tmdb.org/t/p/w500/partydown.jpg"
    assert e["season"] == 1 and e["episode"] == 5
    assert e["watch_url"] == f"/watch/tv/{SHOW_ID}/1/5?type=tv"
    assert "S1E5" in e["label"]


# ── 3. TV: invalid episode → corrected or omitted, never broken ─────────────

def test_tv_invalid_episode_corrected_to_last_valid(sample_user, mock_tmdb):
    """S1E11 does not exist (season ends at E10) — corrected to S1E10."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=11, current_time=600, duration=1800))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert (e["season"], e["episode"]) == (1, SEASON1_EPISODES)
    assert e["watch_url"] == f"/watch/tv/{SHOW_ID}/1/{SEASON1_EPISODES}?type=tv"
    assert "S1E11" not in e["label"]


def test_tv_missing_season_falls_back_to_first_valid(sample_user, mock_tmdb):
    """Stored season doesn't exist at all → first valid episode, not invented."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=9, episode=3, current_time=600, duration=1800))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert (e["season"], e["episode"]) == (1, 1)
    assert e["watch_url"] == f"/watch/tv/{SHOW_ID}/1/1?type=tv"


def test_tv_omitted_when_no_tmdb_data(sample_user, mock_tmdb):
    """No authoritative season data → card omitted, no broken URL emitted."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=424242, media_type="tv",
        season=1, episode=2, current_time=600, duration=1800))
    db.session.commit()
    assert _entries(sample_user) == []


# ── 4. TV: missing stored title/poster are hydrated ──────────────────────────

def test_tv_missing_metadata_hydrated(sample_user, mock_tmdb):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=3, current_time=600, duration=1800,
        title=None, poster_path=None))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert e["title"] == "Party Down"
    assert e["title"] != f"Show {SHOW_ID}"
    assert str(SHOW_ID) not in e["title"]
    assert e["poster"] == "https://image.tmdb.org/t/p/w500/partydown.jpg"


# ── 5. TV: stale stored title/poster → canonical TMDb wins ──────────────────

def test_tv_stale_metadata_corrected(sample_user, mock_tmdb):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=2, current_time=600, duration=1800,
        title="Wrong Old Name", poster_path="/stale.jpg"))
    db.session.commit()
    e = _entries(sample_user)[0]
    assert e["title"] == "Party Down"
    assert e["poster"] == "https://image.tmdb.org/t/p/w500/partydown.jpg"


# ── 6. Homepage never 500s; degradation is graceful ──────────────────────────

def test_homepage_ok_when_tmdb_unreachable(auth_client, sample_user, mock_tmdb):
    mock_tmdb  # mocks in place — every id resolves to None
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=999, media_type="tv",
        season=1, episode=2, current_time=600, duration=1800))
    db.session.commit()
    r = auth_client.get("/")
    assert r.status_code == 200
    assert b"Show 999" not in r.data
    assert b"999" not in r.data.split(b"Continue Watching")[1].split(b"</section>")[0] \
        if b"Continue Watching" in r.data else True


def test_homepage_ok_when_resolver_raises(auth_client, sample_user, monkeypatch):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=MOVIE_ID, media_type="movie",
        current_time=600, duration=1800))
    db.session.commit()
    monkeypatch.setattr(
        cw, "continue_watching_entries",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = auth_client.get("/")
    assert r.status_code == 200


# ── 7. Mixed movie + TV coexist ──────────────────────────────────────────────

def test_mixed_movie_and_tv_coexist(sample_user, mock_tmdb):
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=MOVIE_ID, media_type="movie",
        current_time=600, duration=1800))
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=4, current_time=300, duration=1800))
    db.session.commit()
    entries = _entries(sample_user)
    by_type = {e["media_type"]: e for e in entries}
    assert by_type["movie"]["title"] == "The Matrix"
    assert by_type["tv"]["title"] == "Party Down"
    assert by_type["tv"]["episode"] == 4


# ── 8. Bounded TMDb work (no per-card request storm) ─────────────────────────

def test_deduplicated_details_fetches(sample_user, mock_tmdb):
    """Multiple cards for the same id → ONE details fetch per id per render."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=3, current_time=300, duration=1800))
    db.session.add(TVShowProgress(
        user_id=sample_user.id, show_id=SHOW_ID, status="watching",
        watched_episodes=2, total_episodes=20))
    db.session.commit()
    entries = _entries(sample_user)
    assert len(entries) >= 1
    assert mock_tmdb["tv"] == 1  # memoized after the first lookup


def test_movie_metadata_not_fetched_when_stored_complete(sample_user, mock_tmdb):
    """Movies with complete stored metadata need no TMDb call at all."""
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=777, media_type="movie",
        current_time=600, duration=1800, title="Known", poster_path="/k.jpg"))
    db.session.commit()
    _entries(sample_user)
    assert mock_tmdb["movie"] == 0


# ── 9. Unfinished-show path: validated next episode ──────────────────────────

def test_unfinished_show_next_episode_validated(sample_user, mock_tmdb):
    """All of S1 watched (its real length) → next is S2E1, never S1E11."""
    db.session.add(TVShowProgress(
        user_id=sample_user.id, show_id=SHOW_ID, status="watching",
        watched_episodes=SEASON1_EPISODES, total_episodes=20, total_seasons=2))
    for ep in range(1, SEASON1_EPISODES + 1):
        db.session.add(TVEpisodeWatch(
            user_id=sample_user.id, show_id=SHOW_ID,
            season_number=1, episode_number=ep))
    db.session.commit()
    e = [x for x in _entries(sample_user) if x["media_type"] == "tv"][0]
    assert (e["season"], e["episode"]) == (2, 1)
    assert e["watch_url"] == f"/watch/tv/{SHOW_ID}/2/1"
    assert "S1E11" not in e["label"]


def test_unfinished_show_title_from_tmdb_not_raw_id(sample_user, mock_tmdb):
    db.session.add(TVShowProgress(
        user_id=sample_user.id, show_id=SHOW_ID, status="watching",
        watched_episodes=1, total_episodes=20, total_seasons=2))
    db.session.commit()
    e = [x for x in _entries(sample_user) if x["media_type"] == "tv"][0]
    assert e["title"] == "Party Down"
    assert e["title"] != f"Show {SHOW_ID}"


def test_unfinished_show_omitted_without_tmdb_data(sample_user, mock_tmdb):
    """No TMDb data for a tracked show → omitted from home rail (no raw id,
    no invented episode)."""
    db.session.add(TVShowProgress(
        user_id=sample_user.id, show_id=88888, status="watching",
        watched_episodes=0, total_episodes=0, total_seasons=0))
    db.session.commit()
    assert _entries(sample_user) == []


# ── 10. Completion cross-check: real completed state wins over stale rows ────

def test_completed_movie_with_stale_progress_hidden(sample_user, mock_tmdb):
    """A stale WatchProgress row (older than the diary completion event) for a
    movie the user actually finished must not reappear on Continue Watching."""
    from models import MediaItem, DiaryEntry
    from datetime import datetime, timedelta
    media = MediaItem(tmdb_id=MOVIE_ID, media_type="movie",
                      title="The Matrix")
    db.session.add(media)
    db.session.flush()
    db.session.add(DiaryEntry(
        user_id=sample_user.id, media_id=media.id,
        media_type="movie", watched_date=date.today(), is_rewatch=False))
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=MOVIE_ID, media_type="movie",
        current_time=300, duration=1800,   # only 17% — below the 0.9 filter
        updated_at=datetime.utcnow() - timedelta(days=1)))  # predates completion
    db.session.commit()
    assert _entries(sample_user) == []


def test_completed_episode_with_stale_progress_hidden(sample_user, mock_tmdb):
    """A resume row OLDER than the episode's watched event is stale and must
    not reappear on Continue Watching."""
    from datetime import datetime, timedelta
    db.session.add(TVEpisodeWatch(
        user_id=sample_user.id, show_id=SHOW_ID,
        season_number=1, episode_number=5))
    db.session.commit()
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=5, current_time=300, duration=1800,
        updated_at=datetime.utcnow() - timedelta(days=1)))
    db.session.commit()
    assert _entries(sample_user) == []


def test_rewatch_in_progress_newer_than_completion_kept(sample_user, mock_tmdb):
    """A resume row NEWER than the watched event is a genuine rewatch in
    progress and must be kept (S1E5 partially re-watched after completion)."""
    from datetime import datetime, timedelta
    db.session.add(TVEpisodeWatch(
        user_id=sample_user.id, show_id=SHOW_ID,
        season_number=1, episode_number=5))
    db.session.commit()
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=5, current_time=300, duration=1800,
        updated_at=datetime.utcnow() + timedelta(minutes=5)))
    db.session.commit()
    entries = _entries(sample_user)
    assert len(entries) == 1
    assert (entries[0]["season"], entries[0]["episode"]) == (1, 5)


def test_corrected_invalid_episode_mapped_to_watched_hidden(sample_user, mock_tmdb):
    """S1E11 (invalid) corrects to S1E10 — but the user already watched
    S1E10 and the stale row predates that, so the card is omitted."""
    from datetime import datetime, timedelta
    db.session.add(TVEpisodeWatch(
        user_id=sample_user.id, show_id=SHOW_ID,
        season_number=1, episode_number=SEASON1_EPISODES))
    db.session.commit()
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=11, current_time=300, duration=1800,
        updated_at=datetime.utcnow() - timedelta(days=1)))
    db.session.commit()
    assert _entries(sample_user) == []


def test_unwatched_sibling_episode_still_appears(sample_user, mock_tmdb):
    """Completion is per-episode: hiding S1E5 must not hide S1E6."""
    db.session.add(TVEpisodeWatch(
        user_id=sample_user.id, show_id=SHOW_ID,
        season_number=1, episode_number=5))
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type="tv",
        season=1, episode=6, current_time=300, duration=1800))
    db.session.commit()
    entries = _entries(sample_user)
    assert len(entries) == 1
    assert (entries[0]["season"], entries[0]["episode"]) == (1, 6)
