from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "tv_detail.html"
SCRIPT = ROOT / "static" / "js" / "tv-seasons.js"


def test_tv_seasons_data_is_embedded_for_the_seasons_manager():
    template = TEMPLATE.read_text()

    assert 'id="tv-seasons-data"' in template
    assert "{{ show.seasons | tojson }}" in template


def test_tv_seasons_manager_uses_embedded_data_without_tmdb_proxy_request():
    script = SCRIPT.read_text()

    assert "tv-seasons-data" in script
    assert "/api/tmdb/proxy" not in script
    assert "JSON.parse(seasonsData.textContent || '[]')" in script


def test_tv_seasons_initialization_does_not_request_progress():
    script = SCRIPT.read_text()
    initialize = script.split("async initialize()", 1)[1].split("async loadShowDetails()", 1)[0]

    assert "loadWatchedEpisodes()" in initialize
    assert "loadShowProgress()" not in initialize
