import os
import tempfile

import pytest

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
