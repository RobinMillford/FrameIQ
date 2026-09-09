"""
Production bug-fix regression tests.

Bug 1 — /movies and /tv_shows category posters never loaded:
    setupMobileMenu() in movies-page.js / tv-shows.js threw a TypeError on the
    missing #mobile-menu-button element, aborting the DOMContentLoaded handler
    BEFORE fetchMoviesByGenres()/fetchTVShowsByGenres() ran, so category
    skeletons were never replaced. These tests pin the page contract the fixed
    JS depends on: genre containers exist and the page script is included.

Bug 2 — clicking Watch/Stream returned raw JSON "This page is temporarily
    busy" (429) on browser navigation. The guard now returns a self-healing
    HTML page to text/html navigations while API/fetch clients keep the exact
    JSON contract. The concurrency limit itself is unchanged.
"""
import pytest

import utils.request_guard as request_guard


GENRE_CONTAINERS = ["action", "comedy", "drama", "romance", "thriller", "horror"]
GENRE_CONTAINERS_TV = ["action", "comedy", "drama"]


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1 — poster pipeline page contract
# ─────────────────────────────────────────────────────────────────────────────

class TestCategoryPagesPosterContract:
    def test_movies_page_renders_genre_containers(self, client):
        r = client.get("/movies")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        for genre in GENRE_CONTAINERS:
            assert f'id="{genre}-movies"' in html, f"missing #{genre}-movies container"

    def test_tv_shows_page_renders_genre_containers(self, client):
        r = client.get("/tv_shows")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        for genre in GENRE_CONTAINERS_TV:
            assert f'id="{genre}-tv"' in html, f"missing #{genre}-tv container"

    def test_movies_page_includes_page_script(self, client):
        r = client.get("/movies")
        html = r.get_data(as_text=True)
        assert "movies-page.js" in html

    def test_tv_shows_page_includes_page_script(self, client):
        r = client.get("/tv_shows")
        html = r.get_data(as_text=True)
        assert "tv-shows.js" in html

    def test_fixed_js_no_longer_throws_on_missing_mobile_menu(self):
        """The exact defect: getElementById('mobile-menu-button') result was
        used unconditionally. The guard must exist in both page scripts."""
        for path in ("static/js/movies-page.js", "static/js/tv-shows.js"):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            assert "if (!mobileMenuButton) return;" in src, path


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2 — watch navigation & busy response behavior
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def hold_expensive_slot():
    """Hold the guard's single slot to simulate genuine contention."""
    request_guard._EXPENSIVE_PAGE_SLOTS.acquire()
    try:
        yield
    finally:
        request_guard._EXPENSIVE_PAGE_SLOTS.release()


class TestWatchNavigation:
    def _patch_tmdb_movie(self, monkeypatch):
        import api.tmdb_client as tmdb_client
        monkeypatch.setattr(
            tmdb_client, "fetch_movie_details",
            lambda _id: {"id": _id, "title": "T", "poster_path": None,
                         "overview": "", "release_date": "", "genres": [],
                         "vote_average": 0, "recommendations": []},
        )

    def _patch_tmdb_tv(self, monkeypatch):
        import api.tmdb_client as tmdb_client
        monkeypatch.setattr(
            tmdb_client, "fetch_tv_show_details",
            lambda _id: {"id": _id, "name": "S", "seasons": [], "poster_path": None,
                         "overview": "", "status": "", "number_of_seasons": 1,
                         "vote_average": 0},
        )

    def test_watch_movie_first_request_renders(self, client, monkeypatch):
        """Direct watch URL must render on the very first request."""
        self._patch_tmdb_movie(monkeypatch)
        r = client.get("/watch/movie/603")
        assert r.status_code == 200
        assert b"watch" in r.data.lower() or b"player" in r.data.lower()

    def test_watch_tv_first_request_renders(self, client, monkeypatch):
        self._patch_tmdb_tv(monkeypatch)
        r = client.get("/watch/tv/1399/1/1")
        assert r.status_code == 200

    def test_busy_browser_navigation_gets_html_not_json(self, client, hold_expensive_slot):
        """Browser navigation (Accept: text/html) must NOT receive raw JSON."""
        r = client.get("/watch/movie/603", headers={"Accept": "text/html"})
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "5"
        assert b"This page is temporarily busy" not in r.data  # no raw JSON body
        assert b"location.reload" in r.data  # self-healing retry page

    def test_busy_api_client_keeps_json_contract(self, client, hold_expensive_slot):
        """Fetch/XHR clients keep the exact JSON error contract."""
        r = client.get("/watch/movie/603", headers={"Accept": "application/json"})
        assert r.status_code == 429
        assert r.get_json() == {
            "error": "This page is temporarily busy. Please try again."
        }

    def test_guard_releases_slot_after_success(self, client, monkeypatch):
        """After a successful request the slot is free again (no leak)."""
        self._patch_tmdb_movie(monkeypatch)
        assert client.get("/watch/movie/603").status_code == 200
        # Slot must be free — acquiring twice would raise BoundedSemaphore error.
        request_guard._EXPENSIVE_PAGE_SLOTS.acquire()
        request_guard._EXPENSIVE_PAGE_SLOTS.release()
