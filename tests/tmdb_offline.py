"""Deterministic offline TMDb boundary for the whole test suite (Task F3).

The contract
------------
FrameIQ's test suite must never reach ``api.themoviedb.org`` (or any other
host).  Ordinary CI runs ``pytest tests/`` with a throwaway TMDb key and no
internet; every external payload the application consumes is served from a
small in-process registry of minimal, realistic fixtures registered by the
test that needs them.

Where the boundary sits
-----------------------
One seam, not hundreds: ``requests`` sends every outbound HTTP request
through ``requests.sessions.Session.send``.  ``conftest.py`` replaces that
method for the duration of each test, so:

    fake TMDb payload
        -> real ``cached_tmdb_request``      (cache + retry logic exercised)
        -> real ``fetch_tv_show_details``     (real parser exercised)
        -> real ``aired_positions_for_show``  (real aired rule exercised)
        -> real progress / routes / templates

Nothing inside FrameIQ is stubbed, so the tests still verify FrameIQ logic
rather than their own mocks.  A request to any host that is not a registered
TMDb endpoint raises :class:`OfflineNetworkViolation` — loud, immediate, and
impossible to mistake for a passing test.

Unregistered TMDb endpoints
---------------------------
Return :func:`upstream_failure`, the exact payload shape TMDb returns for an
unusable API key.  The suite historically ran against a dummy key, so this is
byte-for-byte the "upstream unavailable" input the application's graceful
degradation paths were written against; existing degradation tests keep
testing what they always tested, now deterministically.

Measured call counts
-------------------
``registry.calls`` records every outbound URL, so a test can assert the
network budget of a code path (``registry.count('/3/tv/993001')``) instead of
hoping the network stayed quiet. ``registry.reset_calls()`` zeroes the record
while keeping the registered payloads, which is how a budget test measures a
single operation after building its fixture.

Opting in to the real network
-----------------------------
Mark a genuinely external integration test ``@pytest.mark.tmdb``.  Those
tests are excluded from ``pytest -m "not tmdb"`` and are the only place
allowed to talk to the real service.
"""
import json
import re
from urllib.parse import urlparse

TMDB_HOSTS = frozenset({"api.themoviedb.org", "image.tmdb.org"})

# TMDb's own response for an unusable/missing API key. Matches what the suite
# has always degraded against, so it is the faithful "no upstream data" input.
_UPSTREAM_FAILURE_STATUS = 401
_UPSTREAM_FAILURE_MESSAGE = (
    "Invalid API key: You must be granted a valid key."
)


def upstream_failure():
    """The TMDb 'something is wrong upstream' body (dict, never raises)."""
    return {
        "success": False,
        "status_code": _UPSTREAM_FAILURE_STATUS,
        "status_message": _UPSTREAM_FAILURE_MESSAGE,
    }


class OfflineNetworkViolation(RuntimeError):
    """Raised when offline test code attempts a real outbound connection."""


def _violation_message(target):
    return (
        f"Unexpected network access during offline test: {target}\n"
        "The FrameIQ test suite is offline by design. Either\n"
        "  * register a deterministic fixture for this endpoint\n"
        "    (tests/tmdb_offline.py — see the tv_show()/movie() builders), or\n"
        "  * mark the test @pytest.mark.tmdb if it genuinely verifies the\n"
        "    real external service (opt-in: pytest -m tmdb)."
    )


# ── Payload builders ────────────────────────────────────────────────────────
# Minimal realistic TMDb shapes — only the fields the application reads.

DEFAULT_SEASONS = {1: 10}

# ``None`` means "use the sensible default anchor"; NO_ANCHOR means "omit
# last_episode_to_air entirely", which is a real degraded TMDb shape and
# cannot be expressed by passing ``None``.
NO_ANCHOR = object()


def _season_entry(season_number, episode_count, air_date="2010-01-01"):
    return {
        "id": season_number,
        "name": ("Specials" if season_number == 0 else f"Season {season_number}"),
        "season_number": season_number,
        "overview": f"Season {season_number} overview",
        "air_date": air_date,
        "episode_count": episode_count,
        "poster_path": f"/season{season_number}.jpg",
    }


