import os
import tempfile

import pytest

# F56 — PRIVATE Flask internals, used deliberately.
#
# `_cv_app` is the ContextVar Flask binds every pushed application context to,
# and `RequestContext.push` consults it to decide whether to REUSE the bound
# context or create a new one. `_RequestScopedClient` unbinds it around each
# test-client request so the request gets its own context, exactly as on a real
# server.
#
# Compatibility: verified against the pinned Flask==3.1.3, where `_cv_app`
# exists, is a ContextVar, and `push()` uses the rule
# `if app_ctx is None or app_ctx.app is not self.app:`. If a future Flask
# renames or removes it, this import fails at collection — loudly, which is the
# intended failure mode. If the REUSE RULE changes but the name survives, the
# behaviour tests in tests/test_f56_context_isolation.py
# (`test_the_unbinding_assumption_holds_...` and
# `test_the_reuse_assumption_holds_...`) fail and point here. Neither failure
# mode is silent.
from flask.globals import _cv_app
from flask.testing import FlaskClient

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

    # F56: every client this suite hands out — via the `client` fixture or a
    # direct `app.test_client()` call — gets request-scoped contexts. Tests
    # that need Flask-Login's own client (which writes the session directly)
    # still override `test_client_class` themselves and are unaffected.
    flask_app.test_client_class = _RequestScopedClient

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


# ── Per-request application context (F56) ────────────────────────────────────
#
# `pytest-flask` ships an AUTOUSE fixture, `_push_request_context`, that pushes
# an `app.test_request_context()` for the whole test whenever the `app` fixture
# is in play (directly, or via `db` / `client` / `sample_user` / `auth_client`).
# The test body therefore runs with an application context already bound, which
# most tests rely on for `db.session` and for `url_for`.
#
# That silently defeats per-request context isolation. Flask's
# `RequestContext.push` REUSES an already-bound application context when it
# belongs to the same app:
#
#     app_ctx = _cv_app.get(None)
#     if app_ctx is None or app_ctx.app is not self.app:
#         app_ctx = self.app.app_context()
#         app_ctx.push()
#
# so every `client.get(...)` made during a test REUSES the harness context
# instead of pushing its own. `g` is then a single object shared by the whole
# test across all clients, and Flask-Login — which caches the resolved identity
# in exactly that `g` —
#
#     if "_login_user" not in g:
#         current_app.login_manager._load_user()
#     return g._login_user
#
# loads the user only ONCE. The first request that authenticates populates the
# cache, and every later request reads it back — including requests from a
# brand-new, never-used client. One login anywhere in the process therefore
# authenticates everything that follows, and any assertion about "anonymous"
# silently becomes dependent on test order.
#
# Removing the harness context entirely is not an option: `db.session` and
# `url_for` are used directly in test bodies throughout the suite. Instead the
# boundary is moved to where it belongs — the request. `_RequestScopedClient`
# detaches the ambient context for the duration of each request, so the request
# pushes and pops its own exactly as it does on a real server, then restores
# the harness context for the test body. That is the production lifecycle,
# reached from the harness rather than approximated by clearing caches.
class _RequestScopedClient(FlaskClient):
    """A test client whose requests never reuse an ambient application context.

    ``open`` is the single entry point for every request the test client makes
    (``get``/``post``/``open`` all funnel through it), so covering it covers
    the whole client API.

    ``session_transaction`` is deliberately NOT wrapped: it pushes a request
    context only to read and write the cookie jar and runs no view, so it
    cannot populate ``g._login_user`` — the one piece of ``g`` this harness
    cares about. Leaving it alone avoids interfering with Werkzeug's own
    cookie-jar bookkeeping, which several tests rely on.
    """

    def open(self, *args, **kwargs):
        # `_cv_app` is the ContextVar Flask binds every pushed context to.
        # Clearing it is what makes Flask's own `RequestContext.push` take the
        # `app_ctx is None` branch and create a genuine per-request context.
        #
        # `set(None)` rather than `ambient.pop()`: popping would run
        # `do_teardown_appcontext`, which makes Flask-SQLAlchemy call
        # `db.session.remove()` for the *harness* context and detach every
        # instance the test body is still holding. The request gets its own
        # context and its own teardown either way, so the harness context only
        # needs to be unbound and rebound. `reset(token)` puts the previous
        # value back without re-running `push`, so nothing is set up twice.
        ambient = _cv_app.get(None)
        if ambient is None:
            return super().open(*args, **kwargs)

        token = _cv_app.set(None)
        try:
            return super().open(*args, **kwargs)
        finally:
            # Restore the harness context so the test body keeps its `db`
            # session and `url_for`.
            _cv_app.reset(token)


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
