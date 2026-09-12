"""Shared rate-limiter storage (Valkey) — failover & multi-worker tests.

Verifies the opt-in shared storage introduced for the multi-worker
correctness problem (2 Gunicorn workers × memory:// = limits effectively
2× weaker). Uses fakeredis — no real Valkey service is required by pytest.

Locked-in behavior:
  A. default (no RATELIMIT_STORAGE_URI) → per-process memory, unchanged
  B/C. two independent Limiter instances ("workers") over one shared
       backend see the SAME counters and enforce the exact documented
       limit rather than one window per instance
  D. shared storage failure → requests keep serving via in-memory
       fallback counting (protection degraded, never removed, no 500)
  E. failure handling is bounded (single warn + flip, no storm)
  F. all production limit definitions are byte-identical (source check)
"""
import logging

import pytest
import redis as redis_lib
from fakeredis import FakeServer, FakeStrictRedis
from limits.storage.redis import RedisStorage

import extensions


@pytest.fixture
def fake_valkey_uri(monkeypatch):
    """A fakeredis-backed 'valkey' reachable via the production URI shape.

    FakeServer impersonates the real wire protocol; patching redis.from_url
    makes limits' RedisStorage construct against it from the same URI the
    operator would set. Multiple storages built from this fixture share one
    server — the multi-worker topology, without a real service.
    """
    pytest.importorskip("lupa")  # limits' incr is a Lua script; fakeredis needs it
    server = FakeServer()

    def _from_url(uri, **options):
        # Pass through only kwargs FakeStrictRedis accepts.
        return FakeStrictRedis(server=server, **{
            k: v for k, v in options.items()
            if k in ("encoding", "encoding_errors", "decode_responses",
                     "retry_on_timeout")})

    monkeypatch.setattr(redis_lib, "from_url", _from_url)
    return "redis://:test-password@valkey:6379/0"


# ── A. Default: no env var → memory://, unchanged dev/CI behavior ────────────

def test_default_storage_is_memory(monkeypatch):
    # conftest forces RATELIMIT_ENABLED=False suite-wide; the limiter only
    # resolves storage in init_app when enabled, so re-enable for this test.
    monkeypatch.setenv("RATELIMIT_ENABLED", "true")
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    import importlib
    mod = importlib.reload(extensions)
    try:
        assert mod.limiter._storage_uri == "memory://"
        mod.limiter.init_app(__import__("flask").Flask(__name__))
        # Post-init resolution: fallback armed only for shared storage.
        assert mod.limiter._in_memory_fallback_enabled is False
    finally:
        importlib.reload(extensions)


# ── B/C. Two "workers" over one shared backend share exact counters ─────────

def test_shared_counters_across_two_limiter_instances(fake_valkey_uri):
    """Worker A + Worker B (independent Limiter instances, one backend)
    must share ONE window: the 5/min limit is enforced across both."""
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    apps = []
    for _ in range(2):
        app = Flask(__name__)
        limiter = Limiter(get_remote_address,
                          storage_uri=fake_valkey_uri,
                          default_limits=["5 per minute"])
        limiter.init_app(app)

        @app.route("/x")
        @limiter.limit("5 per minute")
        def x():  # noqa: F811
            return "ok"

        apps.append(app)

    hits = 0
    exceeded = False
    for i in range(1, 9):
        resp = apps[i % 2].test_client().get("/x")
        if resp.status_code == 429:
            exceeded = True
            break
        hits += 1

    # The shared window allows exactly 5 successes across BOTH instances.
    assert hits == 5, f"shared window violated: {hits} hits before 429"
    assert exceeded, "shared limit was never enforced"


def test_limiter_uses_shared_storage_when_configured(
        fake_valkey_uri, monkeypatch):
    """With the env var set, the app's own Limiter uses the shared backend
    and has in-memory fallback armed."""
    monkeypatch.setenv("RATELIMIT_ENABLED", "true")
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", fake_valkey_uri)
    import importlib
    mod = importlib.reload(extensions)
    try:
        from flask import Flask
        assert mod.limiter._storage_uri == fake_valkey_uri
        mod.limiter.init_app(Flask(__name__))
        assert isinstance(mod.limiter.storage, RedisStorage)
        assert mod.limiter._in_memory_fallback_enabled is True
    finally:
        monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
        importlib.reload(extensions)


# ── D/E. Valkey failure → in-memory fallback, bounded, serving continues ────

