"""Task F3 — the suite is offline by design, and provably so.

These tests lock the F3 testing contract itself (spec §4, §6, §17):

  * ordinary tests need NO TMDb key, NO DNS and NO internet;
  * an accidental outbound connection FAILS LOUDLY (it can never silently
    reach api.themoviedb.org);
  * TMDb payloads come from deterministic fixtures, while every real
    FrameIQ layer above the HTTP boundary still runs (cache, fetchers,
    parsers, resolvers);
  * a genuine external integration test is a separate, opt-in layer
    (``@pytest.mark.tmdb``, ``pytest -m tmdb``).
"""
import os
import socket

import pytest

from api.tmdb import cache as tmdb_cache_mod
from api.tmdb_client import fetch_tv_show_details
from tests.tmdb_offline import (
    OfflineNetworkViolation, images, movie, release_dates, season_details,
    tv_list, tv_show, upstream_failure, watch_providers,
)

SHOW_ID = 994901
MOVIE_ID = 994902


# ── the fixtures themselves are realistic TMDb shapes ───────────────────────

def test_tv_show_fixture_carries_the_fields_the_app_consumes():
    payload = tv_show(SHOW_ID, {1: 10, 2: 10, 4: 8})
    assert payload["number_of_seasons"] == 3
    assert payload["number_of_episodes"] == 28
    assert payload["last_episode_to_air"]["season_number"] == 4
    assert [s["season_number"] for s in payload["seasons"]] == [1, 2, 4]
    assert payload["credits"]["crew"][0]["job"] == "Creator"


def test_movie_fixture_and_optional_payloads_are_available_offline():
    assert movie(MOVIE_ID)["runtime"] == 120
    assert release_dates("US", "GB")["results"][1]["iso_3166_1"] == "GB"
    assert watch_providers(True)["results"]["US"]["flatrate"][0][
        "provider_name"] == "Netflix"
    assert images()["posters"][0]["file_path"] == "/poster.jpg"
    assert season_details(SHOW_ID, 1, episode_count=3)["episodes"][2][
        "episode_number"] == 3
    assert tv_list(count=2)["results"][1]["name"] == "List Show 2"


# ── deterministic TMDb traffic through the REAL application layers ──────────

def test_tmdb_payload_is_served_offline_through_the_real_fetcher(tmdb):
    """No key, no network — yet ``fetch_tv_show_details`` parses for real."""
    tmdb.tv_show(SHOW_ID, {1: 10, 2: 10, 3: 10, 4: 8}, name="Offline Show")

    show = fetch_tv_show_details(SHOW_ID)

    assert show["name"] == "Offline Show"
    assert show["number_of_episodes"] == 38
    assert show["creator"] == "Fixture Creator"
    assert show["poster_path"].startswith("https://image.tmdb.org/t/p/w500")
    assert [(s["season_number"], s["episode_count"])
            for s in show["seasons"]] == [(1, 10), (2, 10), (3, 10), (4, 8)]
    assert tmdb.count(rf"^/3/tv/{SHOW_ID}$") == 1


def test_tmdb_key_is_a_throwaway_placeholder():
    """The suite must not require (or leak) a real credential."""
    key = os.environ.get("TMDB_API_KEY", "")
    assert key == "test-tmdb-key", key


def test_unregistered_tmdb_endpoint_degrades_like_an_unusable_key(tmdb):
    """An un-fixtured TMDb endpoint is the app's documented upstream failure
    — never a live request, and never a silently invented payload."""
    with pytest.raises(LookupError):
        fetch_tv_show_details(999999999)
    assert upstream_failure()["success"] is False
    assert tmdb.count(r"^/3/tv/999999999$") == 1
    assert tmdb.blocked == []


def test_registry_records_calls_so_network_budgets_can_be_asserted(tmdb):
    tmdb.tv_show(SHOW_ID, {1: 10})
    fetch_tv_show_details(SHOW_ID)
    fetch_tv_show_details(SHOW_ID)          # second call: served by the cache

    assert tmdb.paths() == [f"/3/tv/{SHOW_ID}"]      # ONE upstream request
    # ... and the real cache layer is what absorbed the second call.
    assert tmdb_cache_mod.get_cache_key(tmdb.calls[0]) in \
        tmdb_cache_mod.tmdb_cache


# ── the network guard ──────────────────────────────────────────────────────

def test_raw_socket_connection_is_blocked():
    with pytest.raises(OfflineNetworkViolation) as excinfo:
        sock = socket.socket()
        try:
            sock.connect(("127.0.0.1", 9))
        finally:
            sock.close()

    message = str(excinfo.value)
    assert "network access" in message.lower()
    assert "127.0.0.1" in message


def test_non_tmdb_http_request_is_blocked_loudly():
    """Anything that is not a registered TMDb endpoint fails, with an
    actionable message naming the offending URL."""
    import requests

    with pytest.raises(OfflineNetworkViolation) as excinfo:
        requests.get("https://example.com/some/feed.xml", timeout=1)

    message = str(excinfo.value)
    assert "https://example.com/some/feed.xml" in message
    assert "offline" in message.lower()
    assert "tmdb_offline.py" in message


def test_dns_resolution_never_leaves_the_process():
    """getaddrinfo is part of the guard: no name lookup can escape either."""
    with pytest.raises(OfflineNetworkViolation):
        socket.getaddrinfo("api.themoviedb.org", 443)


# ── the opt-in integration layer ───────────────────────────────────────────

@pytest.mark.tmdb
def test_real_tmdb_integration_is_opt_in():
    """The ONLY layer allowed to touch real TMDb (``pytest -m tmdb``).

    Deselected from the default offline suite via the pytest configuration;
    needs a real ``TMDB_API_KEY`` in the environment.
    """
    import requests

    key = os.environ.get("TMDB_API_KEY", "")
    if key in ("", "test", "test-tmdb-key"):
        pytest.skip("real TMDB_API_KEY not configured — opt-in run only")

    response = requests.get(
        "https://api.themoviedb.org/3/configuration",
        params={"api_key": key}, timeout=10)
    assert response.status_code == 200
