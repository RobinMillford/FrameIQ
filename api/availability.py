"""Where-to-Watch availability — TMDb watch providers, normalized.

Fetches watch/providers via the existing cached_tmdb_request pipeline (bounded
TTL cache + single-flight + retries), normalizes TMDb's provider groups into a
stable internal shape, and matches providers against a user's My Services.

Normalized shape (never exposes raw TMDb payloads to the UI):
    {
        "region": "US",
        "stream":  [{"id": 8, "name": "Netflix", "logo": "/pT...jpg", "priority": 8}],
        "free":    [...],
        "rent":    [...],
        "buy":     [...],
        "link":    "https://www.themoviedb.org/movie/603/watch?locale=US",
    }

Provider availability changes slowly; results are cached at the TMDb layer and
additionally memoized per-process so a burst of detail-page renders never
multiplies upstream calls.
"""
import logging
import threading
import time

from api.tmdb.cache import cached_tmdb_request, get_cache_key

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"

DEFAULT_REGION = "US"
_VALID_REGION = {"US", "GB", "CA", "AU", "DE", "FR", "IN", "BD", "BR", "JP", "NL"}

# How the UI labels each TMDb provider group.
CATEGORY_LABELS = {
    "stream": "Stream",
    "free": "Free",
    "rent": "Rent",
    "buy": "Buy",
}

# Providers change far less often than list data. 24h bounded per-process
# memoization on top of the TMDb HTTP cache (max_age=6h).
_PROVIDER_MEMO_TTL = 24 * 3600
_PROVIDER_MEMO_MAX = 2000
_provider_memo = {}
_memo_lock = threading.Lock()


def normalize_region(raw):
    """Return a safe uppercase region code, or the app default."""
    if not raw:
        return DEFAULT_REGION
    code = str(raw).strip().upper()[:2]
    if not (len(code) == 2 and code.isalpha()):
        return DEFAULT_REGION
    return code


def _provider_entry(provider):
    """Shape a TMDb provider object; None if it lacks the minimum identity."""
    if not isinstance(provider, dict):
        return None
    pid = provider.get("provider_id")
    name = provider.get("provider_name")
    if pid is None or not name:
        return None
    return {
        "id": int(pid),
        "name": str(name),
        "logo": provider.get("logo_path") or None,
        "priority": provider.get("display_priority", 999),
    }


def _normalize_groups(raw_providers, region):
    """TMDb `results.<REGION>` dict -> normalized availability dict."""
    entry = {
        "region": region,
        "stream": [],
        "free": [],
        "rent": [],
        "buy": [],
        "link": None,
    }
    if not isinstance(raw_providers, dict):
        return entry

    entry["link"] = raw_providers.get("link") or None
    group_map = {
        "flatrate": "stream",
        "free": "free",
        "rent": "rent",
        "buy": "buy",
    }
    for tmdb_group, our_group in group_map.items():
        items = raw_providers.get(tmdb_group) or []
        if not isinstance(items, list):
            continue
        seen_ids = set()
        for raw in items:
            provider = _provider_entry(raw)
            if provider is None or provider["id"] in seen_ids:
                continue
            seen_ids.add(provider["id"])
            entry[our_group].append(provider)
        entry[our_group].sort(key=lambda p: p["priority"])
    return entry


def _empty_availability(region):
    return {
        "region": region,
        "stream": [], "free": [], "rent": [], "buy": [],
        "link": None,
        "available": False,
    }


def _fetch_provider_results(media_type, tmdb_id):
    """Raw TMDb `results` dict for a title, memoized per (type, id).

    One upstream response contains every region, so a single fetch serves all
    regions for that title. Returns {} when TMDb fails or has no data.
    """
    memo_key = (media_type, tmdb_id)
    now = time.time()
    with _memo_lock:
        hit = _provider_memo.get(memo_key)
        if hit and now - hit[1] < _PROVIDER_MEMO_TTL:
            return hit[0]

    path = f"/{media_type}/{tmdb_id}/watch/providers"
    # cached_tmdb_request keys on the full URL string, so build it the same way
    # as the rest of the client (key inline).
    from api.tmdb.config import TMDB_API_KEY
    url = f"{TMDB_BASE}{path}?api_key={TMDB_API_KEY}&language=en-US"
    try:
        data = cached_tmdb_request(url, max_age=6 * 3600)
        results = (data or {}).get("results", {}) or {}
    except Exception:
        logger.warning("Provider fetch failed for %s/%s", media_type, tmdb_id)
        results = {}

    with _memo_lock:
        if len(_provider_memo) >= _PROVIDER_MEMO_MAX:
            oldest = sorted(_provider_memo.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[:max(1, _PROVIDER_MEMO_MAX // 10)]:
                del _provider_memo[k]
        _provider_memo[memo_key] = (results, now)
    return results


def get_availability(media_type, tmdb_id, region=None):
    """Normalized where-to-watch availability for a movie or TV show.

    media_type: 'movie' | 'tv'. Returns a normalized dict; on TMDb failure or
    missing data returns the safe empty shape (never raises, never blocks the
    page on repeated external calls).
    """
    media_type = "tv" if str(media_type) == "tv" else "movie"
    tmdb_id = int(tmdb_id)
    region = normalize_region(region)

    results = _fetch_provider_results(media_type, tmdb_id)
    raw = results.get(region)

    if raw is None or raw == {}:
        availability = _empty_availability(region)
    else:
        availability = _normalize_groups(raw, region)
        availability["available"] = bool(
            availability["stream"] or availability["free"]
            or availability["rent"] or availability["buy"]
        )

    return availability


def match_my_services(availability, selected_provider_ids):
    """Cross-reference normalized availability with the user's services.

    Streaming/free providers count as "on your services"; rent/buy never do.
    Returns {"matches": [provider dicts], "available": bool}.
    """
    selected = set()
    for pid in selected_provider_ids or []:
        try:
            selected.add(int(pid))
        except (TypeError, ValueError):
            continue
    candidates = list(availability.get("stream", [])) + list(availability.get("free", []))
    matches = [p for p in candidates if p["id"] in selected]
    return {"matches": matches, "available": bool(matches)}


def all_region_providers(region):
    """Distinct providers available in a region (any category), for the
    My Services settings picker. Aggregates provider ids seen across TMDb is
    not possible cheaply; instead the picker is seeded from TMDb's static
    provider list via /watch/providers/tv (shared by movies and TV).
    """
    region = normalize_region(region)
    memo_key = ("_all_providers", region, "")
    now = time.time()
    with _memo_lock:
        hit = _provider_memo.get(memo_key)
        if hit and now - hit[1] < _PROVIDER_MEMO_TTL:
            return hit[0]

    from api.tmdb.config import TMDB_API_KEY
    url = (
        f"{TMDB_BASE}/watch/providers/tv"
        f"?api_key={TMDB_API_KEY}&watch_region={region}&language=en-US"
    )
    try:
        data = cached_tmdb_request(url, max_age=24 * 3600)
        results = (data or {}).get("results", []) or []
    except Exception:
        logger.warning("Provider list fetch failed for region %s", region)
        return []

    providers = []
    for raw in results:
        entry = _provider_entry(raw)
        if entry:
            providers.append(entry)
    providers.sort(key=lambda p: p["priority"])

    with _memo_lock:
        if len(_provider_memo) >= _PROVIDER_MEMO_MAX:
            oldest = sorted(_provider_memo.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[:max(1, _PROVIDER_MEMO_MAX // 10)]:
                del _provider_memo[k]
        _provider_memo[memo_key] = (providers, now)
    return providers