def tv_show(show_id, seasons=None, *, status="Ended", name=None,
            last_episode_to_air=None, last_air_date="2010-12-31",
            first_air_date="2010-01-01", genres=("Drama",),
            season_entries=None, **extra):
    """Full ``/3/tv/{id}`` payload (append_to_response=credits,videos,
    recommendations,reviews,seasons), shaped for ``fetch_tv_show_details``.

    ``seasons`` is ``{season_number: episode_count}``.  ``last_episode_to_air``
    defaults to the final episode of the highest non-special season, which is
    the aired anchor ``api/user_view_state.py`` resolves against.

    ``season_entries`` replaces the generated ``seasons`` list verbatim — the
    hook for degraded/edge metadata (null counts, null season numbers, an
    empty list) that ``{season: count}`` cannot express.
    """
    seasons = dict(seasons or DEFAULT_SEASONS)
    ordered = sorted(seasons)
    real_seasons = [sn for sn in ordered if sn > 0]
    anchor_season = real_seasons[-1] if real_seasons else 1
    if last_episode_to_air is None:
        last_episode_to_air = {
            "season_number": anchor_season,
            "episode_number": seasons.get(anchor_season, 0),
            "name": f"Episode {seasons.get(anchor_season, 0)}",
            "air_date": last_air_date,
        }
    elif last_episode_to_air is NO_ANCHOR:
        last_episode_to_air = None
    payload = {
        "id": show_id,
        "name": name or f"Fixture Show {show_id}",
        "overview": "Fixture overview",
        "tagline": "Fixture tagline",
        "first_air_date": first_air_date,
        "last_air_date": last_air_date,
        "last_episode_to_air": last_episode_to_air,
        "number_of_seasons": len(real_seasons),
        "number_of_episodes": sum(seasons[sn] for sn in real_seasons),
        "vote_average": 7.5,
        "vote_count": 100,
        "status": status,
        "original_language": "en",
        "poster_path": f"/poster{show_id}.jpg",
        "backdrop_path": f"/backdrop{show_id}.jpg",
        "genres": [{"id": 18, "name": g} for g in genres],
        "credits": {
            "cast": [
                {"id": 1001, "name": "Fixture Actor",
                 "character": "Fixture Character",
                 "profile_path": "/actor.jpg"},
            ],
            "crew": [{"id": 2001, "name": "Fixture Creator",
                      "job": "Creator", "department": "Writing"}],
        },
        "videos": {"results": []},
        "recommendations": {"results": []},
        "reviews": {"results": []},
        "seasons": ([_season_entry(sn, seasons[sn]) for sn in ordered]
                    if season_entries is None else list(season_entries)),
        "created_by": [{"id": 2001, "name": "Fixture Creator"}],
        "origin_country": ["US"],
    }
    payload.update(extra)
    return payload


def tv_list(shows=None, count=3, **fields):
    """``/3/tv/popular``-shaped list payload (poster + name required)."""
    if shows is None:
        shows = [
            {"id": 900000 + i, "name": f"List Show {i}",
             "poster_path": f"/p{i}.jpg", "first_air_date": "2011-01-01",
             "vote_average": 7.0}
            for i in range(1, count + 1)
        ]
    results = []
    for show in shows:
        entry = dict(show)
        entry.update(fields)
        results.append(entry)
    return {"page": 1, "results": results, "total_pages": 1, "total_results": len(results)}


def movie(movie_id, *, title=None, release_date="2010-01-01", runtime=120,
          genres=("Drama",), **extra):
    """Full ``/3/movie/{id}`` payload for ``fetch_movie_details``."""
    payload = {
        "id": movie_id,
        "title": title or f"Fixture Movie {movie_id}",
        "overview": "Fixture overview",
        "tagline": "Fixture tagline",
        "release_date": release_date,
        "runtime": runtime,
        "vote_average": 7.5,
        "vote_count": 100,
        "status": "Released",
        "original_language": "en",
        "budget": 1000000,
        "revenue": 2000000,
        "poster_path": f"/mposter{movie_id}.jpg",
        "backdrop_path": f"/mbackdrop{movie_id}.jpg",
        "genres": [{"id": 18, "name": g} for g in genres],
        "credits": {
            "cast": [{"id": 1001, "name": "Fixture Actor",
                      "character": "Fixture Character",
                      "profile_path": "/actor.jpg"}],
            "crew": [{"id": 3001, "name": "Fixture Director",
                      "job": "Director", "department": "Directing"}],
        },
        "videos": {"results": []},
        "recommendations": {"results": []},
        "reviews": {"results": []},
    }
    payload.update(extra)
    return payload


def movie_list(count=3, **fields):
    """``/3/movie/popular``-shaped list payload (poster + title required)."""
    results = []
    for i in range(1, count + 1):
        entry = {"id": 800000 + i, "title": f"List Movie {i}",
                 "poster_path": f"/m{i}.jpg", "backdrop_path": f"/mb{i}.jpg",
                 "release_date": "2011-01-01"}
        entry.update(fields)
        results.append(entry)
    return {"page": 1, "results": results, "total_pages": 1, "total_results": len(results)}


def season_details(show_id, season_number, episodes=None, *, episode_count=None):
    """``/3/tv/{id}/season/{n}``-shaped payload."""
    if episodes is None:
        total = episode_count or 10
        episodes = [
            {
                "id": show_id * 1000 + en,
                "name": f"S{season_number}E{en}",
                "episode_number": en,
                "season_number": season_number,
                "overview": f"Episode {en}",
                "air_date": "2010-01-01",
                "runtime": 50,
                "still_path": f"/s{season_number}e{en}.jpg",
            }
            for en in range(1, total + 1)
        ]
    return {
        "id": season_number,
        "name": f"Season {season_number}",
        "season_number": season_number,
        "overview": f"Season {season_number} overview",
        "air_date": "2010-01-01",
        "episodes": episodes,
        "poster_path": f"/season{season_number}.jpg",
    }


