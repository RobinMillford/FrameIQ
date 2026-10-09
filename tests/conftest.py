import os
import tempfile

import pytest

try:  # tests/ is a package; direct execution still works.
    from tests.tmdb_offline import (
        OfflineNetworkViolation, TMDbOfflineRegistry, _violation_message,
    )
    from tests.tv_fixtures import tv_builder
except ImportError:  # pragma: no cover - sys.path forms
    from tmdb_offline import (  # type: ignore[no-redef]
        OfflineNetworkViolation, TMDbOfflineRegistry, _violation_message,
    )
    from tv_fixtures import tv_builder  # type: ignore[no-redef]

# Must be set before any app import.
#
# NOTE: tests deliberately use a TEMP-FILE SQLite, not `sqlite:///:memory:`.
# With :memory:, every pooled connection is a SEPARATE empty database — an
# exception path that opens a fresh connection (e.g. external-API error
# storms under a fake CI TMDb key) then fails with "no such table: user".
# A temp file gives all connections the same database, keeping the suite
# hermetic regardless of key validity. Nothing is persisted: the file is
# removed at session teardown.
_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="frameiq_test_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only")
# Forced override (not setdefault): tests must NEVER inherit another
# DATABASE_URL — not from CI env, not from a local .env with a production
# Postgres URI. All test data lives in this throwaway file, removed at
# session teardown.
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
# The test harness bootstraps its own schema AFTER the app imports (the
# session fixture calls db.create_all() on the empty temp file), so the
# startup schema-parity guard would see an empty database and abort every
# test. Tests are an explicit non-production bootstrap context.
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
# Ensure no actual email is sent during tests
os.environ["MAIL_SERVER"] = ""
# Disable rate limiter entirely during tests
os.environ["RATELIMIT_ENABLED"] = "False"


# ── Offline test policy (Task F3) ───────────────────────────────────────────
# Ordinary tests are OFFLINE: no TMDb key, no DNS, no internet. Real TMDb
# traffic is served from deterministic fixtures (tests/tmdb_offline.py) and
# every other outbound connection fails loudly. Genuine external-integration
# tests opt in with `@pytest.mark.tmdb` and run via `pytest -m tmdb`.

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "tmdb: opt-in integration test that may talk to the real TMDb API "
        "(excluded from the offline suite via -m 'not tmdb')",
    )
    config.addinivalue_line(
        "markers",
        "postgres: opt-in integration test requiring a throwaway PostgreSQL "
        "server (excluded from the offline suite via -m 'not postgres')",
    )


def _is_opt_in_integration(request):
    return request.node.get_closest_marker("tmdb") is not None


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    """Fail loudly on any real outbound connection during an offline test.

    Covers everything that does not go through ``requests`` (httpx/OpenAI,
    urllib, raw sockets). Tests marked ``@pytest.mark.tmdb`` opt out.
    """
    if _is_opt_in_integration(request):
        yield
        return

    import socket

    def _deny(*args, **kwargs):
        target = ", ".join(repr(a) for a in args[:2])
        raise OfflineNetworkViolation(_violation_message(f"socket.connect({target})"))

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    monkeypatch.setattr(socket, "getaddrinfo", _deny)
    yield


@pytest.fixture
def tv(db):
    """Deterministic TV fixture builder shared by the F3 suites.

    Lives in ``tests/tv_fixtures.py`` (which owns the fixed show ids, season
    shapes and builders); registered here because this module owns ``db``.
    Requesting it as a normal fixture parameter keeps test signatures free
    of import shadowing.
    """
    yield from tv_builder(db)


@pytest.fixture
def tmdb(tmdb_offline):
    """Explicit handle on this test's offline TMDb fixture registry.

    ``tmdb.tv_show(994001, {1: 10, 2: 10})`` registers a deterministic
    payload; ``tmdb.count(...)`` / ``tmdb.paths()`` assert network budgets.
    """
    return tmdb_offline


@pytest.fixture(autouse=True)
def tmdb_offline(request, monkeypatch):
    """Deterministic TMDb transport for every offline test.

    Replaces ``requests.sessions.Session.send`` so TMDb responses come from
    the in-process registry instead of the network. Nothing inside FrameIQ
    is stubbed: the real cache, fetchers, parsers, resolvers, routes and
    templates all run against these payloads.
    """
    registry = TMDbOfflineRegistry()
    if _is_opt_in_integration(request):
        yield registry
        return

    import requests.sessions

    def _send(_self, prepared_request, **kwargs):
        return registry.dispatch(prepared_request)

    monkeypatch.setattr(requests.sessions.Session, "send", _send)

    # Per-test isolation: no payload, cached detail or memo may leak between
    # tests through a shared session DB / process cache.
    import api.continue_watching as cw
    from api.availability import _provider_memo
    from api.tmdb.cache import tmdb_cache

    tmdb_cache._store.clear()
    cw._memo.clear()
    _provider_memo.clear()

    yield registry

    tmdb_cache._store.clear()
    cw._memo.clear()
    _provider_memo.clear()


@pytest.fixture(scope="session")
def app():
    from app import app as flask_app
    from models import db as _db

    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SERVER_NAME="localhost",
        MAIL_SERVER="",
        RATELIMIT_ENABLED=False,
    )

    # Push the app context only for schema setup, then pop it. Holding it
    # open for the whole session would make `g` (and Flask-Login's cached
    # `g._login_user`) leak across tests — authenticated state from one
    # test would bleed into "unauthenticated" requests in another.
    with flask_app.app_context():
        _db.create_all()

    yield flask_app

    with flask_app.app_context():
        _db.drop_all()

    try:
        os.remove(_test_db_path)
    except OSError:
        pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    from models import db as _db
    return _db


@pytest.fixture
def sample_user(db, app):
    """Creates and returns a test user, cleaned up after test."""
    from models import User
    with app.app_context():
        u = User(username='testuser', email='test@example.com',
                 email_verified=True)
        u.set_password('TestPass1')
        db.session.add(u)
        db.session.commit()
        yield u
        db.session.delete(u)
        db.session.commit()


@pytest.fixture
def auth_client(client, sample_user):
    """Test client with a logged-in user."""
    client.post('/login', data={
        'username': 'testuser',
        'password': 'TestPass1',
    }, follow_redirects=True)
    return client