def test_storage_failure_falls_back_to_memory(fake_valkey_uri):
    """Dead storage → requests still served; in-memory counting stays on."""
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    app = Flask(__name__)
    limiter = Limiter(get_remote_address,
                      storage_uri=fake_valkey_uri,
                      in_memory_fallback_enabled=True,
                      default_limits=["2 per minute"])
    limiter.init_app(app)

    @app.route("/y")
    @limiter.limit("2 per minute")
    def y():  # noqa: F811
        return "ok"

    client = app.test_client()
    assert client.get("/y").status_code == 200
    assert client.get("/y").status_code == 200

    # Simulate Valkey death at the client level: limits' Lua scripts bind
    # the redis client at registration time, so the underlying client's
    # execute_command must fail (covers incr scripts AND check()/ping).
    def _boom(*_a, **_k):
        raise ConnectionError("valkey down")

    limiter.storage.storage.execute_command = _boom  # type: ignore[method-assign]

    # Must NOT 500 and must NOT serve uncounted: 200s or 429s only.
    statuses = [client.get("/y").status_code for _ in range(3)]
    assert all(s in (200, 429) for s in statuses), statuses

    # The limiter flipped to its fallback (in-memory) limiter...
    assert limiter._storage_dead is True
    assert limiter._fallback_limiter is not None
    # ...and the fallback is a REAL counting strategy (never fail-open):
    # keep hammering the dead-storage endpoint and a 429 must appear once
    # the in-memory window of 2/min is exhausted.
    codes = {client.get("/y").status_code for _ in range(10)}
    assert 429 in codes, f"fallback did not enforce limits: {codes}"


def test_fallback_flip_is_bounded_not_a_storm(fake_valkey_uri):
    """Repeated failures: one warning + persistent flip; no per-request storm."""
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    app = Flask(__name__)
    limiter = Limiter(get_remote_address,
                      storage_uri=fake_valkey_uri,
                      in_memory_fallback_enabled=True,
                      default_limits=["100 per minute"])
    limiter.init_app(app)

    @app.route("/z")
    @limiter.limit("100 per minute")
    def z():  # noqa: F811
        return "ok"

    client = app.test_client()

    def _boom(*_a, **_k):
        raise ConnectionError("valkey down")

    # Client-level death (see note in test_storage_failure_falls_back_to_memory).
    limiter.storage.storage.execute_command = _boom  # type: ignore[method-assign]

    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    limiter.logger.addHandler(handler)
    try:
        for _ in range(6):
            client.get("/z")
    finally:
        limiter.logger.removeHandler(handler)

    flip_warnings = [r for r in records
                     if r.levelno == logging.WARNING
                     and "falling back to in-memory" in r.getMessage()]
    assert len(flip_warnings) <= 1, (
        "storage-failure warning should fire once per outage, not per request")


# ── F. No production limit definitions changed ───────────────────────────────

def test_route_limit_definitions_unchanged():
    """All @limiter.limit strings across routes/ must be byte-identical
    to the pre-shared-storage values (sharing changes WHERE counters live,
    never the limits)."""
    import pathlib
    import re

    expected = {
        "routes/smart_lists.py": ["30 per minute", "30 per minute",
                                  "30 per minute", "60 per minute"],
        "routes/notifications.py": ["60 per minute", "10 per minute"],
        "routes/watch.py": ["60 per minute"] * 6,
        "routes/recommendations.py": ["60 per minute"],
        "routes/diary.py": ["60 per minute"],
        "routes/tags.py": ["60 per minute"],
        "routes/browse.py": ["10 per minute", "10 per minute"],
        "routes/tmdb_proxy.py": ["180 per minute"],
        "routes/availability.py": ["30 per minute"],
        "routes/chat.py": ["20 per minute; 100 per hour"],
        "routes/auth.py": ["5 per minute; 20 per hour",
                           "10 per minute; 50 per hour",
                           "5 per minute", "10 per minute",
                           "5 per minute; 10 per hour", "10 per minute"],
    }
    for path, limits in expected.items():
        src = pathlib.Path(path).read_text()
        found = re.findall(r"@limiter\.limit\(\"([^\"]+)\"\)", src)
        assert found == limits, f"{path}: limits changed! {found}"


def test_default_limits_unchanged():
    src = open("extensions.py").read()
    assert 'default_limits=["500 per day", "100 per hour"]' in src
