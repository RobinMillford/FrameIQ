import os
import pytest

# Must be set before any app import
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-tests-only")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
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
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
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