def release_dates(*regions):
    """``/3/movie/{id}/release_dates``-shaped payload (order 3 = theatrical)."""
    if not regions:
        regions = ("US",)
    return {
        "id": 1,
        "results": [
            {"iso_3166_1": region,
             "release_dates": [{"release_date": "2010-01-01", "type": 3}]}
            for region in regions
        ],
    }


def watch_providers(movie=True, *, region="US", stream=None, link=None):
    """``/3/{movie,tv}/{id}/watch/providers``-shaped payload."""
    if stream is None:
        stream = [{"provider_id": 8, "provider_name": "Netflix",
                   "logo_path": "/netflix.jpg", "display_priority": 8}]
    kind = "movie" if movie else "tv"
    return {
        "id": 1,
        "results": {
            region: {
                "link": link or
                f"https://www.themoviedb.org/{kind}/1/watch?locale={region}",
                "flatrate": stream,
            }
        },
    }


def images():
    """``/3/{movie,tv}/{id}/images``-shaped payload."""
    return {
        "id": 1,
        "backdrops": [{"file_path": "/backdrop.jpg", "width": 1920,
                       "height": 1080, "iso_639_1": None}],
        "posters": [{"file_path": "/poster.jpg", "width": 500,
                     "height": 750, "iso_639_1": "en"}],
    }


# ── Registry + transport fake ───────────────────────────────────────────────

class TMDbOfflineRegistry:
    """URL-regex -> payload registry plus a record of every outbound call."""

    def __init__(self):
        self._routes = []
        self.calls = []
        self.blocked = []

    # -- registration -----------------------------------------------------
    def register(self, path_pattern, payload, host=None):
        """Register a payload for requests whose URL *path* matches
        ``path_pattern`` (a regex, anchored with ``match``).

        Re-registering the SAME pattern replaces the earlier payload instead
        of queueing behind it. Without this, a test that registers an
        endpoint and then registers it again with a changed payload (the
        natural way to simulate "a new episode aired") would silently keep
        serving the FIRST payload forever, because dispatch returns on the
        first match — the second registration would be dead code that reads
        as if it had taken effect.
        """
        if isinstance(path_pattern, str):
            path_pattern = re.compile(path_pattern)
        self._routes = [route for route in self._routes
                        if not (route[0].pattern == path_pattern.pattern
                                and route[1] == host)]
        self._routes.append((path_pattern, host, payload))
        return self

    def reset(self):
        """Drop every payload AND every recorded call (a blank slate)."""
        self.reset_calls()
        self._routes.clear()

    def reset_calls(self):
        """Forget the recorded calls but KEEP the registered payloads.

        This is what a network-budget test wants after building its fixture:
        "count only what this operation does", not "start over".
        """
        self.calls.clear()
        self.blocked.clear()

    # -- convenience builders --------------------------------------------
    def tv_show(self, show_id, seasons=None, **kwargs):
        payload = tv_show(show_id, seasons, **kwargs)
        self.register(rf"^/3/tv/{show_id}$", payload)
        return payload

    def movie(self, movie_id, **kwargs):
        payload = movie(movie_id, **kwargs)
        self.register(rf"^/3/movie/{movie_id}$", payload)
        return payload

    def register_list(self, path, payload):
        self.register(rf"^{re.escape(path)}$", payload)
        return payload

    def season(self, show_id, season_number, **kwargs):
        payload = season_details(show_id, season_number, **kwargs)
        self.register(rf"^/3/tv/{show_id}/season/{season_number}$", payload)
        return payload

    def providers(self, media_type, tmdb_id, **kwargs):
        payload = watch_providers(media_type == "movie", **kwargs)
        self.register(rf"^/3/{media_type}/{tmdb_id}/watch/providers$", payload)
        return payload

    # -- observation ------------------------------------------------------
    def count(self, path_pattern):
        """Number of recorded calls whose path matches ``path_pattern``."""
        regex = re.compile(path_pattern) if isinstance(path_pattern, str) \
            else path_pattern
        return sum(1 for url in self.calls if regex.search(urlparse(url).path))

    def paths(self):
        return [urlparse(url).path for url in self.calls]

    # -- transport --------------------------------------------------------
    def dispatch(self, request):
        """Serve one intercepted ``requests`` prepared request."""
        url = request.url or ""
        parsed = urlparse(url)
        self.calls.append(url)

        if parsed.hostname not in TMDB_HOSTS:
            self.blocked.append(url)
            raise OfflineNetworkViolation(_violation_message(url))

        for pattern, host, payload in self._routes:
            if host is not None and host != parsed.hostname:
                continue
            if pattern.match(parsed.path):
                body = payload(request) if callable(payload) else payload
                return _build_response(request, 200, body)

        # Unregistered TMDb endpoint: the upstream-unavailable shape.
        return _build_response(
            request, _UPSTREAM_FAILURE_STATUS, upstream_failure())


def _build_response(request, status_code, payload):
    """A real ``requests.Response`` so callers exercise their real code."""
    from requests import Response

    response = Response()
    response.status_code = status_code
    response.url = request.url
    response.request = request
    response.reason = "OK" if status_code == 200 else "Error"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode("utf-8")
    return response
